"""The shell's commands: one-shot processes that can outlive the call that started them.

``CommandManager`` owns every command the shell Tool runs: its launch on pipes
(no input, no terminal; ``_command_launch``, whose launcher keeps a warm shell
ready on Windows), its output - every byte appended to a live log file, and
a clean transcript (``_command_output``, cleaned on subinterpreter workers off
the Event Loop) - the decision when it ended, its stop, the background
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
- ``scope``: Stop all, a ``background_tasks`` stop of a Sub-Agent, removal of
  the Session, Agent or Project, or the close of an Extension execution group;
  nothing is delivered.

A stop of a command that already ended stops the processes it left running;
its outcome stays as it ended. When something outside vBot kills the command,
for example the Agent with an ordinary shell command against
``CommandReport.pid`` (``taskkill /PID <pid> /T /F``, ``kill -- -<pid>``) or
against the processes in ``still_running``, it ends like any other command, with
the exit code it got, and delivers its result.

A background command stays listed while it runs, while a process it left
running still runs, and for ``COMMAND_RETENTION_SECONDS`` after its last
process ended; then it is forgotten.

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

from core.runs import RunExecutionOwner
from core.sessions import SessionAddress
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._command_checkpoint import CommandCheckpoints, CommandCheckpointStore
from ._command_launch import PipeLauncher
from ._command_output import CommandOutput, command_output_host
from ._command_record import BlockingRunner, CommandRecord, CommandServices
from ._command_types import (
    COMMAND_DRAIN_SECONDS,
    COMMAND_HEAD_CHARS,
    COMMAND_IDLE_WINDOW_SECONDS,
    COMMAND_LEFTOVER_QUIET_SECONDS,
    COMMAND_MAX_LIVE,
    COMMAND_PROGRESS_LINES,
    COMMAND_PROGRESS_SECONDS,
    COMMAND_RECENT_LINES,
    COMMAND_RETENTION_SECONDS,
    COMMAND_STOP_GRACE_SECONDS,
    COMMAND_SWEEP_INTERVAL_SECONDS,
    COMMAND_TAIL_CHARS,
    COMMAND_TEMPORARY_CATEGORY,
    CommandCapacityError,
    CommandChangedCallback,
    CommandCheckpointError,
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
    LogLoss,
    NotRunReason,
    OutputCallback,
    StopReason,
)
from ._interpreter_host import InterpreterHost
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
    *launcher* (by default a ``PipeLauncher``; the manager closes it with itself)
    and *process_tracker*, the clock (*monotonic*, *sleep*) and *run_blocking*,
    which runs process queries, kills and log file writes off the Event Loop.
    """

    def __init__(
        self,
        trigger_service: CompletionSink | None = None,
        *,
        temporary_files: TemporaryFileManager | None = None,
        process_tracker: ProcessTreeTracker = track_process_tree,
        launcher: CommandLauncher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        run_blocking: BlockingRunner = asyncio.to_thread,
        max_live: int = COMMAND_MAX_LIVE,
        retention_seconds: float = COMMAND_RETENTION_SECONDS,
        sweep_interval_seconds: float = COMMAND_SWEEP_INTERVAL_SECONDS,
        output_host: InterpreterHost | None = None,
        checkpoint_store: CommandCheckpointStore | None = None,
    ) -> None:
        """*output_host* cleans the commands' output; by default the manager
        owns one with subinterpreter workers and closes it with itself."""
        if max_live < 1:
            raise ValueError("The command limit must be positive")
        if retention_seconds <= 0 or sweep_interval_seconds <= 0:
            raise ValueError("Command retention and sweep interval must be positive")
        self._temporary_files = temporary_files
        self._process_tracker = process_tracker
        self._launcher = launcher if launcher is not None else PipeLauncher(sleep=sleep)
        self._monotonic = monotonic
        self._sleep = sleep
        self._run_blocking = run_blocking
        self._max_live = max_live
        self._retention_seconds = retention_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._owns_output_host = output_host is None
        self._output_host = output_host or command_output_host()
        self._checkpoints = (
            CommandCheckpoints(checkpoint_store, retention_seconds=retention_seconds)
            if checkpoint_store
            else None
        )
        self._recovery_task: asyncio.Task[None] | None = None
        self._recovered: dict[str, tuple[CommandReport, float]] = {}
        self._services = CommandServices(
            trigger_service=trigger_service,
            monotonic=monotonic,
            sleep=sleep,
            run_blocking=run_blocking,
            changed=self._notify,
            checkpoint=self._checkpoint,
            flush=self.flush,
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

    def start(self, *, recover: bool = True) -> None:
        """Start retention; normal startup also restores durable background work."""
        if self._sweeper is not None and not self._sweeper.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="command-manager-sweep")
        if recover and self._checkpoints is not None and self._recovery_task is None:
            self._recovery_task = asyncio.create_task(
                self._checkpoints.recover(self._services.trigger_service, self._recovered_report),
                name="command-recovery",
            )

    def _recovered_report(self, report: CommandReport) -> None:
        assert self._checkpoints is not None
        retained_at = (
            self._monotonic()
            + self._checkpoints.remaining_retention(report.command_id)
            - self._retention_seconds
        )
        self._recovered[report.command_id] = (report, retained_at)
        self._notify(report.command_id)

    async def flush(self) -> None:
        """Wait until every queued command checkpoint is durable."""
        if self._checkpoints is not None:
            await self._checkpoints.flush()

    async def wait_recovered(self) -> None:
        """Wait for startup recovery and its durable completion deliveries."""
        if self._recovery_task is not None:
            await asyncio.shield(self._recovery_task)

    def add_changed_callback(self, callback: CommandChangedCallback) -> Callable[[], None]:
        """Call *callback* with a background command's id when its status may have changed.

        That is when it moves to the background, ends, when a process it left
        running ends (``CommandReport.still_running`` shrank), when it
        finishes, and when it is forgotten (``status`` is then None). Returns
        the unsubscribe function.
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
        await self._close_launcher()
        await self._stop_records(
            [record for record in self._records.values() if not record.finished], "shutdown"
        )
        # Let the deliveries the stops submitted hand their results over.
        await self.flush()
        await asyncio.sleep(0)

    def close(self) -> None:
        """``aclose`` without an Event Loop: each kill blocks the calling thread.

        Raises ``CommandManagerError`` naming the commands whose kill was not
        confirmed; they are released all the same.
        """
        self._closed = True
        self._stop_sweeper()
        if self._recovery_task is not None:
            self._recovery_task.cancel()
        try:
            self._launcher.close()
        except Exception:
            _LOGGER.error("Closing the command launcher failed", exc_info=True)
        records = list(self._records.values())
        unconfirmed = [record for record in records if not record.kill_now()]
        if self._owns_output_host:
            self._output_host.close()
        _raise_unconfirmed(unconfirmed)

    async def aclose(self) -> None:
        """Kill every command's processes, release them and await every task (Runtime stop).

        The kills run off the Event Loop and in parallel; each may wait for
        the operating system to confirm it. Nothing is delivered any more: a
        result not yet handed over is withdrawn. A command whose kill was not
        confirmed is released all the same, with its surviving processes
        logged: Runtime stop does not wait for them. Raises
        ``CommandManagerError`` naming those commands.
        """
        self._closed = True
        self._stop_sweeper()
        # Launches in progress start their commands first, so their kill is not missed.
        await _settle(list(self._pending))
        await self._close_launcher()
        records = list(self._records.values())
        killed = await asyncio.gather(
            *(record.kill_and_release() for record in records), return_exceptions=True
        )
        for result in killed:
            if isinstance(result, BaseException):
                _LOGGER.error("Killing a command at shutdown failed", exc_info=result)
        tasks: list[asyncio.Task[object]] = [*self._pending]
        if self._sweeper is not None:
            tasks.append(self._sweeper)
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            tasks.append(self._recovery_task)
        for record in self._records.values():
            tasks.extend(record.pending_tasks())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush()
        if self._owns_output_host:
            # Waits for the output work already queued.
            await self._run_blocking(self._output_host.close)
        _raise_unconfirmed(
            [record for record, result in zip(records, killed, strict=True) if result is not True]
        )

    async def _close_launcher(self) -> None:
        try:
            await self._launcher.aclose()
        except Exception:
            _LOGGER.error("Closing the command launcher failed", exc_info=True)

    def _stop_sweeper(self) -> None:
        if self._sweeper is not None:
            # Without a running Event Loop, nothing is left to cancel.
            with contextlib.suppress(RuntimeError):
                self._sweeper.cancel()

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
        request: bytes | None = None,
        warm: bool = True,
        not_run_codes: Mapping[int, NotRunReason] | None = None,
    ) -> CommandHandle:
        """Start one command in the foreground and return its handle.

        *argv* runs exactly, in *cwd*, with exactly *env* (the complete
        environment); *command* and *description* are what the Agent asked for.
        *request*, when given, is what the shell reads from stdin before it runs
        the command (``CommandLauncher.launch``): *env* is then its start
        environment, and the request carries the rest. *warm* False marks an
        *env* that holds values of this call: no shell started ahead serves it.
        *not_run_codes* are exit codes by which the shell says that it ran
        nothing of the command, and why: the report names the reason
        (``CommandReport.not_run``) once the shell exited with one of them.
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
        if self._checkpoints is not None:
            # Snapshot the previous server's commands before any new command
            # can publish a checkpoint, even when launch races startup recovery.
            await self._checkpoints.load()
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
                request=request,
                warm=warm,
                not_run_codes=dict(not_run_codes or {}),
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

    async def wait(
        self, command_id: str, *, seconds: float | None, check_idle: bool = False
    ) -> CommandWaitOutcome:
        """Wait until the command ended (``ended``) or *seconds* passed.

        With *check_idle*, a wait that passed returns ``idle`` when the command's processes were
        quiet over its last ``COMMAND_IDLE_WINDOW_SECONDS`` (no output, no new
        process, at most ``IDLE_CPU_SECONDS`` of CPU time, as for processes left
        running), else ``deadline``; a shorter wait is followed whole. None
        waits without a limit. Cancelling the wait changes nothing.
        """
        return await self._get(command_id).wait(seconds, check_idle=check_idle)

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

    async def to_background_durably(self, command_id: str, *, deliver: bool) -> CommandReport:
        """Publish a background handoff only after its recovery state is durable."""
        report = self.to_background(command_id, deliver=deliver)
        try:
            await self.flush()
        except Exception as error:
            raise CommandCheckpointError(
                f"Recovery state for the started command {command_id} could not be saved"
            ) from error
        return report

    async def stop(self, command_id: str, reason: StopReason) -> CommandReport:
        """Stop the command and every process it started; return its final report.

        Windows kills the command's Job; POSIX sends SIGINT to its process
        group and kills what runs after ``COMMAND_STOP_GRACE_SECONDS``. The
        first reason wins. A command that ended keeps its outcome and loses
        the processes it left running; a finished command is returned
        unchanged. Raises ``CommandStopError`` (naming the survivors) when the
        kill is not confirmed; the command then stays live and a later stop
        retries. When the kill was sent (``kill_sent``), the stop keeps its
        reason and the end the kill causes reads ``stopped``. A stop that sent
        no kill - refused, or cancelled before its kill began - gives its
        reason back: unless the command ended meanwhile, it runs on as if not
        stopped, an end it reaches by itself reports its exit code, and a
        background result is delivered unless a ``scope`` stop withdrew it.
        """
        if command_id in self._recovered:
            return self.report(command_id)
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
        elif self._checkpoints is not None:
            self._checkpoints.acknowledge(command_id)

    def report(self, command_id: str) -> CommandReport:
        """The command's report as of now."""
        self._expire()
        if command_id in self._recovered:
            return self._recovered[command_id][0]
        return self._get(command_id).report()

    def latest_output(self, command_id: str, lines: int) -> str:
        """The newest *lines* clean output lines (at most ``COMMAND_RECENT_LINES``),
        the line still being written included."""
        if command_id in self._recovered:
            report = self.report(command_id)
            return "\n".join((*report.transcript.head, *report.transcript.tail)[-lines:])
        return self._get(command_id).latest(lines)

    def status(self, command_id: str) -> CommandStatus | None:
        """A background command's status; None for an unknown or foreground command."""
        self._expire()
        if command_id in self._recovered:
            return self._recovered[command_id][0].status
        record = self._records.get(command_id)
        if record is None or not record.background:
            return None
        return record.status

    def commands(self, owners: Collection[CommandOwner]) -> list[CommandReport]:
        """The background commands of the *owners* Sessions, newest first.

        Running ones, ended ones whose processes still run, and finished ones
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
        return [record.report() for record in records] + [
            report for report, _at in self._recovered.values() if report.owner in sessions
        ]

    def busy_addresses(self) -> set[SessionAddress]:
        """Sessions with a pending launch or any command process still running."""
        owners = {launch.owner for launch in self._pending.values()}
        owners.update(record.owner for record in self._records.values() if not record.finished)
        return {
            SessionAddress(owner.project_id, owner.agent_id, owner.session_id) for owner in owners
        }

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

        For Stop all, a ``background_tasks`` stop of a Sub-Agent and the
        Session's removal. A result not yet delivered is withdrawn. Returns how
        many unfinished commands were stopped. A kill the operating system does
        not confirm is logged, not raised: the lifecycle step goes on, and the
        command stays live and listed for a later stop.
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

    async def transfer_scope(self, source: CommandOwner, target: CommandOwner) -> int:
        """Move commands to another Session after a Session move; returns how many moved.

        Results go to the new Session: one submitted to the old Session and not
        yet delivered is withdrawn there and submitted again, unless its
        delivery already claimed it.
        """
        await self._settle_transfers(lambda owner: owner == source)
        moved = sum(record.transfer(source, target) for record in self._records.values())
        if self._checkpoints is not None:
            moved += self._checkpoints.transfer(source, target)
        for command_id, (report, retained_at) in tuple(self._recovered.items()):
            if report.owner == source:
                self._recovered[command_id] = (replace(report, owner=target), retained_at)
                self._notify(command_id)
        return moved

    async def transfer_agent_scope(self, agent_id: str, new_agent_id: str) -> int:
        """Move the commands of a renamed Identity Agent to its new id; returns how many.

        Results follow as in ``transfer_scope``.
        """
        await self._settle_transfers(
            lambda owner: owner.project_id is None and owner.agent_id == agent_id
        )
        moved = 0
        for record in self._records.values():
            owner = record.owner
            if owner.project_id is None and owner.agent_id == agent_id:
                moved += record.transfer(owner, replace(owner, agent_id=new_agent_id))
        if self._checkpoints is not None:
            moved += self._checkpoints.transfer_agent(agent_id, new_agent_id)
        for command_id, (report, retained_at) in tuple(self._recovered.items()):
            owner = report.owner
            if owner.project_id is None and owner.agent_id == agent_id:
                self._recovered[command_id] = (
                    replace(report, owner=replace(owner, agent_id=new_agent_id)),
                    retained_at,
                )
                self._notify(command_id)
        return moved

    async def _settle_transfers(self, matches: Callable[[CommandOwner], bool]) -> None:
        while pending := [
            task
            for task, launch in self._pending.items()
            if not task.done() and matches(launch.owner)
        ]:
            await _settle(pending)

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

    def has_unfinished_background(self, *, besides: CommandOwner | None = None) -> bool:
        """Whether a background command of a Session other than *besides* is still unfinished.

        Running, or ended with processes still running. An in-memory scan, cheap
        enough to decide whether a lookup of the Sessions that own them is worth it.
        """
        return any(
            record.background and not record.finished and record.owner != besides
            for record in self._records.values()
        )

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        """Stop the commands of an Extension execution group (reason ``scope``).

        Callers close a group only after every Run of the group settled. New
        launches for the group are refused until pending ones are drained and
        its commands stopped. An unconfirmed kill is logged, as in ``close_scope``.
        """
        key = (extension, group_id, epoch)
        self._closed_execution_groups.add(key)
        try:
            if self._checkpoints is not None:
                self._checkpoints.withdraw_execution(extension, group_id, epoch)
            await _settle(
                [
                    task
                    for task, launch in self._pending.items()
                    if launch.execution_owner is not None
                    and _group_key(launch.execution_owner) == key
                ]
            )
            await self._stop_scope(
                [
                    record
                    for record in self._records.values()
                    if record.execution_owner is not None
                    and _group_key(record.execution_owner) == key
                    and not record.finished
                ]
            )
            await self.flush()
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
        request: bytes | None,
        warm: bool,
        not_run_codes: dict[int, NotRunReason],
    ) -> CommandRecord:
        lease: TemporaryFileLease | None = None
        log_fd: int | None = None
        output: CommandOutput | None = None
        process: CommandProcess | None = None
        try:
            generation_id = None
            if self._checkpoints is not None:
                generation_id = await self._checkpoints.capture_generation(owner)
                if generation_id is None:
                    raise CommandNotStartedError("The Session that owned this command closed")
            if not await self._run_blocking(cwd.is_dir):
                raise CommandLaunchError(f"The workdir is not a directory: {model_path(cwd)}")
            if self._temporary_files is not None:
                lease, log_fd = await self._run_blocking(_open_log, self._temporary_files)
            # Starts an output worker for this command when one more may run.
            await self._run_blocking(self._output_host.prepare)
            try:
                output = CommandOutput.open(self._output_host, log_fd, self._run_blocking)
            except RuntimeError as error:
                raise CommandNotStartedError("vBot is shutting down") from error
            log_fd = None
            try:
                process = await self._launcher.launch(
                    argv, cwd, env, self._process_tracker, request, warm=warm
                )
            except Exception as error:
                raise CommandLaunchError(f"The command could not be started: {error}") from error
        except BaseException:
            if output is not None:
                output.close()
            if log_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(log_fd)
            if lease is not None:
                lease.finish()
            raise
        command_id = new_id("cmd", claim=lambda candidate: candidate not in self._records)
        if self._checkpoints is not None and generation_id is not None:
            self._checkpoints.bind(command_id, generation_id)
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
            output=output,
            log_path=lease.path if lease is not None else None,
            log_lease=lease,
            services=self._services,
            not_run_codes=not_run_codes,
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
        if self._checkpoints is not None:
            self._checkpoints.withdraw(matches)
        await _settle([task for task, launch in self._pending.items() if matches(launch.owner)])
        matching = [record for record in self._records.values() if matches(record.owner)]
        running = [record for record in matching if not record.finished]
        for record in matching:
            if record.finished:
                record.withdraw_delivery()
        await self._stop_scope(running)
        await self.flush()
        return len(running)

    async def _stop_scope(self, records: list[CommandRecord]) -> None:
        """Stop records for a lifecycle close; an unconfirmed kill is logged, not raised."""
        try:
            await self._stop_records(records, "scope")
        except CommandStopError as error:
            _LOGGER.warning(
                "A command survived the close of its scope (%s); it stays listed", error
            )

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
        """Forget commands whose last process ended longer than the retention ago."""
        now = self._monotonic()
        expired = [
            record
            for record in self._records.values()
            if record.finished_at is not None
            and now - record.finished_at >= self._retention_seconds
            and not (self._checkpoints is not None and record.delivery_pending)
        ]
        for record in expired:
            del self._records[record.command_id]
            if self._checkpoints is not None:
                self._checkpoints.delete(record.command_id)
        for record in expired:
            if record.background:
                self._notify(record.command_id)
        for command_id, (_report, retained_at) in tuple(self._recovered.items()):
            if now - retained_at >= self._retention_seconds and not (
                self._checkpoints is not None and command_id in self._checkpoints.pending_ids
            ):
                del self._recovered[command_id]
                # Recovery can still be waiting for delivery. Its checkpoint is
                # kept until that operation has finished.
                if (
                    self._checkpoints is not None
                    and command_id not in self._checkpoints.pending_ids
                ):
                    self._checkpoints.delete(command_id)
                self._notify(command_id)

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await self._sleep(self._sweep_interval_seconds)
                self._expire()
        except asyncio.CancelledError:
            return

    def _checkpoint(self, command_id: str) -> None:
        record = self._records.get(command_id)
        if self._checkpoints is not None and record is not None and record.background:
            self._checkpoints.put(
                record.report(),
                record.origin_run_id,
                pending=record.delivery_pending,
                body=record.delivery_body,
                execution_owner=record.execution_owner,
            )

    def _notify(self, command_id: str) -> None:
        self._checkpoint(command_id)
        for callback in list(self._callbacks):
            try:
                callback(command_id)
            except Exception:
                _LOGGER.warning(
                    "A command change callback failed (command=%s)", command_id, exc_info=True
                )


def _open_log(temporary_files: TemporaryFileManager) -> tuple[TemporaryFileLease, int]:
    """Create the command's private log file and open it for writing (blocking)."""
    lease = temporary_files.create(COMMAND_TEMPORARY_CATEGORY, ".log")
    try:
        if os.name != "nt":
            lease.path.chmod(0o600)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        return lease, os.open(lease.path, flags, 0o600)
    except BaseException:
        lease.finish()
        raise


