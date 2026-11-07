"""Deferred MCP outcomes, using the Extension host's Session receipt delivery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from core.database import DatabaseUnavailableError
from core.tools.tools import ToolContext, tool_failure, tool_success

from ._invocation import CANONICAL_CONTEXT, RETAIN_CALL
from ._recovery import TaskRecovery, _decode, configuration_hash

if TYPE_CHECKING:
    from .client import ConnectionRunner

Finish = Callable[
    ["ConnectionRunner | str", ToolContext, dict[str, Any], Awaitable[dict[str, Any]]],
    Awaitable[dict[str, Any]],
]
_wait = asyncio.wait
_sleep = asyncio.sleep


def _unknown(reason: str) -> dict[str, Any]:
    return tool_failure(
        "mcp_call_unconfirmed",
        f"The call's outcome is unknown: {reason}. It was not repeated. "
        "Check the target's state before deciding whether to call it again.",
    )


@dataclass
class _Pending:
    runner: ConnectionRunner
    context: ToolContext
    invocation: asyncio.Task[dict[str, Any]]
    persisted: asyncio.Event


class BackgroundCalls:
    """The MCP owner of waits that outlive their initial Tool Result.

    Receipt validity, Session routing, final-delivery deduplication and wakeup belong
    to the host. This owner retains MCP outcomes and native server-task handles only.
    """

    def __init__(self, recovery: TaskRecovery, finish: Finish) -> None:
        self.recovery = recovery
        self.host = recovery.host
        self.finish = finish
        self._pending: dict[str, _Pending] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._connections: dict[str, str] = {}
        self._cancellations: set[str] = set()
        self._closed = False

    async def invoke(
        self, runner: ConnectionRunner, context: ToolContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        canonical = CANONICAL_CONTEXT.get() or context
        if context.programmatic:
            # A program consumes the complete result, not a conversational handoff.
            # Retain its request deadline-free while its owning Run keeps waiting.
            token = RETAIN_CALL.set(True)
            try:
                return await self.finish(
                    runner, context, arguments, runner.invoke("tools/call", arguments, context)
                )
            finally:
                RETAIN_CALL.reset(token)
        if (
            self.recovery.database is None
            or self.host.defer_tool_call is None
            or self.host.restore_tool_call is None
            or self.host.deliver_tool_outcome is None
            or canonical.result_persisted_hook is None
        ):
            return await self.finish(
                runner, context, arguments, runner.invoke("tools/call", arguments, context)
            )
        token = RETAIN_CALL.set(True)
        try:
            invocation = asyncio.create_task(
                runner.invoke("tools/call", arguments, context), name=f"mcp-wait:{runner.id}"
            )
        finally:
            RETAIN_CALL.reset(token)
        detached = False
        try:
            done, _ = await _wait({invocation}, timeout=runner.config["timeout"])
            if done:
                return await self.finish(runner, context, arguments, invocation)
            receipt = await self.host.defer_tool_call(canonical)
            if receipt is None:
                # A caller without a durable Session must keep ownership of its wait.
                return await self.finish(runner, context, arguments, invocation)
            self.recovery.deferred_receipts.add(receipt)
            await self.recovery.database.write_async(
                lambda connection: connection.execute(
                    "INSERT INTO pending_calls(receipt_id,connection_id,configuration_hash,"
                    "arguments_json) VALUES(?,?,?,?) ON CONFLICT(receipt_id) DO NOTHING",
                    (receipt, runner.id, configuration_hash(runner.config), json.dumps(arguments)),
                )
            )
            persisted = asyncio.Event()
            canonical.after_result_persisted(persisted.set)

            def cancel_unpublished() -> None:
                if not persisted.is_set():
                    invocation.cancel()
                    # The Run's cancellation boundary may store an interrupted result
                    # instead. Its receipt still owns a final note without a wakeup.
                    persisted.set()

            canonical.on_cancel(cancel_unpublished)
            if canonical.is_cancelled():
                cancel_unpublished()
            pending = _Pending(runner, context, invocation, persisted)
            self._pending[receipt] = pending
            self._start(receipt, runner.id, self._complete(receipt, pending, arguments))
            detached = True
            return tool_success(
                {
                    "state": "running",
                    "result_id": receipt,
                    "message": (
                        "The call is still running in the background. Its final result will "
                        "arrive in this Session. Do not repeat the call while it is running."
                    ),
                    "read": {"action": "read", "result_id": receipt},
                    "cancel": {"action": "cancel", "result_id": receipt},
                }
            )
        finally:
            if not detached and not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)

    def _start(self, receipt: str, connection: str, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work, name=f"mcp-background:{connection}")
        self._workers[receipt] = task
        self._connections[receipt] = connection

        def finished(task: asyncio.Task[None]) -> None:
            if self._workers.get(receipt) is task:
                self._workers.pop(receipt, None)
                self._pending.pop(receipt, None)
                self._connections.pop(receipt, None)
                self._cancellations.discard(receipt)
            if not task.cancelled() and (error := task.exception()) is not None:
                self.recovery.logger.error("MCP background outcome failed", exc_info=error)

        task.add_done_callback(finished)

    async def _complete(self, receipt: str, pending: _Pending, arguments: dict[str, Any]) -> None:
        # The result payload hook belongs to the already returned Tool Result.
        context = replace(pending.context, result_payload_hook=None, result_persisted_hook=None)
        try:
            outcome = await self.finish(pending.runner, context, arguments, pending.invocation)
        except asyncio.CancelledError:
            if self._closed:
                raise
            outcome = _unknown("cancellation was requested; remote effects may already exist")
        outcome = await self._save(receipt, outcome)
        await pending.persisted.wait()
        await self._deliver(receipt, pending.runner.id, outcome)

    async def _save(self, receipt: str, outcome: dict[str, Any]) -> dict[str, Any]:
        assert self.recovery.database is not None

        def save(db: Any) -> dict[str, Any]:
            db.execute(
                "UPDATE pending_calls SET outcome_json=? WHERE receipt_id=? "
                "AND outcome_json IS NULL",
                (json.dumps(outcome), receipt),
            )
            row = db.execute(
                "SELECT outcome_json FROM pending_calls WHERE receipt_id=?", (receipt,)
            ).fetchone()
            return cast(dict[str, Any], json.loads(row["outcome_json"]))

        while True:
            try:
                saved = await self.recovery.database.write_async(save)
                await self.recovery.forget(receipt)
                return saved
            except DatabaseUnavailableError, OSError:
                await _sleep(30)

    async def _deliver(self, receipt: str, connection: str, outcome: dict[str, Any]) -> None:
        assert self.host.restore_tool_call is not None
        assert self.host.deliver_tool_outcome is not None
        assert self.recovery.database is not None
        while not self._closed:
            try:
                original = await self.host.restore_tool_call(receipt)
            except DatabaseUnavailableError, OSError:
                await _sleep(30)
                continue
            if original is None:
                await self._forget(receipt)
                return
            encoded = json.dumps(outcome, ensure_ascii=True)
            result = (
                {"result": outcome}
                if len(encoded) <= 6000
                else {"preview": encoded[:3000], "complete": False}
            )
            data = {
                "tool": original.tool_name,
                "result_id": receipt,
                **result,
                "read": {"tool": f"mcp_{connection}", "action": "read", "result_id": receipt},
            }
            quoted = (
                json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
            )
            message = (
                "MCP background call finished. The original call was not repeated. "
                "Result or error (quoted data): " + quoted
            )
            try:
                delivered = await self.host.deliver_tool_outcome(receipt, message)
            except DatabaseUnavailableError, OSError:
                delivered = False
            if delivered:
                try:
                    await self.recovery.database.write_async(
                        lambda db: db.execute(
                            "UPDATE pending_calls SET delivered=1 WHERE receipt_id=?", (receipt,)
                        )
                    )
                    return
                except DatabaseUnavailableError, OSError:
                    pass  # The host deduplicates a repeated final delivery after an ack failure.
            await _sleep(30)

    async def resume(self, runners: dict[str, ConnectionRunner]) -> None:
        database = self.recovery.database
        if database is None or self._closed:
            return
        rows = await database.read_async(
            lambda db: [dict(row) for row in db.execute("SELECT * FROM pending_calls")]
        )
        self.recovery.deferred_receipts.update(row["receipt_id"] for row in rows)
        for row in rows:
            receipt = row["receipt_id"]
            if receipt in self._workers:
                continue
            if row["delivered"]:
                if (
                    self.host.restore_tool_call is not None
                    and await self.host.restore_tool_call(receipt) is None
                ):
                    await self._forget(receipt)
                continue
            self._start(
                receipt, row["connection_id"], self._recover(row, runners.get(row["connection_id"]))
            )

    async def _recover(self, row: dict[str, Any], runner: ConnectionRunner | None) -> None:
        assert self.recovery.database is not None
        assert self.host.restore_tool_call is not None
        receipt = row["receipt_id"]
        context = await self.host.restore_tool_call(receipt)
        if context is None:
            await self._forget(receipt)
            return
        if row["outcome_json"] is not None:
            outcome = json.loads(row["outcome_json"])
        elif row["received_json"] is not None:

            async def received() -> dict[str, Any]:
                return cast(dict[str, Any], json.loads(row["received_json"]))

            outcome = await self.finish(
                row["connection_id"], context, json.loads(row["arguments_json"]), received()
            )
            outcome = await self._save(receipt, outcome)
        else:
            native = await self.recovery.database.read_async(
                lambda db: db.execute(
                    "SELECT * FROM pending_tasks WHERE receipt_id=?", (receipt,)
                ).fetchone()
            )
            if native is not None and native["outcome_json"] is not None:

                async def stored() -> dict[str, Any]:
                    return cast(dict[str, Any], json.loads(native["outcome_json"]))

                outcome = await self.finish(
                    row["connection_id"], context, json.loads(row["arguments_json"]), stored()
                )
            elif receipt in self._cancellations:
                if (
                    native is not None
                    and runner is not None
                    and runner.config["enabled"]
                    and configuration_hash(runner.config) == row["configuration_hash"]
                ):
                    run = _decode(native["task_json"], receipt)
                    with suppress(ValueError, OSError):
                        await runner.invoke("tasks/cancel", {"taskId": run.state.task_id}, context)
                outcome = _unknown("cancellation was requested; remote effects may already exist")
            elif (
                native is not None
                and runner is not None
                and runner.config["enabled"]
                and configuration_hash(runner.config) == row["configuration_hash"]
            ):
                run = _decode(native["task_json"], receipt)
                arguments = json.loads(row["arguments_json"])

                async def recover() -> dict[str, Any]:
                    while True:
                        try:
                            return await runner.recover_task(run, context, arguments)
                        except ValueError, OSError:
                            # Retry the same native Task handle, never tools/call.
                            await _sleep(30)

                invocation = asyncio.create_task(recover(), name=f"mcp-resumed:{runner.id}")
                persisted = asyncio.Event()
                persisted.set()
                pending = _Pending(runner, context, invocation, persisted)
                self._pending[receipt] = pending
                await self._complete(receipt, pending, arguments)
                return
            else:
                outcome = _unknown("the MCP wait was interrupted before a result was saved")
            outcome = await self._save(receipt, outcome)
        await self._deliver(receipt, row["connection_id"], outcome)

    async def load_result(
        self, receipt: str, context: ToolContext, connection: str
    ) -> dict[str, Any] | None:
        if self.recovery.database is None or self.host.restore_tool_call is None:
            return None
        original = await self.host.restore_tool_call(receipt)
        if original is None or (original.project_id, original.agent_id, original.session_id) != (
            context.project_id,
            context.agent_id,
            context.session_id,
        ):
            return None
        row = await self.recovery.database.read_async(
            lambda db: db.execute(
                "SELECT arguments_json,outcome_json FROM pending_calls "
                "WHERE receipt_id=? AND connection_id=?",
                (receipt, connection),
            ).fetchone()
        )
        if row is None:
            return None
        return {
            "connection": connection,
            "source": json.loads(row["arguments_json"]).get("name"),
            "payload": json.loads(row["outcome_json"])
            if row["outcome_json"] is not None
            else {
                "state": "running",
                "result_id": receipt,
                "message": "The final result will arrive in this Session. Do not repeat the call.",
            },
        }

    async def cancel(self, receipt: str) -> dict[str, Any]:
        pending = self._pending.get(receipt)
        if (pending is not None and not pending.invocation.done()) or (
            pending is None and receipt in self._workers
        ):
            self._cancellations.add(receipt)
            if pending is not None:
                pending.invocation.cancel()
            return tool_success(
                {
                    "state": "cancellation_requested",
                    "result_id": receipt,
                    "message": (
                        "Cancellation was requested. Remote effects may already exist; "
                        "the final outcome will arrive in this Session."
                    ),
                }
            )
        return tool_success(
            {
                "state": "finished",
                "result_id": receipt,
                "read": {"action": "read", "result_id": receipt},
            }
        )

    async def _forget(self, receipt: str) -> None:
        assert self.recovery.database is not None
        await self.recovery.database.write_async(
            lambda db: db.execute("DELETE FROM pending_calls WHERE receipt_id=?", (receipt,))
        )
        self.recovery.deferred_receipts.discard(receipt)
        await self.recovery.forget(receipt)

    async def stop_connection(self, connection: str) -> None:
        for pending in tuple(self._pending.values()):
            if pending.runner.id == connection:
                pending.invocation.cancel()
        # Local result preparation/delivery is independent of the connection. A
        # recovery not yet admitted to a runner observes this flag before resuming.
        for receipt in tuple(self._workers):
            if self._connections.get(receipt) == connection and receipt not in self._pending:
                self._cancellations.add(receipt)

    async def _save_received(self, receipt: str, payload: dict[str, Any]) -> None:
        assert self.recovery.database is not None
        await self.recovery.database.write_async(
            lambda db: db.execute(
                "UPDATE pending_calls SET received_json=? "
                "WHERE receipt_id=? AND outcome_json IS NULL",
                (json.dumps(payload), receipt),
            )
        )

    async def close(self) -> None:
        self._closed = True
        for receipt, pending in tuple(self._pending.items()):
            # A received reply is already known even if local attachment/result
            # preparation is still awaiting I/O. Preserve it before cancelling that work.
            if (
                pending.invocation.done()
                and not pending.invocation.cancelled()
                and pending.invocation.exception() is None
            ):
                assert self.recovery.database is not None
                try:
                    await self._save_received(receipt, pending.invocation.result())
                except (DatabaseUnavailableError, OSError) as error:
                    # Storage failure must not prevent stopping the live request.
                    self.recovery.logger.warning(
                        "MCP received reply could not be saved during shutdown (error_type=%s)",
                        type(error).__name__,
                    )
            pending.runner.preserve_task(pending.context)
            pending.invocation.cancel()
        invocations = tuple(pending.invocation for pending in self._pending.values())
        tasks = tuple(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if invocations:
            await asyncio.gather(*invocations, return_exceptions=True)
