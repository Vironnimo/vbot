"""Session-scoped interactive PTY/ConPTY lifecycle and activity management."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Collection,
    Container,
    Iterable,
    Mapping,
    Sequence,
)
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.runs import RunExecutionOwner
from core.sessions import SessionAddress
from core.storage.temp_files import TemporaryFileManager
from core.tools.shell_environment import terminal_environment
from core.tools.terminal_backend import (
    TerminalAdapter,
    TerminalAdapterFactory,
    default_terminal_argv,
    spawn_terminal_adapter,
)
from core.tools.terminal_store import (
    TERMINAL_GROUP_NAME_MAX_CHARS,
    TERMINAL_MANUAL_GROUP_ID,
    TerminalGroup,
    TerminalLaunchHistoryEntry,
    TerminalOperatorStore,
)
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.processes import process_tree_runs

from ._terminal_catalog import (
    TERMINAL_CALL_NAMES,
    SessionViewResolver,
    TerminalCatalog,
    resolve_root_view,
    session_group_id,
)
from ._terminal_launch import shell_launch
from ._terminal_notices import screen_behind_note, unseen_lines_note
from ._terminal_render_host import TerminalRenderHost
from ._terminal_scopes import TerminalScopes
from ._terminal_session import (
    TerminalLaunch,
    TerminalObservation,
    TerminalSession,
    TerminalSessionServices,
)
from ._terminal_shell_state import ShellState
from ._terminal_spawning import TerminalSessionFactory
from ._terminal_state import (
    TERMINAL_ACTIVITY_QUIET_SECONDS,
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_FINISHED_TTL,
    TERMINAL_IDLE_SECONDS,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_LIVE_GLOBAL,
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
    TerminalLaunchError,
    TerminalManagerError,
    TerminalNotAttachedError,
    TerminalNotFoundError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalProgramNotRunningError,
    TerminalSessionView,
    TerminalStaleScreenError,
    TerminalState,
    TerminalStreamEvent,
    WaitEnded,
    _utc_now,
    _validate_owner,
)
from .process_tree import ProcessTreeTracker, observe_process_tree

_LOGGER = get_logger("tools.terminal_manager")

if TYPE_CHECKING:
    from core.computers import ComputerInfo, ComputerRegistry


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
        idle_seconds: float = TERMINAL_IDLE_SECONDS,
        start_wait_seconds: float = TERMINAL_START_WAIT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        program_probe: Callable[[int, str], bool] = process_tree_runs,
        process_observer: ProcessTreeTracker | None = None,
        session_view: SessionViewResolver | None = None,
        reserved_call_names: Callable[[], Iterable[str]] | None = None,
        credential_names: Callable[[], Container[str]] = frozenset,
        computers: ComputerRegistry | None = None,
    ) -> None:
        """*process_observer* watches a program's process tree for work its output
        does not show; without it, only output and declared signals show work.
        *session_view* names the conversation (root Session and title) an Agent
        Session belongs to, so its Terminals show together; *reserved_call_names*
        are names a Terminal's call name must not take, such as Agent names.
        *credential_names* are the names vBot knows as credentials, which the
        environment of a Terminal under Permission Rules withholds."""
        if scrollback_lines < 1:
            raise ValueError("Terminal scrollback cap must be positive")
        if finished_session_ttl <= timedelta(0):
            raise ValueError("Terminal finished-session TTL must be positive")
        if sweep_interval_seconds <= 0:
            raise ValueError("Terminal sweep interval must be positive")
        if activity_quiet_seconds <= 0:
            raise ValueError("Terminal activity quiet period must be positive")
        if idle_seconds < activity_quiet_seconds:
            raise ValueError("Terminal idle period must not be shorter than its quiet period")
        if start_wait_seconds <= 0:
            raise ValueError("Terminal start wait must be positive")
        self._operator_store = TerminalOperatorStore(
            launch_history_path=launch_history_path,
            groups_path=groups_path,
            data_dir=data_dir,
        )
        self.computers = computers
        # A host passed in belongs to the caller, which closes it.
        self._owns_render_host = render_host is None
        self._render_host = render_host or TerminalRenderHost()
        self._finished_session_ttl = finished_session_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._start_wait_seconds = start_wait_seconds
        self._sessions: dict[str, TerminalSession] = {}
        self._pending_spawns: dict[asyncio.Task[TerminalSession], TerminalOwner | None] = {}
        self._pending_execution_spawns: dict[asyncio.Task[TerminalSession], RunExecutionOwner] = {}
        self._scopes = TerminalScopes(
            self._sessions, self._pending_spawns, self._pending_execution_spawns
        )
        self._closed = False
        self._program_probe = program_probe
        self._reader_executor = ThreadPoolExecutor(
            max_workers=TERMINAL_MAX_LIVE_GLOBAL,
            thread_name_prefix="vbot-terminal-read",
        )
        self._catalog = TerminalCatalog(
            self._operator_store,
            lambda: [session.info() for session in self._sessions.values()],
            self._pin,
            session_view=session_view,
            reserved_names=reserved_call_names,
        )
        self._factory = TerminalSessionFactory(
            temporary_files=temporary_files,
            adapter_factory=adapter_factory or spawn_terminal_adapter,
            render_host=self._render_host,
            scrollback_lines=scrollback_lines,
            process_observer=process_observer,
            validate_pin=self._catalog.validate_pin,
            credential_names=credential_names,
            computers=computers,
        )
        self._credential_names = credential_names
        self._services = TerminalSessionServices(
            trigger_service=trigger_service,
            reader_executor=self._reader_executor,
            activity_quiet_seconds=activity_quiet_seconds,
            idle_seconds=idle_seconds,
            monotonic=monotonic,
            sleep=sleep,
            program_probe=program_probe,
            operator_summary=self._catalog.summary,
            changed=self._catalog.notify_changed,
            open_screen=self._factory.open_screen,
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
        """Return the operator's views: conversations, user views, then the
        operator's own terminals."""
        return self._catalog.list_groups_for_operator()

    def list_operator_launch_history(self) -> list[dict[str, Any]]:
        """Return newest-first manual launch configurations for operator reuse."""
        return self._catalog.list_operator_launch_history()

    def create_group_for_operator(self, name: str) -> dict[str, Any]:
        """Create one durable user view with a unique name."""
        return self._catalog.create_group_for_operator(name)

    def rename_group_for_operator(self, group_id: str, name: str) -> dict[str, Any]:
        """Rename one user view; the other views take their names from elsewhere."""
        return self._catalog.rename_group_for_operator(group_id, name)

    def set_group_order_for_operator(self, group_id: str, order: Sequence[str]) -> dict[str, Any]:
        """Persist one user-set Terminal order; missing ids are appended."""
        return self._catalog.set_group_order_for_operator(group_id, order)

    def delete_group_for_operator(self, group_id: str) -> dict[str, Any]:
        """Remove one user view; its Terminals keep running in their own views."""
        return self._catalog.delete_group_for_operator(group_id)

    def move_for_operator(self, terminal_id: str, group_id: str | None) -> dict[str, Any]:
        """Pin one Terminal to a user view or a conversation's view, or unpin it
        (*group_id* ``None``) so it shows where it belongs."""
        session = self._get(terminal_id)
        self._catalog.validate_pin(group_id)
        self._pin(terminal_id, group_id)
        return self._catalog.summary(session.info())

    def forget_session_views(self) -> None:
        """Re-read conversation titles: a Session title changed. Safe from any thread."""
        self._catalog.forget_session_views()

    def _pin(self, terminal_id: str, group_id: str | None) -> None:
        session = self._get(terminal_id)
        if session.group_id == group_id:
            return
        session.group_id = group_id
        self._catalog.pinned(group_id, terminal_id)
        self._catalog.notify_changed(terminal_id)

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
            (session.terminal_id, session, program)
            for session in self._sessions.values()
            if not session.finished and (program := _launch_program(session.launch.launch_command))
        ]
        if not checks:
            return {}

        def run_checks() -> dict[str, bool]:
            return {
                terminal_id: session.program_runs(program)
                for terminal_id, session, program in checks
            }

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

    async def confined_environment(self, overlay: Mapping[str, str] | None) -> dict[str, str]:
        """The environment a Terminal under Permission Rules starts with, before its
        shell integration: what a permission check expands variables in."""
        credential_names = self._credential_names()
        return await asyncio.to_thread(
            partial(terminal_environment, overlay, confined=True, credential_names=credential_names)
        )

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
        group_id: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
        permission_scope: frozenset[str] | None = None,
    ) -> TerminalInfo:
        """Start one Agent-owned program behind PTY/ConPTY with its exact argv; an
        interactive shell also gets shell integration (``_terminal_launch``).

        *permission_scope*: the Permission Rules layers of a restricted Session,
        kept on the terminal; its environment is confined.
        """
        _validate_owner(owner)
        if not argv or not argv[0]:
            raise ValueError("Terminal command must not be empty")
        if any(not isinstance(token, str) for token in argv):
            raise ValueError("Terminal command and arguments must be strings")
        session = await self._spawn(
            owner,
            argv,
            launch=TerminalLaunch(
                command=argv[0],
                arguments=tuple(argv[1:]),
                cwd=cwd,
                permission_scope=permission_scope,
            ),
            env=env,
            columns=columns,
            rows=rows,
            origin_run_id=origin_run_id,
            group_id=group_id,
            execution_owner=execution_owner,
            integrate_shell=True,
        )
        return session.info()

    async def spawn_for_operator(
        self,
        *,
        command: str | None,
        arguments: Sequence[str],
        cwd: Path | None,
        launch_workdir: str | None = None,
        group_id: str | None = None,
        columns: int = TERMINAL_DEFAULT_COLUMNS,
        rows: int = TERMINAL_DEFAULT_ROWS,
        computer_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one manual Terminal Session that behaves like a normal terminal.

        Without a command the interactive shell starts with *arguments*. A
        requested command runs inside that shell, handed over through the
        shell's own start options: the shell loads its profile, runs the
        program, and keeps its prompt after the program ends or is interrupted.
        """
        if computer_id not in (None, "", "server"):
            if self.computers is None:
                raise ValueError("No Desktop computers are available")
            computer = self.computers.get(computer_id)
            session = await self._spawn_remote(
                None,
                computer,
                command=command,
                arguments=arguments,
                workdir=launch_workdir if launch_workdir is not None else str(cwd) if cwd else None,
                columns=columns,
                rows=rows,
                origin_run_id=None,
                group_id=group_id,
                operator=True,
            )
            self._operator_store.remember_launch(
                command=command,
                arguments=tuple(arguments),
                workdir=launch_workdir,
                computer_id=computer.id,
            )
            return self._catalog.summary(session.info())
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
                group_id=group_id,
                integrate_shell=True,
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

    async def spawn_remote(
        self,
        owner: TerminalOwner,
        computer: ComputerInfo,
        *,
        command: str | None,
        arguments: Sequence[str],
        workdir: str | None,
        columns: int,
        rows: int,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None = None,
    ) -> TerminalInfo:
        """Start an Agent program on an explicitly resolved Desktop computer."""
        _validate_owner(owner)
        session = await self._spawn_remote(
            owner,
            computer,
            command=command,
            arguments=arguments,
            workdir=workdir,
            columns=columns,
            rows=rows,
            origin_run_id=origin_run_id,
            execution_owner=execution_owner,
        )
        return session.info()

    async def _spawn_remote(
        self,
        owner: TerminalOwner | None,
        computer: ComputerInfo,
        *,
        command: str | None,
        arguments: Sequence[str],
        workdir: str | None,
        columns: int,
        rows: int,
        origin_run_id: str | None,
        execution_owner: RunExecutionOwner | None = None,
        group_id: str | None = None,
        operator: bool = False,
    ) -> TerminalSession:
        if computer.id == "server":
            raise ValueError("A Desktop computer is required for remote terminal execution")
        if not computer.online or "terminal" not in computer.capabilities:
            raise ValueError("The selected computer is not offering Terminal right now")
        return await self._spawn(
            owner,
            [command or "default shell", *arguments],
            launch=TerminalLaunch(
                command=command or "default shell",
                arguments=tuple(arguments),
                cwd=Path(workdir or "~"),
                launch_command=command if operator else None,
                launch_arguments=tuple(arguments) if operator else (),
                computer_id=computer.id,
                computer_name=computer.name,
                computer_instance_id=computer.instance_id,
            ),
            env=None,
            columns=columns,
            rows=rows,
            origin_run_id=origin_run_id,
            execution_owner=execution_owner,
            group_id=group_id,
            remote={
                "command": command,
                "args": list(arguments),
                "workdir": workdir,
                "operator": operator,
            },
        )

    async def _spawn(
        self,
        owner: TerminalOwner | None,
        argv: Sequence[str],
        *,
        execution_owner: RunExecutionOwner | None = None,
        **kwargs: Any,
    ) -> TerminalSession:
        """Reserve capacity before starting work and retain ownership through cancellation."""
        if self._closed or (self._scopes.closing(owner)):
            raise TerminalClosedError("Terminal Session is no longer running")
        if execution_owner is not None and self._scopes.execution_closed(execution_owner):
            raise TerminalClosedError("Terminal Session is no longer running")
        self._scopes.enforce_capacity(owner)
        task = asyncio.create_task(
            self._spawn_admitted(owner, argv, execution_owner=execution_owner, **kwargs)
        )
        self._pending_spawns[task] = owner
        if execution_owner is not None:
            self._pending_execution_spawns[task] = execution_owner
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
        group_id: str | None = None,
        **options: Any,
    ) -> TerminalSession:
        session = await self._factory.start(
            owner,
            argv,
            group_id=group_id,
            services=self._services,
            can_start=lambda: not self._closed and not (self._scopes.closing(owner)),
            allocate_id=lambda: new_id(
                "term", claim=lambda candidate: candidate not in self._sessions
            ),
            allocate_call_name=self._catalog.allocate_call_name,
            **options,
        )
        if self._closed or (self._scopes.closing(owner)):
            try:
                await session.terminate(suppress_attention=True)
            except Exception:
                # An unconfirmed stop must retain the process and captured
                # descendant identities under the normal retryable owner.
                self._sessions[session.terminal_id] = session
                session.start()
                raise
            session.release()
            raise TerminalClosedError("Terminal Session is no longer running")
        self._catalog.pinned(group_id, session.terminal_id)
        self._sessions[session.terminal_id] = session
        session.start()
        return session

    # Finding and attaching terminals

    def list_terminals(self) -> list[TerminalInfo]:
        """Return all retained Terminal Sessions discoverable for attachment."""
        return sorted(
            (session.info() for session in self._sessions.values()),
            key=lambda info: info.started_at,
        )

    def busy_addresses(self) -> set[SessionAddress]:
        """Sessions whose Agent Terminal is live or waiting for its launch."""
        owners = {owner for owner in self._pending_spawns.values() if owner is not None}
        owners.update(
            session.lifecycle_owner
            for session in self._sessions.values()
            if not session.finished and session.lifecycle_owner is not None
        )
        return {owner.address for owner in owners}

    def terminal(self, terminal_id: str, owner: TerminalOwner) -> TerminalInfo:
        """Return a Terminal Session attached to the exact vBot Session."""
        return self._attached(terminal_id, owner).info()

    def shell_state(self, terminal_id: str, owner: TerminalOwner) -> ShellState | None:
        """The live prompt state of an attached terminal's integrated shell, else None."""
        return self._attached(terminal_id, owner).shell_state

    def attach(
        self,
        terminal_id: str,
        attachment: TerminalOwner,
        *,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None = None,
    ) -> tuple[TerminalInfo, bool]:
        """Attach one live Terminal Session without changing its process or lifecycle."""
        _validate_owner(attachment)
        session = self._get(terminal_id)
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
        """Remove only one exact vBot Session attachment."""
        session = self._get(terminal_id)
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
        """Explicitly stop an operator-selected Terminal Session."""
        session = self._get(terminal_id)
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
        return await session.snapshot(lines=lines, start_line=start_line)

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
        return await self._get(terminal_id).snapshot(lines=lines, start_line=start_line)

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
        """Wait up to *seconds* until the program exits, prints a match, or goes idle.

        The wait also ends (``idle``) once the program went idle showing a new
        screen in an attention revision above *after_revision* (default: the
        last one a durable result showed or a delivery brought); with
        *pattern* that does not end its wait. Output printed before the call counts for
        *pattern*, except output before the Agent's last input and that
        input's echo. *on_match* receives the line where a match starts.
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
        """Wait up to *seconds* until the output pauses after input, or the program exits.

        *after_quiet* is the ``quiet_boundaries`` count ``send_input`` returned.
        Ends ``quiet``, ``exited`` or ``timeout``.
        """
        return await self._attached(terminal_id, owner).wait_for_reply(
            deadline=self._services.monotonic() + seconds, after_quiet=after_quiet
        )

    async def resize(
        self, terminal_id: str, owner: TerminalOwner, *, columns: int, rows: int, seconds: float
    ) -> bool:
        """Resize an attached terminal and wait up to *seconds* until its program
        redrew the screen for the new size; False when it already had that size.

        A WebUI view of the terminal resizes it again when it opens or changes.
        """
        return await self._attached(terminal_id, owner).resize_for_agent(
            columns, rows, deadline=self._services.monotonic() + seconds
        )

    async def release_focus(self, terminal_id: str, owner: TerminalOwner) -> None:
        """The Agent has the reply to its input: a program that asked for focus
        reports learns that the terminal lost the focus."""
        await self._attached(terminal_id, owner).release_focus()

    async def wait_for_startup(self, terminal_id: str) -> None:
        """Wait until a started program shows its first screen, at most the start wait.

        Returns once the program printed something visible and its output then
        paused for the activity quiet period, or once the program ended. A
        program that shows nothing is waited for the whole start wait.
        """
        deadline = self._services.monotonic() + self._start_wait_seconds
        await self._get(terminal_id).wait_started(deadline=deadline)

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
        await self._scopes.close_execution_group(extension, group_id, epoch)

    async def close_scope(self, owner: TerminalOwner) -> int:
        """Stop a Session's live and launching terminals, and detach its other bindings.

        Returns the number of live terminals stopped or detached, including
        pending launches. New launches in this scope are refused until cleanup
        settles; a later Run can start terminals normally.
        """
        return await self.close_scopes((owner,))

    async def close_scopes(self, owners: Collection[TerminalOwner]) -> int:
        """Close a captured Session tree under one fence, counting each terminal once."""
        scopes = frozenset(owners)
        return await self._scopes.close_matching(scopes.__contains__)

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> int:
        """Apply Terminal lifecycle and attachment cleanup for a removed Agent scope."""
        return await self._scopes.close_matching(
            lambda scope: scope.agent_id == agent_id and scope.project_id == project_id
        )

    async def close_project_scope(self, project_id: str) -> int:
        """Apply Terminal lifecycle and attachment cleanup for a removed Project."""
        return await self._scopes.close_matching(lambda scope: scope.project_id == project_id)

    async def transfer_scope(self, source: TerminalOwner, target: TerminalOwner) -> int:
        """Transfer lifecycle and attachment scopes after a successful Session move."""
        await self._scopes.settle_transfers(lambda owner: owner == source)
        return sum(session.transfer(source, target) for session in self._sessions.values())

    async def transfer_agent_scope(self, agent_id: str, new_agent_id: str) -> int:
        """Move every lifecycle and attachment scope of a renamed Identity Agent to its new id.

        Each scope keeps its Session id, and a pending delivery follows the
        attachment; immutable provenance keeps the old id. Returns how many
        Terminal Sessions changed.
        """
        await self._scopes.settle_transfers(
            lambda owner: owner.project_id is None and owner.agent_id == agent_id
        )
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


def _launch_program(command: str | None) -> str:
    """The program a launch command starts: its file name without the extension."""
    if not command or not command.strip():
        return ""
    name = command.strip().strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, _extension = name.rpartition(".")
    return stem if dot and stem else name


__all__ = [
    "AttentionKind",
    "TERMINAL_CALL_NAMES",
    "TERMINAL_DEFAULT_COLUMNS",
    "TERMINAL_DEFAULT_ROWS",
    "TERMINAL_FINISHED_TTL",
    "TERMINAL_GROUP_NAME_MAX_CHARS",
    "TERMINAL_IDLE_SECONDS",
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
    "TerminalSessionView",
    "TerminalStaleScreenError",
    "TerminalState",
    "WaitEnded",
    "observe_process_tree",
    "resolve_root_view",
    "session_group_id",
    "screen_behind_note",
    "unseen_lines_note",
]
