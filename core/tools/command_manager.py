"""The shell's commands: one-shot processes that can outlive the call that started them.

``CommandManager`` owns every command the shell Tool runs: its launch on pipes
(no input, no terminal; ``_command_launch``), its clean output with a live log
file (``_command_output``), the decision when it ended, its stop, the background
registry with status events, the delivery of a background result, and the
lifecycle scopes it belongs to (``_command_record``). Agent-facing wording
belongs to the Tool, which formats ``CommandReport``.

A command has no time limit: it runs until it ends or something stops it. How
long a call waits for it is the caller's choice (``wait``).

A command starts in the foreground: unlisted, without status events, its
output published through ``on_output`` while the call waits (``wait``). The
call either sees it end, or moves it to the background (``to_background``),
after which it is listed (``commands``, ``status``), raises change callbacks,
and - with ``deliver`` - submits its result through ``TriggerService`` when it
ends: once, also to a Sub-Agent Session, starting a Run when the Session is
idle (notice id ``command:<command_id>``).

Stops (``stop``) name a reason; the first reason wins:

- ``agent``: the Agent of the command's Session stopped it with the
  ``background_tasks`` Tool; nothing more is delivered, since the stop's
  result shows the outcome.
- ``parent``: the Agent of an ancestor Session (a Parent Agent of the Sub-Agent
  that started it) stopped it; the result is delivered to the command's own
  Session, whose Agent did not see the stop.
- ``user``: the user stopped this command; a background result is delivered.
- ``run_cancelled``: the Run whose call waited for the command was cancelled
  (``cancel_run`` stops only foreground commands).
- ``shutdown``: vBot stops (``shutdown``); a background result is delivered.
- ``scope``: Stop all, ``subagent cancel``, removal of the Session, Agent or
  Project, or the close of an Extension execution group; nothing is delivered.

A stop of a command that already ended stops the processes it left running;
its outcome stays as it ended. When something outside vBot kills the command,
for example the Agent with an ordinary shell command against
``CommandReport.pid`` (``taskkill /PID <pid> /T /F``, ``kill -- -<pid>``) or
against the processes in ``still_running``, it ends like any other command, with
the exit code it got, and delivers its result.

A background command stays listed while it runs, while a process it left
running still runs, and for ``COMMAND_RETENTION_SECONDS`` after it ended;
then it is forgotten.

Calls that name a command find it by its id alone; every report names the
Session it belongs to (``CommandReport.owner``), and the caller decides which
Sessions it may act for.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from core.runs import RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._command_launch import launch_pipe_process
from ._command_output import CommandOutput
from ._command_record import BlockingRunner, CommandRecord, CommandServices
from ._command_types import (
    COMMAND_DRAIN_SECONDS,
    COMMAND_HEAD_LINES,
    COMMAND_LEFTOVER_QUIET_SECONDS,
    COMMAND_LOG_REFRESH_SECONDS,
    COMMAND_MAX_LIVE,
    COMMAND_MEMORY_LINE_MAX_CHARS,
    COMMAND_PROGRESS_LINES,
    COMMAND_PROGRESS_SECONDS,
    COMMAND_RECENT_LINES,
    COMMAND_RETENTION_SECONDS,
    COMMAND_STOP_GRACE_SECONDS,
    COMMAND_SWEEP_INTERVAL_SECONDS,
    COMMAND_TAIL_LINES,
    COMMAND_TAIL_SPARE_LINES,
    COMMAND_TEMPORARY_CATEGORY,
    CommandCapacityError,
    CommandChangedCallback,
    CommandHandle,
    CommandLauncher,
    CommandLaunchError,
    CommandManagerError,
    CommandNotFoundError,
    CommandNotStartedError,
    CommandOwner,
    CommandProcess,
    CommandReport,
    CommandReportFormatter,
    CommandStatus,
    CommandStopError,
    CommandTranscript,
    CommandWaitOutcome,
    CompletionSink,
    OutputCallback,
    StopReason,
)
from .process_tree import ProcessTreeTracker, track_process_tree

_LOGGER = get_logger("tools.command_manager")


@dataclass(frozen=True, slots=True)
class _PendingLaunch:
    owner: CommandOwner
    origin_run_id: str
    execution_owner: RunExecutionOwner | None


class CommandManager:
    """Own the shell's command processes from launch until they are forgotten.

    *trigger_service* receives background results (``TriggerService``); without
    it nothing is delivered. *temporary_files* holds the live log files; without
    it commands have none. The other parameters are seams for tests: the process
    *launcher* and *process_tracker*, the clock (*monotonic*, *sleep*) and
    *run_blocking*, which runs process queries and kills off the Event Loop.
    """

    def __init__(
        self,
        trigger_service: CompletionSink | None = None,
        *,
        temporary_files: TemporaryFileManager | None = None,
        process_tracker: ProcessTreeTracker = track_process_tree,
        launcher: CommandLauncher = launch_pipe_process,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        run_blocking: BlockingRunner = asyncio.to_thread,
        max_live: int = COMMAND_MAX_LIVE,
        retention_seconds: float = COMMAND_RETENTION_SECONDS,
        sweep_interval_seconds: float = COMMAND_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if max_live < 1:
            raise ValueError("The command limit must be positive")
        if retention_seconds <= 0 or sweep_interval_seconds <= 0:
            raise ValueError("Command retention and sweep interval must be positive")
        self._temporary_files = temporary_files
        self._process_tracker = process_tracker
        self._launcher = launcher
        self._monotonic = monotonic
        self._sleep = sleep
        self._run_blocking = run_blocking
        self._max_live = max_live
        self._retention_seconds = retention_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._services = CommandServices(
            trigger_service=trigger_service,
            monotonic=monotonic,
            sleep=sleep,
            run_blocking=run_blocking,
            changed=self._notify,
        )
        self._records: dict[str, CommandRecord] = {}
        self._pending: dict[asyncio.Task[CommandRecord], _PendingLaunch] = {}
        self._callbacks: list[CommandChangedCallback] = []
        # Runs being cancelled: their foreground commands stop and no new one starts.
        self._cancelled_runs: set[str] = set()
        self._closed_execution_groups: set[tuple[str, str, str]] = set()
        self._closed = False
        self._sweeper: asyncio.Task[None] | None = None

    # Manager lifecycle

    def start(self) -> None:
        """Start forgetting finished commands after their retention (needs an Event Loop)."""
        if self._sweeper is not None and not self._sweeper.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="command-manager-sweep")

    def add_changed_callback(self, callback: CommandChangedCallback) -> Callable[[], None]:
        """Call *callback* with a background command's id when its status may have changed.

        That is when it moves to the background, ends, finishes, and when it is
        forgotten (``status`` is then None). Returns the unsubscribe function.
        """
        self._callbacks.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

        return remove

    async def shutdown(self) -> None:
        """Stop every command before vBot shuts down (reason ``shutdown``).

        Runs before the delivery service closes: a background command's
        stopped result is submitted. New launches are refused from now on.
        Raises ``CommandStopError`` when a kill was not confirmed, after every
        command was stopped or tried.
        """
        self._closed = True
        await _settle(list(self._pending))
        await self._stop_records(
            [record for record in self._records.values() if not record.finished], "shutdown"
        )
        # Let the deliveries the stops submitted hand their results over.
        await asyncio.sleep(0)

    def close(self) -> None:
        """Kill every command's processes synchronously and release them (Runtime stop).

        Nothing is delivered any more: a result not yet handed over is withdrawn.
        Raises ``CommandManagerError`` naming the commands whose kill was not
        confirmed.
        """
        self._closed = True
        if self._sweeper is not None:
            with contextlib.suppress(RuntimeError):
                self._sweeper.cancel()
        failures = [
            record.command_id for record in list(self._records.values()) if not record.kill_now()
        ]
        if failures:
            raise CommandManagerError(
                "Could not stop the processes of commands: " + ", ".join(failures)
            )

    async def aclose(self) -> None:
        """``close``, then await every task of the manager and its commands."""
        failure: CommandManagerError | None = None
        try:
            self.close()
        except CommandManagerError as error:
            failure = error
        tasks: list[asyncio.Task[object]] = [*self._pending]
        if self._sweeper is not None:
            tasks.append(self._sweeper)
        for record in self._records.values():
            tasks.extend(record.pending_tasks())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if failure is not None:
            raise failure

    # Commands

    async def launch(
        self,
        owner: CommandOwner,
        argv: Sequence[str],
        *,
        command: str,
        description: str | None,
        cwd: Path,
        env: Mapping[str, str],
        origin_run_id: str,
        formatter: CommandReportFormatter,
        execution_owner: RunExecutionOwner | None = None,
        on_output: OutputCallback | None = None,
    ) -> CommandHandle:
        """Start one command in the foreground and return its handle.

        *argv* runs exactly, in *cwd*, with exactly *env* (the complete
        environment); *command* and *description* are what the Agent asked for.
        *formatter* builds the body of the result delivery from the final
        report. *on_output* receives the newest
        ``COMMAND_PROGRESS_LINES`` clean lines at most every
        ``COMMAND_PROGRESS_SECONDS`` while output arrives and the command is in
        the foreground.

        Raises ``CommandNotStartedError`` when the Run *origin_run_id* was
        cancelled, the execution group closed or vBot shuts down,
        ``CommandCapacityError`` when ``COMMAND_MAX_LIVE`` commands run, and
        ``CommandLaunchError`` when the process cannot be started or tracked.
        A cancelled caller leaves no command behind.
        """
        if not owner.agent_id or not owner.session_id:
            raise ValueError("A command needs the Agent and Session it belongs to")
        if not argv or any(not isinstance(token, str) for token in argv) or not argv[0]:
            raise ValueError("A command needs a program and string arguments")
        self._admit(origin_run_id, execution_owner)
        task = asyncio.create_task(
            self._launch_admitted(
                owner,
                list(argv),
                command=command,
                description=description,
                cwd=cwd,
                env=dict(env),
                origin_run_id=origin_run_id,
                formatter=formatter,
                execution_owner=execution_owner,
                on_output=on_output,
            )
        )
        self._pending[task] = _PendingLaunch(owner, origin_run_id, execution_owner)
        try:
            record = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Process creation cannot be cancelled once it started; keep its
            # result owned until it failed or was stopped.
            cleanup = asyncio.create_task(self._discard_cancelled_launch(task))
            while not cleanup.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            cleanup.result()
            raise
        finally:
            self._pending.pop(task, None)
        return CommandHandle(record.command_id, record.pid)

    async def wait(self, command_id: str, *, seconds: float | None) -> CommandWaitOutcome:
        """Wait until the command ended (``ended``) or *seconds* passed (``deadline``).

        None waits without a limit. Cancelling the wait changes nothing.
        """
        return await self._get(command_id).wait(seconds)

    def to_background(self, command_id: str, *, deliver: bool) -> CommandReport:
        """List the command; with *deliver*, its result is delivered when it ends.

        The returned report tells whether it had ended already: then nothing is
        delivered and the caller reports the result. A command that ended with
        processes still running is listed with ``deliver=False``, so the user
        can see and stop what remains.
        """
        record = self._get(command_id)
        record.to_background(deliver=deliver)
        return record.report()

    async def stop(self, command_id: str, reason: StopReason) -> CommandReport:
        """Stop the command and every process it started; return its final report.

        Windows kills the command's Job; POSIX sends SIGINT to its process
        group and kills what runs after ``COMMAND_STOP_GRACE_SECONDS``. The
        first reason wins. A command that ended keeps its outcome and loses
        the processes it left running; a finished command is returned
        unchanged. Raises ``CommandStopError`` (naming the survivors) when the
        kill is not confirmed; the command then stays live and a later stop
        retries.
        """
        record = self._get(command_id)
        await record.stop(reason)
        return record.report()

    def acknowledge(self, command_id: str) -> None:
        """A durable Tool result showed the command's end: deliver nothing more.

        Call it once that result is persisted, and only when it showed the end.
        A delivery ``TriggerService`` has not handed over yet is withdrawn; one
        it handed over stays. An unknown command is ignored.
        """
        self._expire()
        record = self._records.get(command_id)
        if record is not None:
            record.acknowledge()

    def report(self, command_id: str) -> CommandReport:
        """The command's report as of now."""
        return self._get(command_id).report()

    def latest_output(self, command_id: str, lines: int) -> str:
        """The newest *lines* clean output lines (at most ``COMMAND_RECENT_LINES``),
        the line still being written included."""
        return self._get(command_id).latest(lines)

    def status(self, command_id: str) -> CommandStatus | None:
        """A background command's status; None for an unknown or foreground command."""
        self._expire()
        record = self._records.get(command_id)
        if record is None or not record.background:
            return None
        return record.status

    def commands(self, owners: Collection[CommandOwner]) -> list[CommandReport]:
        """The background commands of the *owners* Sessions, newest first.

        Running ones, ended ones whose processes still run, and ended ones
        until their retention ends; each report names its Session. Foreground
        commands, still inside their call, are not listed.
        """
        self._expire()
        sessions = set(owners)
        records = sorted(
            (
                record
                for record in self._records.values()
                if record.background and record.owner in sessions
            ),
            key=lambda record: record.started_at,
            reverse=True,
        )
        return [record.report() for record in records]

    async def wait_finished(self, command_id: str) -> None:
        """Wait until no process of the command runs any longer."""
        await self._get(command_id).wait_finished()

    # Runs

    async def cancel_run(self, run_id: str) -> None:
        """Stop the foreground commands of a cancelled Run and refuse new ones for it.

        Commands already in the background keep running and deliver as usual.
        """
        if not run_id:
            return
        self._cancelled_runs.add(run_id)
        await _settle(
            [task for task, launch in self._pending.items() if launch.origin_run_id == run_id]
        )
        await self._stop_records(
            [
                record
                for record in self._records.values()
                if record.origin_run_id == run_id and not record.background and not record.finished
            ],
            "run_cancelled",
        )

    def release_run(self, run_id: str) -> None:
        """Forget a settled Run's cancellation marker; no command for it can start any more."""
        self._cancelled_runs.discard(run_id)

    # Scopes

    async def close_scope(self, owner: CommandOwner) -> int:
        """Stop the Session's commands without delivery (reason ``scope``).

        For Stop all, ``subagent cancel`` and the Session's removal. A result
        not yet delivered is withdrawn. Returns how many unfinished commands
        were stopped.
        """
        return await self._close_matching(lambda scope: scope == owner)

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> int:
        """``close_scope`` for every Session of an Agent in one Project scope."""
        return await self._close_matching(
            lambda scope: scope.agent_id == agent_id and scope.project_id == project_id
        )

    async def close_project_scope(self, project_id: str) -> int:
        """``close_scope`` for every Session of a Project."""
        return await self._close_matching(lambda scope: scope.project_id == project_id)

    def transfer_scope(self, source: CommandOwner, target: CommandOwner) -> int:
        """Move commands to another Session after a Session move; returns how many moved.

        A result not yet submitted goes to the new Session.
        """
        return sum(record.transfer(source, target) for record in self._records.values())

    def transfer_agent_scope(self, agent_id: str, new_agent_id: str) -> int:
        """Move the commands of a renamed Identity Agent to its new id; returns how many."""
        moved = 0
        for record in self._records.values():
            owner = record.owner
            if owner.project_id is None and owner.agent_id == agent_id:
                moved += record.transfer(owner, replace(owner, agent_id=new_agent_id))
        return moved

    def has_execution_work(self, owner: RunExecutionOwner) -> bool:
        """Whether a command of the execution owner is launching or still running."""
        return any(
            launch.execution_owner == owner
            for task, launch in self._pending.items()
            if not task.done()
        ) or any(
            record.execution_owner == owner and not record.finished
            for record in self._records.values()
        )

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        """Stop the commands of an Extension execution group (reason ``scope``).

        Callers close a group only after every Run of the group settled. New
        launches for the group are refused until pending ones are drained and
        its commands stopped.
        """
        key = (extension, group_id, epoch)
        self._closed_execution_groups.add(key)
        try:
            await _settle(
                [
                    task
                    for task, launch in self._pending.items()
                    if launch.execution_owner is not None
                    and _group_key(launch.execution_owner) == key
                ]
            )
            await self._stop_records(
                [
                    record
                    for record in self._records.values()
                    if record.execution_owner is not None
                    and _group_key(record.execution_owner) == key
                    and not record.finished
                ],
                "scope",
            )
        finally:
            # Each epoch key is used once and its launches are drained above.
            self._closed_execution_groups.discard(key)

    # Internals

    def _admit(self, origin_run_id: str, execution_owner: RunExecutionOwner | None) -> None:
        if self._closed:
            raise CommandNotStartedError("vBot is shutting down")
        if origin_run_id in self._cancelled_runs:
            raise CommandNotStartedError("The Run that started this command was cancelled")
        if (
            execution_owner is not None
            and _group_key(execution_owner) in self._closed_execution_groups
        ):
            raise CommandNotStartedError("The execution group of this command closed")
        live = sum(1 for record in self._records.values() if not record.finished)
        live += sum(1 for task in self._pending if not task.done())
        if live >= self._max_live:
            raise CommandCapacityError(
                f"Running command limit reached ({self._max_live})", limit=self._max_live
            )

    async def _launch_admitted(
        self,
        owner: CommandOwner,
        argv: list[str],
        *,
        command: str,
        description: str | None,
        cwd: Path,
        env: dict[str, str],
        origin_run_id: str,
        formatter: CommandReportFormatter,
        execution_owner: RunExecutionOwner | None,
        on_output: OutputCallback | None,
    ) -> CommandRecord:
        lease: TemporaryFileLease | None = None
        log_file: BinaryIO | None = None
        process: CommandProcess | None = None
        try:
            if not await self._run_blocking(cwd.is_dir):
                raise CommandLaunchError(f"The workdir is not a directory: {model_path(cwd)}")
            if self._temporary_files is not None:
                lease, log_file = await self._run_blocking(_open_log, self._temporary_files)
            try:
                process = await self._launcher(argv, cwd, env, self._process_tracker)
            except Exception as error:
                raise CommandLaunchError(f"The command could not be started: {error}") from error
        except BaseException:
            if log_file is not None:
                with contextlib.suppress(OSError):
                    log_file.close()
            if lease is not None:
                lease.finish()
            raise
        command_id = new_id("cmd", claim=lambda candidate: candidate not in self._records)
        record = CommandRecord(
            command_id,
            owner=owner,
            process=process,
            command=command,
            description=description,
            workdir=cwd,
            origin_run_id=origin_run_id,
            execution_owner=execution_owner,
            formatter=formatter,
            on_output=on_output,
            output=CommandOutput(log_file),
            log_path=lease.path if lease is not None else None,
            log_lease=lease,
            services=self._services,
        )
        self._records[command_id] = record
        record.start()
        return record

    async def _discard_cancelled_launch(self, task: asyncio.Task[CommandRecord]) -> None:
        try:
            record = await task
        except Exception:
            return
        try:
            await record.stop("run_cancelled")
        except CommandStopError:
            _LOGGER.warning(
                "Could not stop command %s whose caller was cancelled",
                record.command_id,
                exc_info=True,
            )

    async def _close_matching(self, matches: Callable[[CommandOwner], bool]) -> int:
        await _settle([task for task, launch in self._pending.items() if matches(launch.owner)])
        matching = [record for record in self._records.values() if matches(record.owner)]
        running = [record for record in matching if not record.finished]
        for record in matching:
            if record.finished:
                record.withdraw_delivery()
        await self._stop_records(running, "scope")
        return len(running)

    async def _stop_records(self, records: list[CommandRecord], reason: StopReason) -> None:
        """Stop every record; raise the first failure once all were stopped or tried."""
        results = await asyncio.gather(
            *(record.stop(reason) for record in records), return_exceptions=True
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        for failure in failures[1:]:
            _LOGGER.warning("Stopping a command failed", exc_info=failure)
        if failures:
            raise failures[0]

    def _get(self, command_id: str) -> CommandRecord:
        self._expire()
        record = self._records.get(command_id)
        if record is None:
            raise CommandNotFoundError(f"No command {command_id} runs or ran recently here")
        return record

    def _expire(self) -> None:
        """Forget finished commands that ended longer than the retention ago."""
        now = self._monotonic()
        expired = [
            record
            for record in self._records.values()
            if record.finished
            and record.ended_at is not None
            and now - record.ended_at >= self._retention_seconds
        ]
        for record in expired:
            del self._records[record.command_id]
        for record in expired:
            if record.background:
                self._notify(record.command_id)

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await self._sleep(self._sweep_interval_seconds)
                self._expire()
        except asyncio.CancelledError:
            return

    def _notify(self, command_id: str) -> None:
        for callback in list(self._callbacks):
            try:
                callback(command_id)
            except Exception:
                _LOGGER.warning(
                    "A command change callback failed (command=%s)", command_id, exc_info=True
                )


def _open_log(temporary_files: TemporaryFileManager) -> tuple[TemporaryFileLease, BinaryIO]:
    """Create the command's private log file (blocking)."""
    lease = temporary_files.create(COMMAND_TEMPORARY_CATEGORY, ".log")
    try:
        if os.name != "nt":
            lease.path.chmod(0o600)
        return lease, lease.path.open("wb")
    except BaseException:
        lease.finish()
        raise


async def _settle(launches: list[asyncio.Task[CommandRecord]]) -> None:
    """Wait until the launches in progress started their commands or failed."""
    if launches:
        await asyncio.gather(*(asyncio.shield(task) for task in launches), return_exceptions=True)


def _group_key(owner: RunExecutionOwner) -> tuple[str, str, str]:
    return (owner.extension, owner.group_id, owner.epoch)


__all__ = [
    "COMMAND_DRAIN_SECONDS",
    "COMMAND_HEAD_LINES",
    "COMMAND_LEFTOVER_QUIET_SECONDS",
    "COMMAND_LOG_REFRESH_SECONDS",
    "COMMAND_MAX_LIVE",
    "COMMAND_MEMORY_LINE_MAX_CHARS",
    "COMMAND_PROGRESS_LINES",
    "COMMAND_PROGRESS_SECONDS",
    "COMMAND_RECENT_LINES",
    "COMMAND_RETENTION_SECONDS",
    "COMMAND_STOP_GRACE_SECONDS",
    "COMMAND_SWEEP_INTERVAL_SECONDS",
    "COMMAND_TAIL_LINES",
    "COMMAND_TAIL_SPARE_LINES",
    "COMMAND_TEMPORARY_CATEGORY",
    "CommandCapacityError",
    "CommandChangedCallback",
    "CommandHandle",
    "CommandLaunchError",
    "CommandLauncher",
    "CommandManager",
    "CommandManagerError",
    "CommandNotFoundError",
    "CommandNotStartedError",
    "CommandOwner",
    "CommandProcess",
    "CommandReport",
    "CommandReportFormatter",
    "CommandStatus",
    "CommandStopError",
    "CommandTranscript",
    "CommandWaitOutcome",
    "CompletionSink",
    "OutputCallback",
    "StopReason",
]
