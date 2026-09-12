"""Session-scoped interactive PTY/ConPTY lifecycle and activity management."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO

from core.runs import RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.tools import terminal_backend
from core.tools.bash import get_shell_env, reset_shell_env_cache
from core.tools.process_manager import log_background_task_result
from core.tools.terminal_backend import (
    TerminalAdapter,
    TerminalAdapterFactory,
    TerminalRenderer,
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

from ._terminal_catalog import TerminalCatalog
from ._terminal_events import TerminalEvents
from ._terminal_io import TerminalSessionIO
from ._terminal_state import (
    TERMINAL_ACTIVITY_QUIET_SECONDS,
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_FINISHED_TTL,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_LIVE_GLOBAL,
    TERMINAL_MAX_LIVE_PER_SESSION,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TERMINAL_SCROLLBACK_LINES,
    TERMINAL_STATUS_DEFAULT_LINES,
    TERMINAL_STATUS_MAX_LINES,
    TERMINAL_SWEEP_INTERVAL_SECONDS,
    TERMINAL_TEMPORARY_CATEGORY,
    AttentionKind,
    TerminalAlreadyAttachedError,
    TerminalCapacityError,
    TerminalChangedCallback,
    TerminalClosedError,
    TerminalCursorError,
    TerminalLaunchError,
    TerminalManagerError,
    TerminalNotAttachedError,
    TerminalNotFoundError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalSession,
    TerminalStaleScreenError,
    TerminalState,
    TerminalStreamEvent,
    _finish_files,
    _require_live,
    _utc_now,
    _validate_dimensions,
    _validate_owner,
)

_LOGGER = get_logger("tools.terminal_manager")


class TerminalManager:
    """Own Agent and manually started interactive terminal processes."""

    def __init__(
        self,
        trigger_service: Any | None = None,
        *,
        temporary_files: TemporaryFileManager | None = None,
        launch_history_path: Path | None = None,
        groups_path: Path | None = None,
        data_dir: Path | None = None,
        adapter_factory: TerminalAdapterFactory | None = None,
        scrollback_lines: int = TERMINAL_SCROLLBACK_LINES,
        finished_session_ttl: timedelta = TERMINAL_FINISHED_TTL,
        sweep_interval_seconds: float = TERMINAL_SWEEP_INTERVAL_SECONDS,
        activity_quiet_seconds: float = TERMINAL_ACTIVITY_QUIET_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        self._adapter_factory = adapter_factory or spawn_terminal_adapter
        self._scrollback_lines = scrollback_lines
        self._finished_session_ttl = finished_session_ttl
        self._sweep_interval_seconds = sweep_interval_seconds
        self._sessions: dict[str, TerminalSession] = {}
        self._pending_spawns: dict[asyncio.Task[TerminalSession], TerminalOwner | None] = {}
        self._pending_execution_spawns: dict[asyncio.Task[TerminalSession], RunExecutionOwner] = {}
        self._closed_execution_groups: set[tuple[str, str, str]] = set()
        self._closed = False
        self._reader_executor = ThreadPoolExecutor(
            max_workers=TERMINAL_MAX_LIVE_GLOBAL, thread_name_prefix="vbot-terminal-read"
        )
        self._catalog = TerminalCatalog(
            self._sessions, self._operator_store, self._terminate_session
        )
        self._events = TerminalEvents(self._catalog)
        self._io = TerminalSessionIO(
            self._events,
            self._reader_executor,
            trigger_service,
            activity_quiet_seconds=activity_quiet_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._sweeper_task: asyncio.Task[None] | None = None

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
        failures: list[str] = []
        for session in list(self._sessions.values()):
            session.suppress_exit_attention = True
            self._io._cancel_delivery(session)
            if session.termination_pending or session.state not in {"exited", "error"}:
                session.termination_pending = True
                try:
                    terminal_backend.terminate_process_tree(
                        session.adapter, targets=session.termination_targets
                    )
                except OSError:
                    failures.append(session.terminal_id)
                    continue
                session.termination_pending = False
            session.adapter.close()
            for task in (
                session.reader_task,
                session.initial_input_task,
                session.operator_command_task,
                session.settle_task,
            ):
                if task is not None and not task.done():
                    task.cancel()
            _finish_files(session)
        if failures:
            raise TerminalManagerError(
                "Could not terminate terminal process trees: " + ", ".join(failures)
            )
        self._reader_executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Stop all children and await reader, event, notification, and sweep tasks."""
        sweeper = self._sweeper_task
        failure: TerminalManagerError | None = None
        try:
            self.stop()
        except TerminalManagerError as error:
            failure = error
        tasks: list[asyncio.Task[Any]] = []
        tasks.extend(self._pending_spawns)
        if sweeper is not None and not sweeper.done():
            tasks.append(sweeper)
        for session in self._sessions.values():
            if session.termination_pending:
                continue
            for task in (
                session.reader_task,
                session.initial_input_task,
                session.operator_command_task,
                session.settle_task,
                session.notification_task,
            ):
                if task is not None and not task.done():
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if failure is not None:
            raise failure

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
    ) -> TerminalSession:
        """Start one Agent-owned program behind PTY/ConPTY."""
        _validate_owner(owner)
        return await self._spawn(
            owner,
            argv,
            cwd=cwd,
            env=env,
            columns=columns,
            rows=rows,
            origin_run_id=origin_run_id,
            initial_text=initial_text,
            name=name,
            group_id=group_id,
            execution_owner=execution_owner,
        )

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

        A requested command runs inside the interactive shell instead of as the
        bare PTY child, so the Session keeps a live prompt after the program
        ends, is interrupted, or is stopped. The command is entered through the
        existing initial-input path, which means the exact launch command stays
        available as metadata and the Agent-owned spawn path (exact argv) is
        untouched.
        """
        environment = await get_shell_env()
        if command is None:
            argv = default_terminal_argv(environment)
            argv.extend(arguments)
            launch_command = None
            launch_arguments: tuple[str, ...] = ()
        else:
            argv = default_terminal_argv(environment)
            launch_command = command
            launch_arguments = tuple(arguments)
        session = await self._spawn(
            None,
            argv,
            cwd=cwd or Path.home(),
            env=None,
            columns=columns,
            rows=rows,
            origin_run_id=None,
            name=name,
            launch_command=launch_command,
            launch_arguments=launch_arguments,
            group_id=group_id,
        )
        if launch_command is not None:
            session.operator_command_task = asyncio.create_task(
                self._io._send_operator_command(session),
                name=f"terminal:{session.terminal_id}:operator-command",
            )
            session.operator_command_task.add_done_callback(
                lambda task: log_background_task_result(
                    task, f"Terminal operator command failed for terminal={session.terminal_id}"
                )
            )
        remembered_workdir = launch_workdir
        if remembered_workdir is None and cwd is not None:
            remembered_workdir = str(cwd)
        self._operator_store.remember_launch(
            command=launch_command,
            arguments=launch_arguments,
            workdir=remembered_workdir,
        )
        return self._catalog._operator_summary(session)

    async def _spawn(
        self,
        owner: TerminalOwner | None,
        argv: Sequence[str],
        **kwargs: Any,
    ) -> TerminalSession:
        """Reserve capacity before starting work and retain ownership through cancellation."""
        if self._closed:
            raise TerminalClosedError("Terminal Session is no longer running")
        execution_owner = kwargs.get("execution_owner")
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
        self._enforce_capacity(owner)
        task = asyncio.create_task(self._spawn_admitted(owner, argv, **kwargs))
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
        await self._terminate_session(session, suppress_attention=True)

    async def _spawn_admitted(
        self,
        owner: TerminalOwner | None,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        columns: int,
        rows: int,
        origin_run_id: str | None,
        initial_text: str | None = None,
        name: str | None = None,
        launch_command: str | None = None,
        launch_arguments: tuple[str, ...] = (),
        group_id: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> TerminalSession:
        """Start one unmodified program behind PTY/ConPTY."""
        if not argv or not argv[0]:
            raise ValueError("Terminal command must not be empty")
        if any(not isinstance(token, str) for token in argv):
            raise ValueError("Terminal command and arguments must be strings")
        _validate_dimensions(columns, rows)
        if not cwd.is_dir():
            raise ValueError(f"Terminal workdir is not a directory: {model_path(cwd)}")
        if group_id is not None:
            self._catalog._require_group(group_id)

        log_path: Path | None = None
        log_handle: TextIO | None = None
        log_lease: TemporaryFileLease | None = None
        process_env = await get_shell_env()
        # A service or pipe-based parent may advertise no terminal. The child
        # has a real VT here; preserve only an explicit caller override.
        if process_env.get("TERM") in {None, "", "dumb"}:
            process_env["TERM"] = "xterm-256color"
        if env is not None:
            process_env.update(env)

        try:
            log_path, log_handle, log_lease = self._open_raw_log()
            for attempt in range(2):
                try:
                    adapter = await asyncio.to_thread(
                        self._adapter_factory, list(argv), cwd, process_env, rows, columns
                    )
                    break
                except FileNotFoundError:
                    if attempt:
                        raise
                    reset_shell_env_cache()
                    process_env = await get_shell_env()
                    if process_env.get("TERM") in {None, "", "dumb"}:
                        process_env["TERM"] = "xterm-256color"
                    if env is not None:
                        process_env.update(env)
            if self._closed:
                await asyncio.to_thread(terminal_backend.terminate_process_tree, adapter)
                raise TerminalClosedError("Terminal Session is no longer running")
        except Exception as error:
            if log_handle is not None:
                log_handle.close()
            if log_lease is not None:
                log_lease.finish()
            raise TerminalLaunchError(f"Terminal process could not be started: {error}") from error

        terminal_id = new_id("term", claim=lambda candidate: candidate not in self._sessions)
        session = TerminalSession(
            terminal_id=terminal_id,
            owner=owner,
            lifecycle_owner=owner,
            attachment=owner,
            adapter=adapter,
            renderer=TerminalRenderer(columns, rows, scrollback_lines=self._scrollback_lines),
            command=argv[0],
            arguments=tuple(argv[1:]),
            launch_command=launch_command,
            launch_arguments=launch_arguments,
            name=name,
            group_id=group_id,
            cwd=cwd,
            state="starting" if initial_text is not None else "ready",
            started_at=_utc_now(),
            origin_run_id=origin_run_id,
            activity_origin_run_id=None,
            suppress_until_activity=(owner is not None and initial_text is None),
            log_path=log_path,
            log_handle=log_handle,
            log_lease=log_lease,
            execution_owner=execution_owner,
            activity_execution_owner=execution_owner,
        )
        if group_id is not None:
            self._catalog._append_group_terminal(group_id, terminal_id)
        self._sessions[terminal_id] = session
        session.reader_task = asyncio.create_task(
            self._io._read_terminal(session), name=f"terminal:{terminal_id}:reader"
        )
        session.reader_task.add_done_callback(
            lambda task: log_background_task_result(
                task, f"Terminal reader failed for terminal={terminal_id}"
            )
        )
        if initial_text is not None and origin_run_id is not None:
            session.initial_input_task = asyncio.create_task(
                self._io._send_initial_input_when_ready(
                    session, initial_text, origin_run_id=origin_run_id
                ),
                name=f"terminal:{terminal_id}:initial-input",
            )
            session.initial_input_task.add_done_callback(
                lambda task: log_background_task_result(
                    task, f"Terminal initial input failed for terminal={terminal_id}"
                )
            )
        self._events._publish_state(session)
        return session

    def list_sessions(self) -> list[TerminalSession]:
        """Return all retained Terminal Sessions discoverable for attachment."""
        return sorted(
            self._sessions.values(),
            key=lambda session: session.started_at,
        )

    def get_session(self, terminal_id: str, owner: TerminalOwner) -> TerminalSession:
        """Return a Terminal Session attached to the exact vBot Session."""
        session = self._sessions.get(terminal_id)
        if session is None:
            raise TerminalNotFoundError(f"Terminal Session not found: {terminal_id}")
        if session.attachment != owner:
            raise TerminalNotOwnedError(
                f"Terminal Session is not attached to this vBot Session; attach it first "
                f"(id: {terminal_id})"
            )
        return session

    def attach(
        self,
        terminal_id: str,
        attachment: TerminalOwner,
        *,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None = None,
    ) -> tuple[TerminalSession, bool]:
        """Attach one live Terminal Session without changing its process or lifecycle."""
        _validate_owner(attachment)
        session = self._catalog._get_for_operator(terminal_id)
        _require_live(session)
        if session.attachment is not None and session.attachment != attachment:
            raise TerminalAlreadyAttachedError(
                "Terminal Session is already attached to another vBot Session."
            )
        changed = session.attachment is None
        if changed:
            session.observed_screen = None
        session.attachment = attachment
        session.activity_origin_run_id = origin_run_id
        session.activity_execution_owner = execution_owner
        session.acknowledged_attention_revision = session.attention_revision
        session.settled_delivery_enabled = True
        # An explicit Agent attach is deliberate contact: the Agent sees the
        # current screen in the attach result, so the startup suppression no
        # longer applies and delivery resumes normally.
        session.suppress_until_activity = False
        if session.state == "working":
            self._io._schedule_settle(session, notify=True)
        if changed:
            self._events._publish_state(session)
            _LOGGER.info(
                "Attached Terminal Session terminal=%s agent=%s session=%s project=%s",
                terminal_id,
                attachment.agent_id,
                attachment.session_id,
                attachment.project_id,
            )
        return session, changed

    def detach(self, terminal_id: str, attachment: TerminalOwner) -> TerminalSession:
        """Remove only one exact vBot Session attachment."""
        session = self._catalog._get_for_operator(terminal_id)
        if session.attachment != attachment:
            raise TerminalNotAttachedError("Terminal Session is not attached to this vBot Session.")
        self._io._detach_session(session)
        self._events._publish_state(session)
        _LOGGER.info(
            "Detached Terminal Session terminal=%s agent=%s session=%s project=%s",
            terminal_id,
            attachment.agent_id,
            attachment.session_id,
            attachment.project_id,
        )
        return session

    async def watch_for_operator(
        self, terminal_id: str
    ) -> AsyncGenerator[TerminalStreamEvent, None]:
        """Yield an authoritative VT snapshot followed by sequenced live events."""
        session = self._catalog._get_for_operator(terminal_id)
        async with contextlib.aclosing(self._events.watch_for_operator(session)) as events:
            async for event in events:
                yield event

    def read_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Read a bounded screen without changing any Agent's observation or binding."""
        return self._events.read_for_operator(self._catalog._get_for_operator(terminal_id))

    async def send_operator_input(
        self, terminal_id: str, data: str, *, expected_screen_revision: int | None = None
    ) -> dict[str, Any]:
        """Write exact user-controlled terminal bytes through the existing PTY."""
        if not isinstance(data, str) or not data:
            raise ValueError("Terminal input must be a non-empty string")
        if len(data) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(
                f"Terminal input must not exceed {TERMINAL_INPUT_MAX_CHARS} characters"
            )
        session = self._catalog._get_for_operator(terminal_id)
        await self._io.send_operator_input(
            session, data, expected_screen_revision=expected_screen_revision
        )
        return self._catalog._operator_summary(session)

    async def resize_for_operator(
        self, terminal_id: str, *, columns: int, rows: int
    ) -> dict[str, Any]:
        """Resize an operator-selected Terminal Session."""
        session = self._catalog._get_for_operator(terminal_id)
        await self._io._resize_session(session, columns=columns, rows=rows)
        return self._catalog._operator_summary(session)

    async def kill_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Explicitly stop an operator-selected Terminal Session."""
        session = self._catalog._get_for_operator(terminal_id)
        await self._terminate_session(session, suppress_attention=True)
        return self._catalog._operator_summary(session)

    def forget_for_operator(self, terminal_id: str) -> dict[str, Any]:
        """Remove one finished Terminal Session from the retained operator catalog."""
        session = self._catalog._get_for_operator(terminal_id)
        if session.state not in {"exited", "error"} or session.finished_at is None:
            raise ValueError("A running Terminal Session must be stopped before removal")
        summary = self._catalog._operator_summary(session)
        self._sessions.pop(terminal_id)
        self._io._cancel_delivery(session)
        _finish_files(session)
        self._catalog._notify_changed(terminal_id)
        return summary

    async def snapshot(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        lines: int = TERMINAL_STATUS_DEFAULT_LINES,
        start_line: int | None = None,
        include_name: bool = True,
    ) -> dict[str, Any]:
        """Return one bounded rendered status page.

        ``start_line`` addresses the whole buffer by absolute zero-based line
        (Hermes ``read_terminal`` contract); omit it for the newest page.
        """
        if not 1 <= lines <= TERMINAL_STATUS_MAX_LINES:
            raise ValueError(f"lines must be between 1 and {TERMINAL_STATUS_MAX_LINES}")
        if start_line is not None and start_line < 0:
            raise ValueError("start_line must be a non-negative integer")
        session = self.get_session(terminal_id, owner)
        return await self._events.snapshot(
            session, lines=lines, start_line=start_line, include_name=include_name
        )

    async def wait_for_attention(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        after_revision: int,
        timeout_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        """Wait for a newer attention revision without owning the child lifetime."""
        session = self.get_session(terminal_id, owner)
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        timed_out = False
        while session.attention_revision <= after_revision and session.state not in {
            "exited",
            "error",
        }:
            session.attention_event.clear()
            if session.attention_revision > after_revision:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(session.attention_event.wait(), timeout=remaining)
            except TimeoutError:
                timed_out = True
                break
        return await self.snapshot(terminal_id, owner, include_name=False), timed_out

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
        session = self.get_session(terminal_id, owner)
        return await self._io.send_input(
            session,
            text=text,
            key=key,
            expected_screen_revision=expected_screen_revision,
            origin_run_id=origin_run_id,
            data=data,
            execution_owner=execution_owner,
        )

    async def resize(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        columns: int,
        rows: int,
    ) -> dict[str, Any]:
        """Resize both the host PTY/ConPTY and rendered screen."""
        session = self.get_session(terminal_id, owner)
        return await self._io._resize_session(session, columns=columns, rows=rows)

    async def kill(self, terminal_id: str, owner: TerminalOwner) -> dict[str, Any]:
        """Explicitly terminate one Terminal Session without an automatic exit wakeup."""
        session = self.get_session(terminal_id, owner)
        await self._terminate_session(session, suppress_attention=True)
        return await self.snapshot(terminal_id, owner, include_name=False)

    def has_execution_work(self, owner: RunExecutionOwner) -> bool:
        return any(value == owner for value in self._pending_execution_spawns.values()) or any(
            session.execution_owner == owner and session.state not in {"exited", "error"}
            for session in self._sessions.values()
        )

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        """Drain exact execution-owned terminals without changing attachment authority."""
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
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions)
        )

    async def close_scope(self, owner: TerminalOwner) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Session."""
        sessions = [
            session for session in self._sessions.values() if session.lifecycle_owner == owner
        ]
        for session in self._sessions.values():
            if session.lifecycle_owner != owner and session.attachment == owner:
                self._io._detach_session(session)
                self._events._publish_state(session)
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Agent scope."""
        sessions = [
            session
            for session in self._sessions.values()
            if session.lifecycle_owner is not None
            and session.lifecycle_owner.agent_id == agent_id
            and session.lifecycle_owner.project_id == project_id
        ]
        terminating = {session.terminal_id for session in sessions}
        for session in self._sessions.values():
            attachment = session.attachment
            if (
                session.terminal_id not in terminating
                and attachment is not None
                and attachment.agent_id == agent_id
                and attachment.project_id == project_id
            ):
                self._io._detach_session(session)
                self._events._publish_state(session)
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    async def close_project_scope(self, project_id: str) -> None:
        """Apply Terminal lifecycle and attachment cleanup for a removed Project."""
        sessions = [
            session
            for session in self._sessions.values()
            if session.lifecycle_owner is not None
            and session.lifecycle_owner.project_id == project_id
        ]
        terminating = {session.terminal_id for session in sessions}
        for session in self._sessions.values():
            attachment = session.attachment
            if (
                session.terminal_id not in terminating
                and attachment is not None
                and attachment.project_id == project_id
            ):
                self._io._detach_session(session)
                self._events._publish_state(session)
        await asyncio.gather(
            *(self._terminate_session(session, suppress_attention=True) for session in sessions),
            return_exceptions=False,
        )

    def transfer_scope(self, source: TerminalOwner, target: TerminalOwner) -> int:
        """Transfer lifecycle and attachment scopes after a successful Session move."""
        transferred = 0
        for session in self._sessions.values():
            lifecycle_matches = session.lifecycle_owner == source
            attachment_matches = session.attachment == source
            if lifecycle_matches or attachment_matches:
                attention = session.attention
                pending_delivery = (
                    attachment_matches
                    and attention is not None
                    and session.notification_task is not None
                    and not session.notification_task.done()
                )
                if pending_delivery:
                    self._io._cancel_delivery(session)
                if lifecycle_matches:
                    session.lifecycle_owner = target
                if attachment_matches:
                    session.attachment = target
                self._events._publish_state(session)
                if pending_delivery and attention is not None:
                    self._io._schedule_attention_delivery(session, attention)
                transferred += 1
        return transferred

    def acknowledge_screen(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        *,
        screen_revision: int,
        columns: int,
        rows: int,
    ) -> bool:
        """Remember a durably delivered screen without consuming later resizes."""
        session = self._sessions.get(terminal_id)
        if session is None or session.attachment != owner:
            return False
        observed = session.observed_screen
        if observed is None or screen_revision >= observed[0]:
            session.observed_screen = (screen_revision, columns, rows)
        return True

    def acknowledge_attention(
        self,
        terminal_id: str,
        owner: TerminalOwner,
        revision: int,
    ) -> None:
        """Cancel equivalent automatic delivery after a manual result is durable."""
        session = self.get_session(terminal_id, owner)
        attention = session.attention
        if attention is None or attention.revision != revision:
            return
        session.acknowledged_attention_revision = max(
            session.acknowledged_attention_revision, revision
        )
        if attention.details.get("screen_revision") == session.renderer.revision:
            session.settled_screen_signature = session.renderer.screen_signature()
        self._io._cancel_delivery(session)

    async def sweep_finished(self) -> None:
        """Forget terminal metadata after the bounded inspection window."""
        cutoff = _utc_now() - self._finished_session_ttl
        expired = [
            terminal_id
            for terminal_id, session in self._sessions.items()
            if session.finished_at is not None and session.finished_at < cutoff
        ]
        for terminal_id in expired:
            session = self._sessions.pop(terminal_id)
            self._io._cancel_delivery(session)
            _finish_files(session)
            self._catalog._notify_changed(terminal_id)

    async def _terminate_session(
        self, session: TerminalSession, *, suppress_attention: bool
    ) -> None:
        await self._io._terminate_session(session, suppress_attention=suppress_attention)

    def _enforce_capacity(self, owner: TerminalOwner | None) -> None:
        pending = [owner for task, owner in self._pending_spawns.items() if not task.done()]
        live = [
            session
            for session in self._sessions.values()
            if session.state not in {"exited", "error"}
        ]
        if len(live) + len(pending) >= TERMINAL_MAX_LIVE_GLOBAL:
            raise TerminalCapacityError(
                f"Live Terminal Session limit reached ({TERMINAL_MAX_LIVE_GLOBAL})"
            )
        owned = sum(1 for session in live if owner is not None and session.lifecycle_owner == owner)
        owned += sum(1 for pending_owner in pending if owner is not None and pending_owner == owner)
        if owner is not None and owned >= TERMINAL_MAX_LIVE_PER_SESSION:
            raise TerminalCapacityError(
                "Live Terminal Session limit reached for this vBot Session "
                f"({TERMINAL_MAX_LIVE_PER_SESSION})"
            )

    def _open_raw_log(
        self,
    ) -> tuple[Path | None, TextIO | None, TemporaryFileLease | None]:
        if self._temporary_files is None:
            return None, None, None
        lease = self._temporary_files.create(TERMINAL_TEMPORARY_CATEGORY, ".log")
        return lease.path, lease.path.open("a", encoding="utf-8", newline=""), lease

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_finished()
        except asyncio.CancelledError:
            return


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
    "TERMINAL_STATUS_DEFAULT_LINES",
    "TERMINAL_STATUS_MAX_LINES",
    "TERMINAL_TEMPORARY_CATEGORY",
    "TerminalAdapter",
    "TerminalAlreadyAttachedError",
    "TerminalCapacityError",
    "TerminalClosedError",
    "TerminalCursorError",
    "TerminalGroup",
    "TerminalLaunchError",
    "TerminalLaunchHistoryEntry",
    "TerminalManager",
    "TerminalManagerError",
    "TerminalNotFoundError",
    "TerminalNotAttachedError",
    "TerminalOwner",
    "TerminalSession",
    "TerminalStaleScreenError",
    "TerminalState",
    "agent_group_id",
]
