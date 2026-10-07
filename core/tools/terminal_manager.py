"""Session-scoped interactive PTY/ConPTY lifecycle and activity management."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO

from core.runs import RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.tools import terminal_backend
from core.tools.shell_environment import terminal_environment
from core.tools.terminal_backend import (
    TerminalAdapter,
    TerminalAdapterFactory,
    default_terminal_argv,
    spawn_terminal_adapter,
)
from core.tools.terminal_store import (
    TERMINAL_AGENT_GROUP_ID_PREFIX,
    TERMINAL_FINISHED_GROUP_ID,
    TERMINAL_GROUP_NAME_MAX_CHARS,
    TERMINAL_MANUAL_GROUP_ID,
    GroupKind,
    TerminalGroup,
    TerminalLaunchHistoryEntry,
    TerminalOperatorStore,
    agent_group_id,
)
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.paths import model_path
from core.utils.processes import process_tree_runs

from ._terminal_catalog import TerminalCatalog
from ._terminal_command import (
    COMMAND_TEMPORARY_CATEGORY,
    CommandReport,
    CommandReportFormatter,
    CommandState,
    StopReason,
)
from ._terminal_launch import shell_launch
from ._terminal_process_tree import ProcessTreeTracker, track_process_tree
from ._terminal_render_host import TerminalRenderHost
from ._terminal_session import (
    CommandWaitOutcome,
    TerminalLaunch,
    TerminalObservation,
    TerminalSession,
    TerminalSessionServices,
)
from ._terminal_state import (
    TERMINAL_ACTIVITY_QUIET_SECONDS,
    TERMINAL_COMMAND_COLUMNS,
    TERMINAL_COMMAND_ROWS,
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_FINISHED_TTL,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_LIVE_COMMANDS,
    TERMINAL_MAX_LIVE_GLOBAL,
    TERMINAL_MAX_LIVE_PER_SESSION,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TERMINAL_SCROLLBACK_LINES,
    TERMINAL_START_WAIT_SECONDS,
    TERMINAL_STATUS_DEFAULT_LINES,
    TERMINAL_STATUS_MAX_LINES,
    TERMINAL_SWEEP_INTERVAL_SECONDS,
    TERMINAL_TEMPORARY_CATEGORY,
    AttentionKind,
    TerminalAlreadyAttachedError,
    TerminalCapacityError,
    TerminalChangedCallback,
    TerminalClosedError,
    TerminalInfo,
    TerminalIsCommandError,
    TerminalKind,
    TerminalLaunchError,
    TerminalManagerError,
    TerminalNotAttachedError,
    TerminalNotFoundError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalProgramNotRunningError,
    TerminalStaleScreenError,
    TerminalState,
    TerminalStreamEvent,
    WaitEnded,
    _utc_now,
    _validate_dimensions,
    _validate_owner,
)

_LOGGER = get_logger("tools.terminal_manager")


class TerminalManager:
    """Own Agent and manually started interactive terminal processes.

    Each process is a ``TerminalSession``; the manager admits, finds and
    authorizes them, applies scope lifecycle, and hands out ``TerminalInfo``
    views. Operator groups, ordering and summaries belong to the catalog.
    """

    def __init__(
        self,
        trigger_service: Any | None = None,
        *,
        temporary_files: TemporaryFileManager | None = None,
        launch_history_path: Path | None = None,
        groups_path: Path | None = None,
        data_dir: Path | None = None,
        adapter_factory: TerminalAdapterFactory | None = None,
        render_host: TerminalRenderHost | None = None,
        scrollback_lines: int = TERMINAL_SCROLLBACK_LINES,
        finished_session_ttl: timedelta = TERMINAL_FINISHED_TTL,
        sweep_interval_seconds: float = TERMINAL_SWEEP_INTERVAL_SECONDS,
        activity_quiet_seconds: float = TERMINAL_ACTIVITY_QUIET_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        program_probe: Callable[[int, str], bool] = process_tree_runs,
        process_tracker: ProcessTreeTracker = track_process_tree,
    ) -> None:
        if scrollback_lines < 1:
            raise ValueError("Terminal scrollback cap must be positive")
        if finished_session_ttl <= timedelta(0):
            raise ValueError("Terminal finished-session TTL must be positive")
        if sweep_interval_seconds <= 0:
            raise ValueError("Terminal sweep interval must be positive")
        if activity_quiet_seconds <= 0:
            raise ValueError("Terminal activity quiet period must be positive")
        self._temporary_files = temporary_files
        self._operator_store = TerminalOperatorStore(
            launch_history_path=launch_history_path,
            groups_path=groups_path,
            data_dir=data_dir,
        )
        self._adapter_factory: TerminalAdapterFactory = adapter_factory or spawn_terminal_adapter
        # A host passed in belongs to the caller, which closes it.
        self._owns_render_host = render_host is None
        self._render_host = render_host or TerminalRenderHost()
        self._scrollback_lines = scrollback_lines
        self._finished_session_ttl = finished_session_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._sessions: dict[str, TerminalSession] = {}
        self._pending_spawns: dict[
            asyncio.Task[TerminalSession], tuple[TerminalOwner | None, TerminalKind]
        ] = {}
        self._pending_execution_spawns: dict[asyncio.Task[TerminalSession], RunExecutionOwner] = {}
        self._pending_command_runs: dict[asyncio.Task[TerminalSession], str] = {}
        self._closed_execution_groups: set[tuple[str, str, str]] = set()
        # Runs being cancelled: their commands are stopped and no new one starts.
        self._cancelled_runs: set[str] = set()
        self._closed = False
        self._program_probe = program_probe
        self._process_tracker = process_tracker
        self._reader_executor = ThreadPoolExecutor(
            max_workers=TERMINAL_MAX_LIVE_GLOBAL + TERMINAL_MAX_LIVE_COMMANDS,
            thread_name_prefix="vbot-terminal-read",
        )
        self._catalog = TerminalCatalog(
            self._operator_store,
            lambda: [session.info() for session in self._sessions.values() if not session.hidden],
            self._terminate_by_id,
        )
        self._services = TerminalSessionServices(
            trigger_service=trigger_service,
            reader_executor=self._reader_executor,
            activity_quiet_seconds=activity_quiet_seconds,
            monotonic=monotonic,
            sleep=sleep,
            program_probe=program_probe,
            operator_summary=self._catalog.summary,
            changed=self._catalog.notify_changed,
        )
        self._sweeper_task: asyncio.Task[None] | None = None

    # Operator catalog

    def add_changed_callback(self, callback: TerminalChangedCallback) -> Callable[[], None]:
        """Notify transport edges when operator-visible Terminal state changes."""
        return self._catalog.add_changed_callback(callback)

    def list_for_operator(self) -> list[dict[str, Any]]:
        """Return retained Terminal Sessions grouped and ordered for display."""
        return self._catalog.list_for_operator()

    def list_groups_for_operator(self) -> list[dict[str, Any]]:
        """Return operator-visible groups: user/agent groups, then the shared
        manual automatic group, then one automatic group per active Agent."""
        return self._catalog.list_groups_for_operator()

    def list_operator_launch_history(self) -> list[dict[str, Any]]:
        """Return newest-first manual launch configurations for operator reuse."""
        return self._catalog.list_operator_launch_history()

    def create_group_for_operator(self, name: str) -> dict[str, Any]:
        """Create one durable user group with a unique name."""
        return self._catalog.create_group_for_operator(name)

    def rename_group_for_operator(self, group_id: str, name: str) -> dict[str, Any]:
        """Rename one user or agent group; automatic groups are fixed."""
        return self._catalog.rename_group_for_operator(group_id, name)

    def set_group_order_for_operator(self, group_id: str, order: Sequence[str]) -> dict[str, Any]:
        """Persist one user-set Terminal order; missing ids are appended."""
        return self._catalog.set_group_order_for_operator(group_id, order)

    async def delete_group_for_operator(self, group_id: str) -> dict[str, Any]:
        """Remove a user or Agent group, retaining stopped terminals as finished."""
        return await self._catalog.delete_group_for_operator(group_id)

    def resolve_or_create_agent_group(self, name: str) -> TerminalGroup:
        """Resolve an existing named group or create a non-durable Agent group."""
        return self._catalog.resolve_or_create_agent_group(name)

    async def running_programs_for_operator(self) -> dict[str, bool]:
        """Whether each live manual Terminal's launch program still runs in it.

        Keyed by terminal id, for Terminals started with a launch command that
        have not finished. The program is the command's file name without its
        extension (``codex`` for ``C:\\bin\\codex.cmd``), looked up in the
        Terminal's process tree with the probe that guards expected-program
        input, off the Event Loop; a tree that cannot be inspected reads as not
        running. The shell stays open after its program ends or fails to start,
        so the Terminal's state alone cannot tell.
        """
        checks = [
            (session.terminal_id, session.pid, program)
            for session in self._sessions.values()
            if not session.finished and (program := _launch_program(session.launch.launch_command))
        ]
        if not checks:
            return {}
        probe = self._program_probe

        def run_checks() -> dict[str, bool]:
            return {terminal_id: probe(pid, program) for terminal_id, pid, program in checks}

        return await asyncio.to_thread(run_checks)

    # Lifecycle

    def start(self) -> None:
        """Start bounded retention cleanup when an event loop is available."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop(), name="terminal-manager-sweep")

    def stop(self) -> None:
        """Synchronously stop every child and cancel background tasks."""
        self._closed = True
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None
        sessions = list(self._sessions.values())
        failures = [session.terminal_id for session in sessions if not session.stop_now()]
        if failures:
            raise TerminalManagerError(
                "Could not terminate terminal process trees: " + ", ".join(failures)
            )
        self._reader_executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Stop all children, await their tasks, then release the renderer."""
        sweeper = self._sweeper_task
        failure: TerminalManagerError | None = None
        try:
            self.stop()
        except TerminalManagerError as error:
            failure = error
        tasks: list[asyncio.Task[Any]] = list(self._pending_spawns)
        if sweeper is not None and not sweeper.done():
            tasks.append(sweeper)
        for session in self._sessions.values():
            tasks.extend(session.pending_tasks())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_render_host:
            await asyncio.to_thread(self._render_host.close)
        if failure is not None:
            raise failure

    # Starting terminals

    async def spawn(
        self,
        owner: TerminalOwner,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        columns: int = TERMINAL_DEFAULT_COLUMNS,
        rows: int = TERMINAL_DEFAULT_ROWS,
        origin_run_id: str,
        initial_text: str | None = None,
        name: str | None = None,
        group_id: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> TerminalInfo:
        """Start one Agent-owned program behind PTY/ConPTY with its exact argv."""
        _validate_owner(owner)
        if not argv or not argv[0]:
            raise ValueError("Terminal command must not be empty")
        if any(not isinstance(token, str) for token in argv):
            raise ValueError("Terminal command and arguments must be strings")
        session = await self._spawn(
            owner,
            argv,
            launch=TerminalLaunch(command=argv[0], arguments=tuple(argv[1:]), cwd=cwd),
            env=env,
            columns=columns,
            rows=rows,
            origin_run_id=origin_run_id,
            initial_text=initial_text,
            name=name,
            group_id=group_id,
            execution_owner=execution_owner,
        )
        return session.info()

    async def spawn_for_operator(
        self,
        *,
        command: str | None,
        arguments: Sequence[str],
        cwd: Path | None,
        launch_workdir: str | None = None,
        name: str | None = None,
        group_id: str | None = None,
        columns: int = TERMINAL_DEFAULT_COLUMNS,
        rows: int = TERMINAL_DEFAULT_ROWS,
    ) -> dict[str, Any]:
        """Start one manual Terminal Session that behaves like a normal terminal.

        Without a command the interactive shell starts with *arguments*. A
        requested command runs inside that shell, handed over through the
        shell's own start options: the shell loads its profile, runs the
        program, and keeps its prompt after the program ends or is interrupted.
        """
        environment = await asyncio.to_thread(terminal_environment)
        shell_argv = default_terminal_argv(environment)
        workdir = cwd or Path.home()
        if command is None:
            argv = [*shell_argv, *arguments]
            launch = TerminalLaunch(command=argv[0], arguments=tuple(argv[1:]), cwd=workdir)
            session = await self._spawn(
                None,
                argv,
                launch=launch,
                env=None,
                columns=columns,
                rows=rows,
                origin_run_id=None,
                name=name,
                group_id=group_id,
            )
        else:
            start = shell_launch(shell_argv, command, arguments, environment=environment)
            launch = TerminalLaunch(
                command=shell_argv[0],
                arguments=tuple(shell_argv[1:]),
                cwd=workdir,
                launch_command=command,
                launch_arguments=tuple(arguments),
            )
            try:
                session = await self._spawn(
                    None,
                    start.argv,
                    launch=launch,
                    env=start.environment or None,
                    columns=columns,
                    rows=rows,
                    origin_run_id=None,
                    name=name,
                    group_id=group_id,
                    command_line=start.command_line,
                    cleanup=start.remove_scratch if start.scratch is not None else None,
                )
            except BaseException:
                start.remove_scratch()
                raise
        remembered_workdir = launch_workdir
        if remembered_workdir is None and cwd is not None:
            remembered_workdir = str(cwd)
        self._operator_store.remember_launch(
            command=launch.launch_command,
            arguments=launch.launch_arguments,
            workdir=remembered_workdir,
        )
        return self._catalog.summary(session.info())

    async def spawn_command(
        self,
        owner: TerminalOwner,
        argv: Sequence[str],
        *,
        command: str,
        description: str | None,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float | None,
        formatter: CommandReportFormatter,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None = None,
        command_line: str | None = None,
    ) -> str:
        """Start one shell command in a new terminal; returns its terminal id.

        *env* is the complete environment. The command stays unlisted until
        ``hand_off_command``; its process tree is tracked from the start, and a
        tree that cannot be tracked is not started. *timeout_seconds* stops the
        command (Ctrl+C, then a kill), whether or not it was handed off.
        """
        _validate_owner(owner)
        if origin_run_id in self._cancelled_runs:
            raise TerminalClosedError("The Run that started this command was cancelled")
        session = await self._spawn(
            owner,
            argv,
            launch=TerminalLaunch(command=argv[0], arguments=tuple(argv[1:]), cwd=cwd),
            env=env,
            exact_env=True,
            columns=TERMINAL_COMMAND_COLUMNS,
            rows=TERMINAL_COMMAND_ROWS,
            origin_run_id=origin_run_id,
            name=description,
            execution_owner=execution_owner,
            command_line=command_line,
            kind="command",
            command_options=(command, description, timeout_seconds, formatter),
        )
        return session.terminal_id

    async def wait_command(
        self,
        terminal_id: str,
        *,
        seconds: float | None,
        idle_seconds: float | None,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> CommandWaitOutcome:
        """Wait for the command to end, for *seconds* (None: no limit), or idleness.

        A command ends once its shell exited and every process it left running
        ended or went quiet; the outcome is then ``exited``.
        """
        deadline = None if seconds is None else self._services.monotonic() + seconds
        return await self._get(terminal_id).wait_command(
            deadline=deadline, idle_seconds=idle_seconds, progress=progress
        )

    async def stop_command(self, terminal_id: str, reason: StopReason) -> CommandReport:
        """Interrupt the command, kill what still runs, and return its outcome."""
        session = self._get(terminal_id)
        await session.stop_command(reason)
        return session.command_report()

    def hand_off_command(self, terminal_id: str, *, deliver: bool) -> CommandReport:
        """List a running command; with *deliver*, its result is delivered when it ends.

        The returned report tells whether the command ended before the hand-off,
        in which case nothing is delivered and the caller reports the result.
        """
        session = self._get(terminal_id)
        session.hand_off(deliver=deliver)
        return session.command_report()

    def command_report(self, terminal_id: str) -> CommandReport:
        return self._get(terminal_id).command_report()

    async def command_idle_seconds(self, terminal_id: str) -> float | None:
        """How long a running command has printed nothing, got no input and used no CPU.

        None while it works, as the command idle rule decides, and once its
        shell exited: processes it left running end the command when they go quiet.
        """
        return await self._get(terminal_id).command_idle_seconds()

    def command_status(self, terminal_id: str) -> str | None:
        """The status of a listed command, or None for an unknown or unlisted terminal.

        ``running``, ``completed`` (exit code 0), ``failed`` (another exit
        code) or ``stopped`` (vBot stopped it).
        """
        session = self._sessions.get(terminal_id)
        if session is None or session.command is None or session.hidden:
            return None
        report = session.command_report()
        if not report.exited:
            return "running"
        if report.stop_reason is not None:
            return "stopped"
        return "completed" if report.exit_code == 0 else "failed"

    async def wait_finished(self, terminal_id: str) -> None:
        """Wait until none of the terminal's processes runs any longer."""
        await self._get(terminal_id).wait_finished()

    async def command_screen(self, terminal_id: str, lines: int) -> str:
        """The newest non-blank rows of a command's screen."""
        return await self._get(terminal_id).command_screen(lines)

    async def command_view(self, terminal_id: str, rows: int) -> tuple[CommandReport, str]:
        """A command's report and the output that follows its transcript, without
        gap or overlap: while the shell runs, the screen's newest *rows* rows."""
        return await self._get(terminal_id).command_view(rows)

    async def cancel_run(self, run_id: str) -> None:
        """Kill the foreground commands a cancelled Run started and reject new ones for it.

        Commands handed off to the background keep running and deliver their
        result as usual; stopping them is part of the user's "Stop all".
        """
        if not run_id:
            return
        self._cancelled_runs.add(run_id)
        pending = [task for task, run in self._pending_command_runs.items() if run == run_id]
        if pending:
            await asyncio.gather(
                *(asyncio.shield(task) for task in pending), return_exceptions=True
            )
        sessions = [
            session
            for session in self._sessions.values()
            if session.command is not None
            and session.hidden
            and session.origin_run_id == run_id
            and not session.finished
        ]
        await asyncio.gather(
            *(
                session.terminate(suppress_attention=True, reason="run_cancelled")
                for session in sessions
            )
        )

    def release_run(self, run_id: str) -> None:
        """Forget a settled Run's cancellation marker.

        Call this only once the Run is terminal and its Tool tasks have settled:
        no command for it can start any more.
        """
        self._cancelled_runs.discard(run_id)

    async def shutdown_commands(self) -> None:
        """Stop every command before vBot shuts down.

        A handed-off command's result is submitted as stopped by the shutdown,
        so it reaches its Session even though the command could not finish.
        Call this before the delivery service closes.
        """
        sessions = [
            session
            for session in self._sessions.values()
            if session.command is not None and not session.finished
        ]
        await asyncio.gather(
            *(
                session.terminate(suppress_attention=False, reason="shutdown")
                for session in sessions
            ),
            return_exceptions=True,
        )
        # Let the result deliveries the stops scheduled hand their results over.
        await asyncio.sleep(0)

    async def _spawn(
        self,
        owner: TerminalOwner | None,
        argv: Sequence[str],
        *,
        execution_owner: RunExecutionOwner | None = None,
        kind: TerminalKind = "terminal",
        **kwargs: Any,
    ) -> TerminalSession:
        """Reserve capacity before starting work and retain ownership through cancellation."""
        if self._closed:
            raise TerminalClosedError("Terminal Session is no longer running")
        if (
            execution_owner is not None
            and (
                execution_owner.extension,
                execution_owner.group_id,
                execution_owner.epoch,
            )
            in self._closed_execution_groups
        ):
            raise TerminalClosedError("Terminal Session is no longer running")
        self._enforce_capacity(owner, kind)
        task = asyncio.create_task(
            self._spawn_admitted(owner, argv, execution_owner=execution_owner, **kwargs)
        )
        self._pending_spawns[task] = (owner, kind)
        if execution_owner is not None:
            self._pending_execution_spawns[task] = execution_owner
        if kind == "command" and kwargs.get("origin_run_id"):
            self._pending_command_runs[task] = kwargs["origin_run_id"]
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A worker cannot be cancelled once process creation has started. Keep
            # its result owned until it has either failed or been stopped, even if
            # the caller receives repeated cancellation requests during cleanup.
            cleanup = asyncio.create_task(self._discard_cancelled_spawn(task))
            while not cleanup.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            cleanup.result()
            raise
        finally:
            self._pending_spawns.pop(task, None)
            self._pending_execution_spawns.pop(task, None)
            self._pending_command_runs.pop(task, None)

    async def _discard_cancelled_spawn(self, task: asyncio.Task[TerminalSession]) -> None:
        try:
            session = await task
        except Exception:
            return
        await session.terminate(suppress_attention=True)

    async def _spawn_admitted(
        self,
        owner: TerminalOwner | None,
        argv: Sequence[str],
        *,
        launch: TerminalLaunch,
        env: Mapping[str, str] | None,
        columns: int,
        rows: int,
        origin_run_id: str | None,
        initial_text: str | None = None,
        name: str | None = None,
        group_id: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
        command_line: str | None = None,
        cleanup: Callable[[], None] | None = None,
        exact_env: bool = False,
        command_options: tuple[str, str | None, float | None, CommandReportFormatter] | None = None,
    ) -> TerminalSession:
        """Start one process behind PTY/ConPTY and its Terminal Session."""
        _validate_dimensions(columns, rows)
        cwd = launch.cwd
        if not cwd.is_dir():
            raise ValueError(f"Terminal workdir is not a directory: {model_path(cwd)}")
        if group_id is not None:
            self._catalog.require_group(group_id)

        log_path: Path | None = None
        log_handle: TextIO | None = None
        log_lease: TemporaryFileLease | None = None
        transcript_lease: TemporaryFileLease | None = None
        screen = None
        adapter: TerminalAdapter | None = None
        command: CommandState | None = None
        try:
            await asyncio.to_thread(self._render_host.prepare)
            screen = self._render_host.open_screen(
                columns,
                rows,
                scrollback_lines=self._scrollback_lines,
                transcript=command_options is not None,
            )
            if command_options is None:
                log_path, log_handle, log_lease = self._open_raw_log()
            elif self._temporary_files is not None:
                transcript_lease = self._temporary_files.create(COMMAND_TEMPORARY_CATEGORY, ".log")
            adapter = await self._start_process(
                argv, cwd, env, rows, columns, command_line, exact_env=exact_env
            )
            if command_options is not None:
                tree = await asyncio.to_thread(self._process_tracker, adapter.pid)
                text, description, timeout_seconds, formatter = command_options
                command = CommandState(
                    command=text,
                    description=description,
                    workdir=cwd,
                    tree=tree,
                    transcript_lease=transcript_lease,
                    timeout_seconds=timeout_seconds,
                    formatter=formatter,
                    started_at=self._services.monotonic(),
                )
            if self._closed:
                raise TerminalClosedError("Terminal Session is no longer running")
        except Exception as error:
            if adapter is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    if command is not None and command.tree is not None:
                        await asyncio.to_thread(command.tree.terminate)
                    else:
                        await asyncio.to_thread(terminal_backend.terminate_process_tree, adapter)
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(adapter.close)
            if command is not None:
                command.close()
            elif transcript_lease is not None:
                transcript_lease.finish()
            if screen is not None:
                screen.close()
            if log_handle is not None:
                log_handle.close()
            if log_lease is not None:
                log_lease.finish()
            if isinstance(error, TerminalClosedError):
                raise
            raise TerminalLaunchError(f"Terminal process could not be started: {error}") from error

        terminal_id = new_id("term", claim=lambda candidate: candidate not in self._sessions)
        session = TerminalSession(
            terminal_id,
            owner=owner,
            adapter=adapter,
            screen=screen,
            launch=launch,
            name=name,
            group_id=group_id,
            origin_run_id=origin_run_id,
            execution_owner=execution_owner,
            awaiting_initial_input=initial_text is not None,
            log_path=log_path,
            log_handle=log_handle,
            log_lease=log_lease,
            cleanup=cleanup,
            services=self._services,
            command=command,
        )
        if group_id is not None:
            self._catalog.add_to_group(group_id, terminal_id)
        self._sessions[terminal_id] = session
        session.start(initial_text=initial_text)
        return session

    async def _start_process(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None,
        rows: int,
        columns: int,
        command_line: str | None,
        *,
        exact_env: bool = False,
    ) -> TerminalAdapter:
        """Start the process in *env* when exact, else in a terminal environment plus *env*."""
        if exact_env and env is not None:
            process_env = dict(env)
        else:
            process_env = await asyncio.to_thread(terminal_environment, env)
        return await asyncio.to_thread(
            self._adapter_factory,
            list(argv),
            cwd,
            process_env,
            rows,
            columns,
            command_line=command_line,
        )

    # Finding and attaching terminals

    def list_terminals(self) -> list[TerminalInfo]:
        """Return all retained Terminal Sessions discoverable for attachment."""
        return sorted(
            (session.info() for session in self._sessions.values() if not session.hidden),
            key=lambda info: info.started_at,
        )

    def terminal(self, terminal_id: str, owner: TerminalOwner) -> TerminalInfo:
        """Return a Terminal Session attached to the exact vBot Session."""
        return self._attached(terminal_id, owner).info()

    def attach(
        self,
        terminal_id: str,
        attachment: TerminalOwner,
        *,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None = None,
    ) -> tuple[TerminalInfo, bool]:
        """Attach one live Terminal Session without changing its process or lifecycle.

        A shell command stays attached to the Session that ran it, so its result
        reaches that Session: attaching it elsewhere raises ``TerminalIsCommandError``.
        """
        _validate_owner(attachment)
        session = self._get(terminal_id)
        if session.command is not None:
            if session.attachment != attachment:
                raise TerminalIsCommandError(
                    f"Terminal {terminal_id} runs a command of another Session"
                )
            return session.info(), False
        changed = session.attach(
            attachment, origin_run_id=origin_run_id, execution_owner=execution_owner
        )
        if changed:
            _LOGGER.info(
                "Attached Terminal Session (terminal=%s agent=%s session=%s project=%s)",
                terminal_id,
                attachment.agent_id,
                attachment.session_id,
                attachment.project_id,
            )
        return session.info(), changed

    def detach(self, terminal_id: str, attachment: TerminalOwner) -> TerminalInfo:
        """Remove only one exact vBot Session attachment; a command stays attached."""
        session = self._get(terminal_id)
        if session.command is not None:
            raise TerminalIsCommandError(f"Terminal {terminal_id} runs a command")
        if session.attachment != attachment:
            raise TerminalNotAttachedError("Terminal Session is not attached to this vBot Session.")
        session.detach()
        _LOGGER.info(
            "Detached Terminal Session (terminal=%s agent=%s session=%s project=%s)",
            terminal_id,
            attachment.agent_id,
            attachment.session_id,
            attachment.project_id,
        )
        return session.info()

    # Operator access (no attachment required)

    async def watch_for_operator(self, terminal_id: str) -> AsyncGenerator[TerminalStreamEvent]:
        """Yield an authoritative VT snapshot followed by sequenced live events."""
        session = self._get(terminal_id)
        async with contextlib.aclosing(session.watch()) as events:
            async for event in events:
                yield event

    async def read_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Read a bounded screen without changing any Agent's observation or binding."""
        return await self._get(terminal_id).read_for_operator()

    async def send_operator_input(
        self,
        terminal_id: str,
        data: str,
        *,
        expected_screen_revision: int | None = None,
        expected_program: str | None = None,
    ) -> dict[str, Any]:
        """Write exact user-controlled terminal bytes through the existing PTY.

        ``expected_screen_revision`` rejects input based on an older screen;
        ``expected_program`` rejects input unless that program (a command name
        such as ``codex``) runs in the Terminal's process tree.
        """
        if not isinstance(data, str) or not data:
            raise ValueError("Terminal input must be a non-empty string")
        if len(data) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(
                f"Terminal input must not exceed {TERMINAL_INPUT_MAX_CHARS} characters"
            )
        if expected_program is not None and (
            not isinstance(expected_program, str) or not expected_program.strip()
        ):
            raise ValueError("expected_program must be a non-empty program name")
        session = self._get(terminal_id)
        await session.send_operator_input(
            data,
            expected_screen_revision=expected_screen_revision,
            expected_program=expected_program,
        )
        return self._catalog.summary(session.info())

    async def resize_for_operator(
        self, terminal_id: str, *, columns: int, rows: int
    ) -> dict[str, Any]:
        """Resize an operator-selected Terminal Session."""
        session = self._get(terminal_id)
        await session.resize(columns, rows)
        return self._catalog.summary(session.info())

    async def kill_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Explicitly stop an operator-selected Terminal Session.

        A command is interrupted and then killed; its result tells its Session
        that the user stopped it.
        """
        session = self._get(terminal_id)
        if session.command is not None:
            await session.stop_command("user")
        else:
            await session.terminate(suppress_attention=True)
        return self._catalog.summary(session.info())

    def forget_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Remove one finished Terminal Session from the retained operator catalog."""
        session = self._get(terminal_id)
        info = session.info()
        if not info.finished or info.finished_at is None:
            raise ValueError("A running Terminal Session must be stopped before removal")
        summary = self._catalog.summary(info)
        self._forget(session)
        return summary

    # Agent access (attachment required)

    async def snapshot(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        lines: int = TERMINAL_STATUS_DEFAULT_LINES,
        start_line: int | None = None,
        include_name: bool = True,
    ) -> dict[str, Any]:
        """Return the screen and one history page.

        ``start_line`` is an absolute history line; omit it for the lines
        directly above the screen. The ``observation`` entry identifies the
        shown screen for ``acknowledge_screen``.
        """
        if not 1 <= lines <= TERMINAL_STATUS_MAX_LINES:
            raise ValueError(f"lines must be between 1 and {TERMINAL_STATUS_MAX_LINES}")
        if start_line is not None and start_line < 0:
            raise ValueError("start_line must be a non-negative integer")
        session = self._attached(terminal_id, owner)
        return await session.snapshot(lines=lines, start_line=start_line, include_name=include_name)

    async def read(
        self,
        terminal_id: str,
        *,
        lines: int = TERMINAL_STATUS_DEFAULT_LINES,
        start_line: int | None = None,
    ) -> dict[str, Any]:
        """Return the screen and one history page of any terminal, attached or not.

        Reading attaches nothing and acknowledges nothing: no Agent's
        deliveries change.
        """
        if not 1 <= lines <= TERMINAL_STATUS_MAX_LINES:
            raise ValueError(f"lines must be between 1 and {TERMINAL_STATUS_MAX_LINES}")
        if start_line is not None and start_line < 0:
            raise ValueError("start_line must be a non-negative integer")
        return await self._get(terminal_id).snapshot(
            lines=lines, start_line=start_line, include_name=True
        )

    async def wait(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        seconds: float,
        pattern: re.Pattern[str] | None = None,
        after_revision: int | None = None,
        on_match: Callable[[str], None] | None = None,
    ) -> WaitEnded:
        """Wait up to *seconds* until the program exits, prints a match, or its output settles.

        A command's wait ends only when its shell exits, at a match, or after
        *seconds*. An interactive program's wait also ends (``quiet``) once
        its output settled after activity in an attention revision above
        *after_revision* (default: the last one a durable result
        acknowledged); with *pattern* that does not end its wait. Output
        printed before the call counts for *pattern*, except output before the
        Agent's last input and that input's echo. *on_match* receives the
        line where a match starts.
        """
        session = self._attached(terminal_id, owner)
        if after_revision is None:
            after_revision = session.info().acknowledged_attention_revision
        return await session.wait_for_program(
            deadline=self._services.monotonic() + seconds,
            pattern=pattern,
            after_revision=after_revision,
            on_match=on_match,
        )

    async def wait_for_reply(
        self, terminal_id: str, owner: TerminalOwner, *, seconds: float, after_quiet: int
    ) -> WaitEnded:
        """Wait up to *seconds* until the output settles after input, or the program exits.

        *after_quiet* is the ``quiet_boundaries`` count ``send_input`` returned.
        Ends ``quiet``, ``exited`` or ``timeout``.
        """
        return await self._attached(terminal_id, owner).wait_for_reply(
            deadline=self._services.monotonic() + seconds, after_quiet=after_quiet
        )

    async def wait_for_startup(
        self, terminal_id: str, *, seconds: float = TERMINAL_START_WAIT_SECONDS
    ) -> None:
        """Wait until a started program shows its first screen, at most *seconds*.

        Returns once start-up output paused for the activity quiet period,
        the output after start's text settled, or the program ended.
        """
        await self._get(terminal_id).wait_started(deadline=self._services.monotonic() + seconds)

    def acknowledge_exit(self, terminal_id: str, owner: TerminalOwner) -> None:
        """A durable result showed the program's end: do not deliver its exit."""
        session = self._sessions.get(terminal_id)
        if session is not None and session.attachment == owner:
            session.acknowledge_exit()

    async def send_input(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        text: str | None,
        key: str | None,
        expected_screen_revision: int | None,
        origin_run_id: str,
        data: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> dict[str, Any]:
        """Write exact data or named terminal input and track generic PTY activity."""
        return await self._attached(terminal_id, owner).send_input(
            text=text,
            key=key,
            expected_screen_revision=expected_screen_revision,
            origin_run_id=origin_run_id,
            data=data,
            execution_owner=execution_owner,
        )

    async def kill(self, terminal_id: str, owner: TerminalOwner) -> TerminalInfo:
        """Explicitly terminate one Terminal Session without an automatic exit wakeup.

        A program that already ended stays as it ended.
        """
        session = self._attached(terminal_id, owner)
        await session.terminate(suppress_attention=True)
        return session.info()

    def acknowledge_screen(
        self, terminal_id: str, owner: TerminalOwner, observation: TerminalObservation
    ) -> bool:
        """Remember a durably delivered screen; False when the attachment is gone."""
        session = self._sessions.get(terminal_id)
        if session is None or session.attachment != owner:
            return False
        session.acknowledge_screen(observation)
        return True

    def acknowledge_attention(self, terminal_id: str, owner: TerminalOwner, revision: int) -> None:
        """Cancel equivalent automatic delivery after a manual result is durable."""
        self._attached(terminal_id, owner).acknowledge_attention(revision)

    # Scope lifecycle

    def has_execution_work(self, owner: RunExecutionOwner) -> bool:
        return any(value == owner for value in self._pending_execution_spawns.values()) or any(
            session.execution_owner == owner and not session.finished
            for session in self._sessions.values()
        )

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        """Drain exact execution-owned terminals without changing attachment authority.

        Callers close a group only after every Run of the group has settled.
        Admission stays closed while pending launches drain and owned terminals
        are terminated; a launch racing the drain is rejected. After the drain
        the admission marker is released.
        """
        key = (extension, group_id, epoch)
        self._closed_execution_groups.add(key)
        pending = [
            task
            for task, owner in self._pending_execution_spawns.items()
            if (owner.extension, owner.group_id, owner.epoch) == key
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        sessions = [
            session
            for session in self._sessions.values()
            if session.execution_owner is not None
            and (
                session.execution_owner.extension,
                session.execution_owner.group_id,
                session.execution_owner.epoch,
            )
            == key
        ]
        try:
            await asyncio.gather(
                *(session.terminate(suppress_attention=True) for session in sessions)
            )
        finally:
            # The group owner settles every Run of the group before closing its
            # resources, and each epoch key is used only once. With the launches
            # drained above, no launch for this key can arrive any more, so the
            # closed marker is released instead of accumulating for the server life.
            self._closed_execution_groups.discard(key)

    async def close_scope(self, owner: TerminalOwner) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Session."""
        await self._close_matching(lambda scope: scope == owner)

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Agent scope."""
        await self._close_matching(
            lambda scope: scope.agent_id == agent_id and scope.project_id == project_id
        )

    async def close_project_scope(self, project_id: str) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Project."""
        await self._close_matching(lambda scope: scope.project_id == project_id)

    def transfer_scope(self, source: TerminalOwner, target: TerminalOwner) -> int:
        """Transfer lifecycle and attachment scopes after a successful Session move."""
        return sum(session.transfer(source, target) for session in self._sessions.values())

    def transfer_agent_scope(self, agent_id: str, new_agent_id: str) -> int:
        """Move every lifecycle and attachment scope of a renamed Identity Agent to its new id.

        Each scope keeps its Session id, and a pending delivery follows the
        attachment; immutable provenance keeps the old id. Returns how many
        Terminal Sessions changed.
        """
        moved = 0
        for session in list(self._sessions.values()):
            scopes = {
                scope
                for scope in (session.lifecycle_owner, session.attachment)
                if scope is not None and scope.project_id is None and scope.agent_id == agent_id
            }
            for scope in scopes:
                session.transfer(scope, replace(scope, agent_id=new_agent_id))
            moved += bool(scopes)
        return moved

    async def _close_matching(self, matches: Callable[[TerminalOwner], bool]) -> None:
        """Stop terminals whose lifecycle owner matches; detach the others' attachments."""
        terminating = [
            session
            for session in self._sessions.values()
            if session.lifecycle_owner is not None and matches(session.lifecycle_owner)
        ]
        for session in self._sessions.values():
            attachment = session.attachment
            if session not in terminating and attachment is not None and matches(attachment):
                session.detach()
        await asyncio.gather(
            *(session.terminate(suppress_attention=True) for session in terminating)
        )

    # Retention

    async def sweep_finished(self) -> None:
        """Forget terminal metadata after the bounded inspection window."""
        cutoff = _utc_now() - self._finished_session_ttl
        expired = [
            session
            for session in self._sessions.values()
            if session.finished_at is not None and session.finished_at < cutoff
        ]
        for session in expired:
            self._forget(session)

    def _forget(self, session: TerminalSession) -> None:
        self._sessions.pop(session.terminal_id, None)
        session.release()
        self._catalog.notify_changed(session.terminal_id)

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_finished()
        except asyncio.CancelledError:
            return

    # Helpers

    def _get(self, terminal_id: str) -> TerminalSession:
        session = self._sessions.get(terminal_id)
        if session is None:
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        return session

    def _attached(self, terminal_id: str, owner: TerminalOwner) -> TerminalSession:
        session = self._get(terminal_id)
        if session.attachment != owner:
            raise TerminalNotOwnedError(
                f"Terminal Session is not attached to this vBot Session; attach it first "
                f"(id: {terminal_id})"
            )
        return session

    async def _terminate_by_id(self, terminal_id: str) -> None:
        await self._get(terminal_id).terminate(suppress_attention=True)

    def _enforce_capacity(self, owner: TerminalOwner | None, kind: TerminalKind) -> None:
        pending = [
            pending_owner
            for task, (pending_owner, pending_kind) in self._pending_spawns.items()
            if not task.done() and pending_kind == kind
        ]
        live = [
            session
            for session in self._sessions.values()
            if not session.finished
            and ("command" if session.command is not None else "terminal") == kind
        ]
        if kind == "command":
            if len(live) + len(pending) >= TERMINAL_MAX_LIVE_COMMANDS:
                raise TerminalCapacityError(
                    f"Running command limit reached ({TERMINAL_MAX_LIVE_COMMANDS})",
                    scope="commands",
                    limit=TERMINAL_MAX_LIVE_COMMANDS,
                )
            return
        # The live terminals the caller started: the ones it can stop.
        mine = tuple(
            session.info()
            for session in sorted(live, key=lambda item: item.started_at)
            if owner is not None and session.lifecycle_owner == owner
        )
        if len(live) + len(pending) >= TERMINAL_MAX_LIVE_GLOBAL:
            raise TerminalCapacityError(
                f"Live Terminal Session limit reached ({TERMINAL_MAX_LIVE_GLOBAL})",
                scope="global",
                limit=TERMINAL_MAX_LIVE_GLOBAL,
                terminals=mine,
            )
        owned = len(mine)
        owned += sum(1 for pending_owner in pending if owner is not None and pending_owner == owner)
        if owner is not None and owned >= TERMINAL_MAX_LIVE_PER_SESSION:
            raise TerminalCapacityError(
                "Live Terminal Session limit reached for this vBot Session "
                f"({TERMINAL_MAX_LIVE_PER_SESSION})",
                scope="session",
                limit=TERMINAL_MAX_LIVE_PER_SESSION,
                terminals=mine,
            )

    def _open_raw_log(
        self,
    ) -> tuple[Path | None, TextIO | None, TemporaryFileLease | None]:
        if self._temporary_files is None:
            return None, None, None
        lease = self._temporary_files.create(TERMINAL_TEMPORARY_CATEGORY, ".log")
        return lease.path, lease.path.open("a", encoding="utf-8", newline=""), lease


def _launch_program(command: str | None) -> str:
    """The program a launch command starts: its file name without the extension."""
    if not command or not command.strip():
        return ""
    name = command.strip().strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, _extension = name.rpartition(".")
    return stem if dot and stem else name


__all__ = [
    "AttentionKind",
    "GroupKind",
    "TERMINAL_AGENT_GROUP_ID_PREFIX",
    "TERMINAL_DEFAULT_COLUMNS",
    "TERMINAL_DEFAULT_ROWS",
    "TERMINAL_FINISHED_GROUP_ID",
    "TERMINAL_FINISHED_TTL",
    "TERMINAL_GROUP_NAME_MAX_CHARS",
    "TERMINAL_INPUT_MAX_CHARS",
    "TERMINAL_INPUT_KEY_SEQUENCES",
    "TERMINAL_MANUAL_GROUP_ID",
    "TERMINAL_MAX_COLUMNS",
    "TERMINAL_MAX_ROWS",
    "TERMINAL_MIN_COLUMNS",
    "TERMINAL_MIN_ROWS",
    "TERMINAL_START_WAIT_SECONDS",
    "TERMINAL_STATUS_DEFAULT_LINES",
    "TERMINAL_STATUS_MAX_LINES",
    "TERMINAL_TEMPORARY_CATEGORY",
    "TerminalAdapter",
    "TerminalAlreadyAttachedError",
    "TerminalCapacityError",
    "TerminalClosedError",
    "TerminalGroup",
    "TerminalInfo",
    "TerminalIsCommandError",
    "TerminalLaunchError",
    "TerminalLaunchHistoryEntry",
    "TerminalManager",
    "TerminalManagerError",
    "TerminalNotFoundError",
    "TerminalNotAttachedError",
    "TerminalObservation",
    "TerminalOwner",
    "TerminalProgramNotRunningError",
    "TerminalRenderHost",
    "TerminalStaleScreenError",
    "TerminalState",
    "WaitEnded",
    "agent_group_id",
]
