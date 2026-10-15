"""Command-owned checkpoints and restart delivery over canonical Session storage."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.sessions import SessionAddress
from core.utils.logging import get_logger

from ._command_types import (
    CommandReport,
    CommandTranscript,
    CompletionSink,
    LogLoss,
    NotRunReason,
    StopReason,
)

_LOGGER = get_logger("tools.command_manager")


class CommandCheckpointStore(Protocol):
    def background_command_put(
        self,
        address: SessionAddress,
        command_id: str,
        origin_run_id: str,
        checkpoint: dict[str, Any],
        *,
        generation_id: str,
    ) -> None: ...

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]: ...

    def background_command_read(self) -> list[dict[str, Any]]: ...

    def background_command_delete(self, command_id: str) -> None: ...

    def background_command_delivered(self, command_id: str, body: str) -> bool: ...

    async def run_async[Result](
        self, function: Callable[..., Result], *args: Any, **kwargs: Any
    ) -> Result: ...


@dataclass
class _Recovered:
    report: CommandReport
    origin_run_id: str
    body: str | None
    execution_owner: RunExecutionOwner | None
    pending: bool
    submitted_to: SessionAddress | None = None


class CommandCheckpoints:
    def __init__(self, store: CommandCheckpointStore, *, retention_seconds: float) -> None:
        self.store = store
        self._retention_seconds = retention_seconds
        self._retained_until: dict[str, float] = {}
        self._tail: asyncio.Task[None] | None = None
        self._write_errors: dict[str, Exception] = {}
        self._generations: dict[str, str] = {}
        self._loading: asyncio.Task[list[dict[str, Any]]] | None = None
        self._recovery_loaded = False
        self.pending_ids: set[str] = set()
        self._recovered: dict[str, _Recovered] = {}
        self._sink: CompletionSink | None = None
        self._withdrawals: list[Callable[[_Recovered], bool]] = []
        self._transfers: list[Callable[[SessionAddress], SessionAddress]] = []

    async def load(self) -> list[dict[str, Any]]:
        if self._loading is None:
            self._loading = asyncio.create_task(
                self.store.run_async(self.store.background_command_read), name="command-load"
            )
        return await asyncio.shield(self._loading)

    async def capture_generation(self, owner: SessionAddress) -> str | None:
        versions = await self.store.run_async(self.store.list_history_versions, [owner])
        version = versions.get(owner)
        return version[0] if version is not None else None

    def bind(self, command_id: str, generation_id: str) -> None:
        self._generations[command_id] = generation_id

    def remaining_retention(self, command_id: str) -> float:
        return self._retained_until[command_id] - time.time()

    def put(
        self,
        report: CommandReport,
        origin_run_id: str,
        *,
        pending: bool,
        body: str | None,
        execution_owner: RunExecutionOwner | None,
    ) -> None:
        if pending:
            self.pending_ids.add(report.command_id)
        else:
            self.pending_ids.discard(report.command_id)
        if report.finished:
            self._retained_until.setdefault(
                report.command_id, time.time() + self._retention_seconds
            )
        checkpoint = {
            "command": report.command,
            "description": report.description,
            "workdir": str(report.workdir),
            "pid": report.pid,
            "ended": report.ended,
            "finished": report.finished,
            "exit_code": report.exit_code,
            "stop_reason": report.stop_reason,
            "not_run": report.not_run,
            "transcript": asdict(report.transcript),
            "log_path": str(report.log_path) if report.log_path is not None else None,
            "failed_programs": list(report.failed_programs),
            "log_loss": (
                {"kept_bytes": report.log_loss.kept_bytes, "kept_lines": report.log_loss.kept_lines}
                if report.log_loss is not None
                else None
            ),
            "elapsed_seconds": report.elapsed_seconds,
            "pending": pending,
            "body": body,
            "execution_owner": asdict(execution_owner) if execution_owner is not None else None,
            "retained_until": self._retained_until.get(report.command_id),
        }
        self._queue(
            report.command_id,
            self.store.background_command_put,
            report.owner,
            report.command_id,
            origin_run_id,
            checkpoint,
            generation_id=self._generations[report.command_id],
        )

    def delete(self, command_id: str) -> None:
        self._recovered.pop(command_id, None)
        self._generations.pop(command_id, None)
        self._retained_until.pop(command_id, None)
        self.pending_ids.discard(command_id)
        self._queue(command_id, self.store.background_command_delete, command_id)

    def _queue(
        self, command_id: str, function: Callable[..., None], *args: Any, **kwargs: Any
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            function(*args, **kwargs)
            return
        previous = self._tail

        async def write() -> None:
            if previous is not None:
                # A later full checkpoint can recover a failed earlier write.
                await asyncio.gather(previous, return_exceptions=True)
            try:
                await self.store.run_async(function, *args, **kwargs)
            except Exception as error:
                self._write_errors[command_id] = error
                raise
            else:
                self._write_errors.pop(command_id, None)

        self._tail = asyncio.create_task(write(), name="command-checkpoint")
        self._tail.add_done_callback(self._done)

    @staticmethod
    def _done(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.error("Saving command recovery state failed", exc_info=error)

    async def flush(self) -> None:
        while (tail := self._tail) is not None:
            await asyncio.gather(asyncio.shield(tail), return_exceptions=True)
            if self._tail is tail:
                break
        # A successful checkpoint for another command must not hide an earlier
        # failure. Only replacing that command's full checkpoint repairs it.
        if self._write_errors:
            raise next(iter(self._write_errors.values()))

    def withdraw(self, matches: Callable[[SessionAddress], bool]) -> None:
        self._withdraw(lambda entry: matches(entry.report.owner))

    def withdraw_execution(self, extension: str, group_id: str, epoch: str) -> None:
        self._withdraw(
            lambda entry: (
                entry.execution_owner is not None
                and (
                    entry.execution_owner.extension,
                    entry.execution_owner.group_id,
                    entry.execution_owner.epoch,
                )
                == (extension, group_id, epoch)
            )
        )

    def acknowledge(self, command_id: str) -> None:
        if (entry := self._recovered.get(command_id)) is not None:
            self._withdraw_entry(entry)

    def _withdraw(self, matches: Callable[[_Recovered], bool]) -> None:
        if not self._recovery_loaded:
            self._withdrawals.append(matches)
        for entry in self._recovered.values():
            if matches(entry):
                self._withdraw_entry(entry)

    def _withdraw_entry(self, entry: _Recovered) -> None:
        if entry.pending:
            entry.pending = False
            self._save(entry)
            self._cancel_submission(entry)

    def transfer(self, source: SessionAddress, target: SessionAddress) -> int:
        return self._transfer(lambda owner: target if owner == source else owner)

    def transfer_agent(self, agent_id: str, new_agent_id: str) -> int:
        return self._transfer(
            lambda owner: (
                replace(owner, agent_id=new_agent_id)
                if owner.project_id is None and owner.agent_id == agent_id
                else owner
            )
        )

    def _transfer(self, resolve: Callable[[SessionAddress], SessionAddress]) -> int:
        if not self._recovery_loaded:
            self._transfers.append(resolve)
        moved = 0
        for entry in self._recovered.values():
            target = resolve(entry.report.owner)
            if entry.report.owner == target:
                continue
            entry.report = replace(entry.report, owner=target)
            self._save(entry)
            self._cancel_submission(entry)
            moved += 1
        return moved

    def _cancel_submission(self, entry: _Recovered) -> None:
        if self._sink is not None and entry.submitted_to is not None:
            target = entry.submitted_to
            try:
                self._sink.cancel_completion(
                    target.agent_id,
                    target.session_id,
                    notice_id=f"command:{entry.report.command_id}",
                    project_id=target.project_id,
                )
            except Exception:
                _LOGGER.warning(
                    "Could not withdraw recovered command result (command=%s)",
                    entry.report.command_id,
                    exc_info=True,
                )

    def _save(self, entry: _Recovered) -> None:
        self.put(
            entry.report,
            entry.origin_run_id,
            pending=entry.pending,
            body=entry.body,
            execution_owner=entry.execution_owner,
        )

    async def recover(
        self, sink: CompletionSink | None, publish: Callable[[CommandReport], None]
    ) -> None:
        from ._shell_results import format_command_delivery

        self._sink = sink
        rows = await self.load()
        deliveries = []
        for row in rows:
            state = row["checkpoint"]
            report = _report(row)
            self.bind(report.command_id, row["generation_id"])
            if (deadline := state.get("retained_until")) is not None:
                self._retained_until[report.command_id] = float(deadline)
                if report.finished and not state["pending"] and deadline <= time.time():
                    self.delete(report.command_id)
                    continue
            for resolve in self._transfers:
                report = replace(report, owner=resolve(report.owner))
            body = state.get("body")
            execution = state.get("execution_owner")
            if not report.finished:
                # Never attach to a reused PID. The old process tree belongs to
                # the previous server; its exact exit and effects are unknown.
                report = (
                    replace(report, finished=True)
                    if report.ended
                    else replace(
                        report,
                        ended=True,
                        finished=True,
                        exit_code=None,
                        stop_reason="server_restart",
                    )
                )
            if body is None and state["pending"]:
                body = format_command_delivery(report)
            entry = _Recovered(
                report,
                row["origin_run_id"],
                body,
                RunExecutionOwner(**execution) if execution is not None else None,
                bool(state["pending"]),
            )
            if any(matches(entry) for matches in self._withdrawals):
                entry.pending = False
            self._recovered[report.command_id] = entry
            self._save(entry)
            publish(report)
            if entry.pending and entry.body is not None and sink is not None:
                deliveries.append(entry)
        self._recovery_loaded = True
        self._withdrawals.clear()
        self._transfers.clear()
        # load() remains a completed startup barrier for new launches, without
        # retaining a second copy of every recovered output past its expiry.
        rows.clear()
        # Publish every status and persist each recovered end before awaiting
        # any Session's delivery; an occupied Session cannot hide another one.
        await self.flush()
        if deliveries and sink is not None:
            await asyncio.gather(*(self._deliver(entry, sink) for entry in deliveries))
        await self.flush()

    async def _deliver(self, entry: _Recovered, sink: CompletionSink) -> None:
        assert entry.body is not None
        while entry.pending:
            if await self.store.run_async(
                self.store.background_command_delivered, entry.report.command_id, entry.body
            ):
                entry.pending = False
                self._save(entry)
                return
            if not entry.pending:
                return
            target = entry.report.owner
            entry.submitted_to = target
            try:
                await asyncio.shield(
                    sink.submit_completion(
                        target.agent_id,
                        target.session_id,
                        notice_id=f"command:{entry.report.command_id}",
                        origin_run_id=entry.origin_run_id,
                        body=entry.body,
                        project_id=target.project_id,
                        execution_owner=entry.execution_owner,
                        wake=True,
                    )
                )
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                # The sink withdrew an unclaimed result for Stop all or a
                # Session move. A move retries at the new canonical address.
                if entry.pending and entry.report.owner != target:
                    continue
                return
            except RunAdmissionBlockedError:
                entry.pending = False
                self._save(entry)
                return
            except Exception:
                if entry.pending and entry.report.owner != target:
                    continue
                _LOGGER.warning(
                    "Recovering command delivery failed (command=%s)",
                    entry.report.command_id,
                    exc_info=True,
                )
                return
            else:
                entry.pending = False
                self._save(entry)
                return


def _report(row: dict[str, Any]) -> CommandReport:
    state = row["checkpoint"]
    transcript = state["transcript"]
    return CommandReport(
        command_id=row["command_id"],
        owner=row["owner"],
        pid=int(state["pid"]),
        command=state["command"],
        description=state["description"],
        workdir=Path(state["workdir"]),
        ended=bool(state["ended"]),
        finished=bool(state["finished"]),
        exit_code=state["exit_code"],
        stop_reason=cast(StopReason | None, state["stop_reason"]),
        transcript=CommandTranscript(
            tuple(transcript["head"]),
            tuple(transcript["tail"]),
            int(transcript["total_lines"]),
            int(transcript["head_cut"]),
            int(transcript["tail_cut"]),
        ),
        log_path=Path(state["log_path"]) if state["log_path"] else None,
        failed_programs=tuple(state["failed_programs"]),
        still_running=(),
        background=True,
        elapsed_seconds=float(state["elapsed_seconds"]),
        not_run=cast(NotRunReason | None, state.get("not_run")),
        log_loss=(
            LogLoss(
                RuntimeError("output logging failed before restart"),
                int(state["log_loss"]["kept_bytes"]),
                int(state["log_loss"]["kept_lines"]),
            )
            if state.get("log_loss") is not None
            else None
        ),
    )