def _raise_unconfirmed(records: list[CommandRecord]) -> None:
    if records:
        raise CommandManagerError(
            "Could not stop the processes of commands: "
            + ", ".join(record.command_id for record in records)
        )


async def _settle(launches: list[asyncio.Task[CommandRecord]]) -> None:
    """Wait until the launches in progress started their commands or failed."""
    if launches:
        await asyncio.gather(*(asyncio.shield(task) for task in launches), return_exceptions=True)


def _group_key(owner: RunExecutionOwner) -> tuple[str, str, str]:
    return (owner.extension, owner.group_id, owner.epoch)


__all__ = [
    "COMMAND_DRAIN_SECONDS",
    "COMMAND_HEAD_CHARS",
    "COMMAND_IDLE_WINDOW_SECONDS",
    "COMMAND_LEFTOVER_QUIET_SECONDS",
    "COMMAND_MAX_LIVE",
    "COMMAND_PROGRESS_LINES",
    "COMMAND_PROGRESS_SECONDS",
    "COMMAND_RECENT_LINES",
    "COMMAND_RETENTION_SECONDS",
    "COMMAND_STOP_GRACE_SECONDS",
    "COMMAND_SWEEP_INTERVAL_SECONDS",
    "COMMAND_TAIL_CHARS",
    "COMMAND_TEMPORARY_CATEGORY",
    "CommandCapacityError",
    "CommandCheckpointError",
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
    "LogLoss",
    "NotRunReason",
    "OutputCallback",
    "StopReason",
]
