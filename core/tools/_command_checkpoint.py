"""Command-owned checkpoints and restart delivery over canonical Session storage.

A checkpoint holds a background command's state with its start time. After an
unexpected stop of vBot, ``recover`` rebuilds each command that was still
running from its raw log file: the output it wrote until then, and how long it
ran at least (from its start to the last write of that file or its last saved
state). Its result is delivered as usual; nothing is run again.

A checkpoint also keeps the Permission Boundary of the command's Session as a
ceiling (``permission_ceiling``, ``None`` for full access), so a delivery that
recovery formats names only paths that Session may read.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from core.permissions import (
    BoundaryContext,
    PermissionBoundary,
    PermissionCeiling,
    normalize_permission_rules,
)
from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.sessions import SessionAddress, SessionNotFoundError
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp

from ._command_output import LogSnapshot
from ._command_types import (
    CommandReport,
    CommandTranscript,
    CompletionSink,
    LogLoss,
    NotRunReason,
    StopReason,
)

_LOGGER = get_logger("tools.command_manager")
# Reads the raw log file a previous server wrote; None when it cannot be read.
LogReader = Callable[[Path], Awaitable[LogSnapshot | None]]


class CommandCheckpointStore(Protocol):
    @property
    def data_dir(self) -> Path:
        """The data directory the store keeps its checkpoints in."""
        ...

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

    def generation_address(self, generation_id: str) -> SessionAddress | None: ...

    def append_notes_once(
        self,
        address: SessionAddress,
        delivery_id: str,
        notes: Sequence[str],
        *,
        generation_id: str,
    ) -> None: ...

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
        # When each command started, in seconds since the epoch.
        self._started_at: dict[str, float] = {}
        self._tail: asyncio.Task[None] | None = None
        self._write_errors: dict[str, Exception] = {}
        self._generations: dict[str, str] = {}
        # The stored ceiling of each command's Session boundary; None for full access.
        self._ceilings: dict[str, dict[str, object] | None] = {}
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

    def bind(self, command_id: str, generation_id: str, *, permissions: PermissionBoundary) -> None:
        """Bind a new command to its Session generation and the boundary it runs under."""
        self._bind(command_id, generation_id, permissions.to_ceiling_dict(source="agent"))

    def _bind(self, command_id: str, generation_id: str, ceiling: dict[str, object] | None) -> None:
        self._generations[command_id] = generation_id
        self._ceilings[command_id] = ceiling

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
        started_at = self._started_at.setdefault(
            report.command_id, time.time() - report.elapsed_seconds
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
            "started_at": format_canonical_timestamp(datetime.fromtimestamp(started_at, UTC)),
            "permission_ceiling": self._ceilings.get(report.command_id),
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
        self._ceilings.pop(command_id, None)
        self._retained_until.pop(command_id, None)
        self._started_at.pop(command_id, None)
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
                    target,
                    notice_id=f"command:{entry.report.command_id}",
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
        self,
        sink: CompletionSink | None,
        publish: Callable[[CommandReport], None],
        read_log: LogReader | None = None,
    ) -> None:
        """Restore the previous server's commands; *read_log* reads their raw log files."""
        from ._shell_results import format_command_delivery

        self._sink = sink
        rows = await self.load()
        # Only commands still running then miss output in their saved state.
        logs = await asyncio.gather(*(_read_unfinished_log(row, read_log) for row in rows))
        deliveries = []
        for row, log in zip(rows, logs, strict=True):
            state = row["checkpoint"]
            report = _report(row)
            ceiling = state.get("permission_ceiling")
            self._bind(report.command_id, row["generation_id"], ceiling)
            if (started_at := _started_at(state)) is not None:
                self._started_at[report.command_id] = started_at
            if (deadline := state.get("retained_until")) is not None:
                self._retained_until[report.command_id] = float(deadline)
                if report.finished and not state["pending"] and deadline <= time.time():
                    self.delete(report.command_id)
                    continue
            for resolve in self._transfers:
                report = replace(report, owner=resolve(report.owner))
            body = state.get("body")
            execution = state.get("execution_owner")
            report = self._interrupted(report, log)
            if body is None and state["pending"]:
                body = format_command_delivery(
                    report,
                    permissions=_delivery_boundary(
                        report.command_id, ceiling, report.workdir, self.store.data_dir
                    ),
                )
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

    def _interrupted(self, report: CommandReport, log: LogSnapshot | None) -> CommandReport:
        """The end of a command as recovery finds it; unchanged when it had finished."""
        if report.finished:
            return report
        if log is not None:
            report = replace(report, transcript=log.transcript)
        if report.ended:
            return replace(report, finished=True)
        # Never attach to a reused PID. The old process tree belongs to the
        # previous server; its exact exit and effects are unknown. It ran at
        # least until its last output or its last saved state.
        elapsed = report.elapsed_seconds
        started_at = self._started_at.get(report.command_id)
        if log is not None and started_at is not None:
            elapsed = max(elapsed, log.modified_at - started_at)
        return replace(
            report,
            ended=True,
            finished=True,
            exit_code=None,
            stop_reason="server_restart",
            elapsed_seconds=elapsed,
        )

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
            if entry.execution_owner is not None:
                # A restored execution epoch can never be admitted again. Keep
                # its result in the same canonical Session without starting a
                # Run or reviving the Extension's old group.
                await self._persist_owned_result(entry)
                return
            target = entry.report.owner
            entry.submitted_to = target
            try:
                await asyncio.shield(
                    sink.submit_completion(
                        target,
                        notice_id=f"command:{entry.report.command_id}",
                        origin_run_id=entry.origin_run_id,
                        body=entry.body,
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

    async def _persist_owned_result(self, entry: _Recovered) -> None:
        assert entry.body is not None
        generation_id = self._generations[entry.report.command_id]
        while entry.pending:
            target = await self.store.run_async(self.store.generation_address, generation_id)
            if target is None:
                # Its Session was deleted or archived; never create a target.
                entry.pending = False
                self._save(entry)
                return
            if not entry.pending:
                return
            try:
                await self.store.run_async(
                    self.store.append_notes_once,
                    target,
                    f"command:{entry.report.command_id}",
                    [entry.body],
                    generation_id=generation_id,
                )
            except SessionNotFoundError:
                # A move between resolution and commit changed only the address.
                # Resolve the same generation again; deletion ends it above.
                continue
            except Exception:
                _LOGGER.warning(
                    "Persisting recovered owned command failed (command=%s)",
                    entry.report.command_id,
                    exc_info=True,
                )
                return
            entry.pending = False
            self._save(entry)
            return


async def _read_unfinished_log(
    row: dict[str, Any], read_log: LogReader | None
) -> LogSnapshot | None:
    """The raw log of a command that was still running, unless its saved state
    holds everything it wrote; None otherwise."""
    state = row["checkpoint"]
    path = state.get("log_path")
    if read_log is None or state["finished"] or not path or state.get("log_loss") is not None:
        # Writing a log that failed kept less than the saved transcript holds.
        return None
    return await read_log(Path(path))


def _delivery_boundary(
    command_id: str, ceiling: object, workdir: Path, data_dir: Path
) -> PermissionBoundary:
    """The boundary of the command's Session, rebuilt from its stored *ceiling*.

    Only the ceiling's layers, the Agent's own still tagged ``agent``, with the
    protected locations of a ceiling-only boundary, as a Cron watch check has
    them: the Session's Workspace, Scratch Area and Skill folders are no
    readable exceptions in the data directory, and its Agent's configuration
    file is not named. A ceiling that cannot be read permits reading only
    vBot's artifact folders, so the hint names no other path.
    """
    if ceiling is None:
        return PermissionBoundary.UNRESTRICTED
    from core.chat._run_permissions import artifact_dirs, installation_dirs

    context = BoundaryContext(
        workdir=workdir,
        home=Path.home(),
        data_dir=data_dir,
        install_dirs=installation_dirs(),
        readable_dirs=artifact_dirs(data_dir),
    )
    try:
        stored = PermissionCeiling.from_ceiling_dict(ceiling)
    except ValueError:
        _LOGGER.warning(
            "Unreadable permission rules of a recovered command (command=%s)", command_id
        )
        rules = normalize_permission_rules(
            {"read": "work_area", "write": "scratch", "deny": ["Read({workdir})"]}
        )
        return PermissionBoundary.build(rules, context)
    return PermissionBoundary.build(None, context, ceilings=(stored,))


def _started_at(state: dict[str, Any]) -> float | None:
    """When the command started, in seconds since the epoch; None for a checkpoint
    saved before it was recorded."""
    value = state.get("started_at")
    if not isinstance(value, str):
        return None
    try:
        return parse_timestamp(value).timestamp()
    except ValueError:
        return None


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
