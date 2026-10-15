"""Durable server-task waits, resumed without repeating the original Tool call."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import weakref
from dataclasses import asdict
from functools import partial
from typing import TYPE_CHECKING, Any

from core.extensions.databases import Database
from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext

from .content import ContentStore

if TYPE_CHECKING:
    from ._tasks import TaskRun
    from .client import ConnectionRunner

TASK_SCHEMA = """
CREATE TABLE pending_tasks(
    receipt_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    task_json TEXT NOT NULL CHECK(json_valid(task_json)),
    arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
    outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1))
) STRICT;
"""
_sleep = asyncio.sleep
_RETRY_SECONDS = 30.0


def configuration_hash(config: dict[str, Any]) -> str:
    selection = {
        key: value
        for key, value in config.items()
        if key not in {"description", "enabled", "timeout"}
    }
    return hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()


class TaskRecovery:
    def __init__(self, host: ExtensionHost, logger: Any) -> None:
        self.host = host
        self.logger = logger
        self.database: Database | None = None
        self.content = ContentStore(host)
        self._acknowledgments: set[asyncio.Task[None]] = set()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._runs: dict[str, TaskRun] = {}
        self._connections: dict[str, str] = {}
        # Calls captured in this process retain their ordinary delivery owner,
        # including reconnects and the gap before canonical result persistence.
        self._live_receipts: set[str] = set()
        self._retired: weakref.WeakSet[ConnectionRunner] = weakref.WeakSet()
        self._capturing = asyncio.Lock()
        self._closed = False

    async def open(self) -> None:
        if self.host.open_database is not None and self.host.capture_tool_call is not None:
            self.database = await self.host.open_database("tasks", TASK_SCHEMA)

    async def remember(
        self,
        runner: ConnectionRunner,
        context: ToolContext | None,
        arguments: dict[str, Any],
        run: TaskRun,
    ) -> None:
        database = self.database
        if database is None or context is None or run.receipt_id is not None:
            return
        assert self.host.capture_tool_call is not None
        receipt = await self.host.capture_tool_call(context)
        if receipt is None:
            return
        async with self._capturing:
            if self._closed or runner in self._retired:
                return
            run.receipt_id = receipt
            self._live_receipts.add(receipt)
            payload = _encode(run)
            await database.write_async(
                lambda connection: connection.execute(
                    "INSERT INTO pending_tasks(receipt_id,connection_id,configuration_hash,"
                    "task_json,arguments_json) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(receipt_id) DO NOTHING",
                    (
                        receipt,
                        runner.id,
                        configuration_hash(runner.config),
                        payload,
                        json.dumps(arguments),
                    ),
                )
            )
            if context.result_persisted_hook is not None:
                context.after_result_persisted(lambda: self._acknowledge(receipt))

    async def checkpoint(self, run: TaskRun) -> None:
        if self.database is not None and run.receipt_id is not None:
            payload = _encode(run)
            await self.database.write_async(
                lambda connection: connection.execute(
                    "UPDATE pending_tasks SET task_json=? WHERE receipt_id=?",
                    (payload, run.receipt_id),
                )
            )

    async def finish(self, run: TaskRun, outcome: dict[str, Any]) -> None:
        if self.database is not None and run.receipt_id is not None:
            await self.database.write_async(
                lambda connection: connection.execute(
                    "UPDATE pending_tasks SET outcome_json=? WHERE receipt_id=?",
                    (json.dumps(outcome), run.receipt_id),
                )
            )

    async def forget(self, receipt: str) -> None:
        if self.database is not None:
            await self.database.write_async(
                lambda connection: connection.execute(
                    "DELETE FROM pending_tasks WHERE receipt_id=?",
                    (receipt,),
                )
            )
        self._live_receipts.discard(receipt)

    def _acknowledge(self, receipt: str) -> None:
        if not self._closed:
            task = asyncio.create_task(self.forget(receipt), name="mcp-task-acknowledge")
            self._acknowledgments.add(task)
            task.add_done_callback(self._acknowledgments.discard)
            task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self.logger.error(
                "MCP task recovery failed (error_type=%s)", type(error).__name__, exc_info=error
            )

    async def resume(self, runners: dict[str, ConnectionRunner]) -> None:
        if self.database is None:
            return
        rows = await self.database.read_async(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    "SELECT receipt_id,connection_id,configuration_hash,task_json,"
                    "arguments_json,outcome_json,delivered FROM pending_tasks"
                )
            ]
        )
        for row in rows:
            receipt = row["receipt_id"]
            if receipt in self._workers or receipt in self._live_receipts:
                continue
            if row["delivered"]:
                if (
                    self.host.restore_tool_call is not None
                    and await self.host.restore_tool_call(receipt) is None
                ):
                    await self.forget(receipt)
                continue
            runner = runners.get(row["connection_id"])
            if (
                runner is None
                or not runner.config["enabled"]
                or configuration_hash(runner.config) != row["configuration_hash"]
            ):
                continue
            task = asyncio.create_task(
                self._recover(runner, row), name=f"mcp-task-recovery:{runner.id}"
            )
            self._workers[receipt] = task
            self._connections[receipt] = runner.id
            task.add_done_callback(partial(self._worker_finished, receipt))
            task.add_done_callback(self._finished)

    def _worker_finished(self, receipt: str, task: asyncio.Task[None]) -> None:
        if self._workers.get(receipt) is task:
            self._workers.pop(receipt)

    async def _recover(self, runner: ConnectionRunner, row: dict[str, Any]) -> None:
        assert self.host.restore_tool_call is not None
        assert self.host.deliver_tool_outcome is not None
        receipt = row["receipt_id"]
        run = _decode(row["task_json"], receipt)
        self._runs[receipt] = run
        try:
            while not self._closed:
                context = await self.host.restore_tool_call(receipt)
                if context is None:
                    await self.forget(receipt)
                    return
                try:
                    outcome = (
                        json.loads(row["outcome_json"])
                        if row["outcome_json"]
                        else await runner.recover_task(
                            run, context, json.loads(row["arguments_json"])
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except ValueError, OSError:
                    # Only a completed stored outcome is deliverable. Transport failures
                    # keep the same handle for a later read; they never resend tools/call.
                    await _sleep(_RETRY_SECONDS)
                    continue
                outcome, _artifacts = await self.content.preserve(outcome)
                await self.finish(run, outcome)
                row["outcome_json"] = json.dumps(outcome)
                text = _outcome_note(context.tool_name, runner.id, receipt, outcome)
                await self.host.deliver_tool_outcome(receipt, text)
                assert self.database is not None
                await self.database.write_async(
                    lambda connection: connection.execute(
                        "UPDATE pending_tasks SET delivered=1 WHERE receipt_id=?",
                        (receipt,),
                    )
                )
                return
        finally:
            self._runs.pop(receipt, None)
            self._connections.pop(receipt, None)

    async def load_result(
        self, receipt: str, context: ToolContext, connection_id: str
    ) -> dict[str, Any] | None:
        if self.database is None or self.host.restore_tool_call is None:
            return None
        original = await self.host.restore_tool_call(receipt)
        if original is None or (original.project_id, original.agent_id, original.session_id) != (
            context.project_id,
            context.agent_id,
            context.session_id,
        ):
            return None
        row = await self.database.read_async(
            lambda connection: connection.execute(
                "SELECT outcome_json,arguments_json FROM pending_tasks WHERE receipt_id=? AND "
                "connection_id=? AND delivered=1",
                (receipt, connection_id),
            ).fetchone()
        )
        if row is None:
            return None
        return {
            "connection": connection_id,
            "source": json.loads(row["arguments_json"]).get("name"),
            "payload": json.loads(row["outcome_json"]),
        }

    async def stop_connection(
        self,
        connection_id: str,
        *,
        runner: ConnectionRunner | None = None,
        remove: bool = False,
    ) -> None:
        # Fence late captures before stopping the connection; a reused id belongs
        # to a different runner and must never revive the retired invocation.
        async with self._capturing:
            if runner is not None:
                self._retired.add(runner)
        workers = [
            task
            for receipt, task in self._workers.items()
            if self._connections.get(receipt) == connection_id
        ]
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if remove and self.database is not None:
            await self.database.write_async(
                lambda connection: connection.execute(
                    "DELETE FROM pending_tasks WHERE connection_id=?",
                    (connection_id,),
                )
            )

    async def close(self) -> None:
        self._closed = True
        for run in self._runs.values():
            run.kept = True
        workers = tuple(self._workers.values())
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if self._acknowledgments:
            await asyncio.gather(*tuple(self._acknowledgments), return_exceptions=True)


def _encode(run: TaskRun) -> str:
    return json.dumps(
        {
            "state": asdict(run.state),
            "legacy": run.legacy,
            "received_at": run.received_at,
            "answers": {
                key: value.model_dump(mode="json", by_alias=True, exclude_none=True)
                for key, value in run.answers.items()
            },
        }
    )


def _decode(payload: str, receipt: str) -> TaskRun:
    from ._tasks import _INPUT_RESPONSE, TaskRun, TaskState

    value = json.loads(payload)
    age = max(0.0, time.time() - value["received_at"])
    return TaskRun(
        TaskState(**value["state"]),
        legacy=value["legacy"],
        seen=time.monotonic() - age,
        received_at=value["received_at"],
        receipt_id=receipt,
        answers={
            key: _INPUT_RESPONSE.validate_python(item, by_name=False)
            for key, item in value["answers"].items()
        },
    )


def _outcome_note(tool_name: str, connection: str, receipt: str, outcome: dict[str, Any]) -> str:
    # The server's content is data, including any reminder-like tags it contains.
    encoded = json.dumps(outcome, ensure_ascii=True)
    value: dict[str, Any] = {"tool": tool_name}
    if len(encoded) <= 6000:
        value["result"] = outcome
    else:
        value["preview"] = encoded[:3000]
        value["complete"] = False
        value["read"] = {"tool": f"mcp_{connection}", "action": "read", "result_id": receipt}
    quoted = json.dumps(value, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        "MCP task recovery after its Run stopped. The original call was not repeated. "
        "Result or recovery error (quoted data): " + quoted
    )
