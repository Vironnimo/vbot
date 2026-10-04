"""One interactive Terminal Session: process I/O, screen, activity and delivery."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease
from core.tools import terminal_backend
from core.tools.process_manager import log_background_task_result
from core.tools.terminal_backend import TerminalAdapter
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._terminal_activity import QuietRestart, TerminalActivity
from ._terminal_command import (
    COMMAND_IDLE_CPU_SECONDS,
    COMMAND_STOP_GRACE_SECONDS,
    COMMAND_TREE_POLL_SECONDS,
    CommandReport,
    CommandState,
    StopReason,
)
from ._terminal_input import input_chunks
from ._terminal_render_host import TerminalScreen
from ._terminal_state import (
    TERMINAL_DELIVERY_TAIL_LINES,
    TERMINAL_INITIAL_INPUT_QUIET_SECONDS,
    TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS,
    TERMINAL_INPUT_KEY_DELAY_SECONDS,
    TERMINAL_NOTICE_MESSAGE_CAP_CHARS,
    TERMINAL_REPAINT_WINDOW_SECONDS,
    AttentionKind,
    TerminalAlreadyAttachedError,
    TerminalAttention,
    TerminalClosedError,
    TerminalInfo,
    TerminalManagerError,
    TerminalOwner,
    TerminalProgramNotRunningError,
    TerminalStaleScreenError,
    TerminalStreamEvent,
    _attention_data,
    _new_terminal_stream,
    _utc_now,
    _validate_dimensions,
)

_LOGGER = get_logger("tools.terminal_manager")
_READ_CHUNK_CHARS = 4096
_OPERATOR_READ_HISTORY_LINES = 30
# A command's shell is checked this often even while output keeps arriving.
_COMMAND_LIVENESS_SECONDS = 0.5
# After the shell exited, its last output is read for at most this long.
_COMMAND_DRAIN_SECONDS = 1.0
# A command's progress tail is published at most this often.
_COMMAND_PROGRESS_SECONDS = 0.5
_COMMAND_PROGRESS_LINES = 40
# Output must pause this long before the idle CPU baseline is taken.
_COMMAND_IDLE_BASELINE_SECONDS = 1.0

CommandWaitOutcome = Literal["exited", "deadline", "idle"]


@dataclass(frozen=True, slots=True)
class TerminalSessionServices:
    """What every Terminal Session of one manager shares."""

    trigger_service: Any | None
    reader_executor: ThreadPoolExecutor
    activity_quiet_seconds: float
    monotonic: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    program_probe: Callable[[int, str], bool]
    # The operator summary of a Terminal (owned by the catalog, which knows groups).
    operator_summary: Callable[[TerminalInfo], dict[str, Any]]
    # Operator-visible Terminal state changed.
    changed: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class TerminalObservation:
    """The screen a screen-bearing result showed, remembered once it is durable."""

    screen_revision: int
    resize_count: int
    columns: int
    rows: int


@dataclass(frozen=True, slots=True)
class TerminalLaunch:
    """How the Session's process was started, as callers see it."""

    command: str
    arguments: tuple[str, ...]
    cwd: Path
    # A manual start that runs a program inside the interactive shell.
    launch_command: str | None = None
    launch_arguments: tuple[str, ...] = ()


class TerminalSession:
    """Own one terminal process: its output, rendered screen, activity and attention.

    The screen revision names the content an observation saw: program output
    that may change the screen and real input advance it; a resize and the
    program's redraw for the new size do not, so input guarded with the
    revision of a delivered screen still applies after an operator resize.
    """

    def __init__(
        self,
        terminal_id: str,
        *,
        owner: TerminalOwner | None,
        adapter: TerminalAdapter,
        screen: TerminalScreen,
        launch: TerminalLaunch,
        name: str | None,
        group_id: str | None,
        origin_run_id: str | None,
        execution_owner: RunExecutionOwner | None,
        awaiting_initial_input: bool,
        log_path: Path | None,
        log_handle: TextIO | None,
        log_lease: TemporaryFileLease | None,
        cleanup: Callable[[], None] | None,
        services: TerminalSessionServices,
        command: CommandState | None = None,
    ) -> None:
        self.terminal_id = terminal_id
        self.owner = owner
        self.lifecycle_owner = owner
        self.attachment = owner
        self.name = name
        self.group_id = group_id
        self.origin_run_id = origin_run_id
        self.execution_owner = execution_owner
        self.started_at = _utc_now()
        self.finished_at: datetime | None = None
        self.exit_code: int | None = None
        self._launch = launch
        self._adapter = adapter
        self._screen = screen
        self._services = services
        self._lock = asyncio.Lock()
        self._command = command
        self._activity = TerminalActivity(
            awaiting_initial_input=awaiting_initial_input,
            startup_silence=owner is not None and not awaiting_initial_input,
            repaint_window=TERMINAL_REPAINT_WINDOW_SECONDS,
            # A command's output is its result, not a reason to wake the Agent.
            output_wakes=command is None,
        )
        self._title = ""
        self._bracketed_paste = False
        self._alternate_screen = False
        self._screen_revision = 0
        self._resize_count = 0
        # Revision and signature of the screen at the last quiet boundary.
        self._settled: tuple[int, str] | None = None
        self._observed: TerminalObservation | None = None
        self._output_count = 0
        self._snapshot_on_settle = False
        self._attention: TerminalAttention | None = None
        self._attention_body = ""
        self._attention_revision = 0
        self._acknowledged_attention_revision = 0
        self._activity_origin_run_id: str | None = None
        self._activity_execution_owner = execution_owner
        self._suppress_exit_attention = False
        self._termination_pending = False
        self._termination_targets: list[Any] = []
        self._output_event = asyncio.Event()
        self._attention_event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._initial_input_task: asyncio.Task[None] | None = None
        self._settle_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._stream_sequence = 0
        self._stream = _new_terminal_stream()
        self._log_path = log_path
        self._log_handle = log_handle
        self._log_lease = log_lease
        self._cleanup = cleanup
        self._timeout_task: asyncio.Task[None] | None = None
        self._last_output_at = services.monotonic()
        self._shell_dead_at: float | None = None
        # Set once vBot killed the command's process tree.
        self._tree_killed = asyncio.Event()
        self._next_liveness_check = 0.0
        self._next_tree_poll = 0.0

    # Facts

    @property
    def state(self) -> str:
        return self._activity.state

    @property
    def finished(self) -> bool:
        return self._activity.finished

    @property
    def pid(self) -> int:
        return self._adapter.pid

    @property
    def launch(self) -> TerminalLaunch:
        return self._launch

    @property
    def command(self) -> CommandState | None:
        return self._command

    @property
    def hidden(self) -> bool:
        """A command still running in the foreground of its Tool call is not listed."""
        return self._command is not None and self._command.hidden

    def info(self) -> TerminalInfo:
        return TerminalInfo(
            terminal_id=self.terminal_id,
            owner=self.owner,
            lifecycle_owner=self.lifecycle_owner,
            attachment=self.attachment,
            state=self._activity.state,
            command=self._launch.command,
            arguments=self._launch.arguments,
            launch_command=self._launch.launch_command,
            launch_arguments=self._launch.launch_arguments,
            name=self.name,
            group_id=self.group_id,
            cwd=self._launch.cwd,
            pid=self._adapter.pid,
            started_at=self.started_at,
            finished_at=self.finished_at,
            exit_code=self.exit_code,
            title=self._title,
            columns=self._screen.columns,
            rows=self._screen.rows,
            alternate_screen=self._alternate_screen,
            screen_revision=self._screen_revision,
            attention=self._attention,
            attention_revision=self._attention_revision,
            acknowledged_attention_revision=self._acknowledged_attention_revision,
            log_path=self._log_path if self._command is None else self._command.log_path,
            kind="terminal" if self._command is None else "command",
            hidden=self.hidden,
        )

    # Lifecycle

    def start(self, *, initial_text: str | None) -> None:
        """Start reading output and, for an Agent start with text, its first input."""
        self._reader_task = self._background(self._read(), "reader")
        command = self._command
        if command is not None and command.timeout_seconds is not None:
            self._timeout_task = self._background(
                self._stop_after(command.timeout_seconds), "timeout"
            )
        if initial_text is not None and self.origin_run_id is not None:
            self._initial_input_task = self._background(
                self._send_initial_input(initial_text, origin_run_id=self.origin_run_id),
                "initial-input",
            )
        self._publish_state()

    async def terminate(
        self, *, suppress_attention: bool, reason: StopReason | None = None
    ) -> None:
        """Stop the process tree; a failure leaves the Session unfinished for a retry.

        For a command, *reason* records why vBot stopped it (the Agent's own
        kill is ``agent``).
        """
        self._cancel_task(self._initial_input_task)
        if self._command is not None:
            self._command.request_stop(reason or "agent")
        if suppress_attention:
            self._suppress_exit_attention = True
            self._cancel_delivery()
        self._cancel_task(self._settle_task)
        if self._termination_pending or not self.finished:
            self._termination_pending = True
            try:
                await asyncio.to_thread(self._kill_tree)
            except OSError as error:
                raise TerminalManagerError(
                    f"Could not terminate terminal {self.terminal_id}; "
                    "its process tree may still be running. Retry the kill operation."
                ) from error
            self._termination_pending = False
            self._tree_killed.set()
        await asyncio.to_thread(self._adapter.close)
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=5)
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        if not self.finished:
            await self._mark_finished(None)

    def stop_now(self) -> bool:
        """Synchronously stop the process for Runtime shutdown; False if the tree survived."""
        self._suppress_exit_attention = True
        self._cancel_delivery()
        if self._command is not None:
            self._command.request_stop("shutdown")
        if self._termination_pending or not self.finished:
            self._termination_pending = True
            try:
                self._kill_tree()
            except OSError:
                return False
            self._termination_pending = False
        self._adapter.close()
        for task in (
            self._reader_task,
            self._initial_input_task,
            self._settle_task,
            self._timeout_task,
        ):
            self._cancel_task(task)
        self._finish_files()
        return True

    def _kill_tree(self) -> None:
        """Kill the process tree (blocking); OSError when that is not confirmed."""
        tree = self._command.tree if self._command is not None else None
        if tree is not None:
            tree.terminate()
            return
        terminal_backend.terminate_process_tree(self._adapter, targets=self._termination_targets)

    def pending_tasks(self) -> list[asyncio.Task[Any]]:
        """Background tasks to await after ``stop_now``."""
        if self._termination_pending:
            return []
        return [
            task
            for task in (
                self._reader_task,
                self._initial_input_task,
                self._settle_task,
                self._delivery_task,
                self._timeout_task,
            )
            if task is not None and not task.done()
        ]

    def release(self) -> None:
        """Drop a finished Session's retained resources when it leaves the catalog."""
        self._cancel_delivery()
        self._finish_files()
        self._screen.close()

    # Commands

    async def wait_command(
        self,
        *,
        deadline: float | None,
        idle_seconds: float | None,
        progress: Callable[[str], Awaitable[None]] | None,
    ) -> CommandWaitOutcome:
        """Wait until the command's shell exits, *deadline* passes or it turns idle.

        *deadline* is a ``monotonic`` time. Idle means no output and less
        than a trace of CPU time in the whole process tree for *idle_seconds*.
        *progress* receives the screen's newest rows while output arrives.
        """
        command = self._require_command()
        services = self._services
        published_output = -1
        next_progress = 0.0
        while not command.shell_exited:
            now = services.monotonic()
            if deadline is not None and now >= deadline:
                return "deadline"
            if idle_seconds is not None and await self._command_idle(command, idle_seconds):
                return "idle"
            if (
                progress is not None
                and self._output_count != published_output
                and now >= next_progress
            ):
                published_output = self._output_count
                next_progress = now + _COMMAND_PROGRESS_SECONDS
                async with self._lock:
                    tail = (await self._screen.observe(_COMMAND_PROGRESS_LINES)).tail
                await progress(tail)
            pause = _COMMAND_PROGRESS_SECONDS
            if deadline is not None:
                pause = min(pause, max(0.0, deadline - now))
            await self._wait_or_sleep(command.exited, pause)
        return "exited"

    async def _command_idle(self, command: CommandState, idle_seconds: float) -> bool:
        now = self._services.monotonic()
        if command.quiet_output_count != self._output_count:
            # New output ends the quiet period; its CPU baseline is taken once
            # output has paused for a moment.
            command.quiet_output_count = self._output_count
            command.quiet_since = None
            return False
        if command.quiet_since is None:
            if now - self._last_output_at < _COMMAND_IDLE_BASELINE_SECONDS:
                return False
            facts = await asyncio.to_thread(command.tree_facts)
            if facts is None:
                return False
            command.quiet_since = now
            command.quiet_cpu_seconds = facts.cpu_seconds
            command.quiet_started_processes = facts.started
            return False
        if now - command.quiet_since < idle_seconds:
            return False
        facts = await asyncio.to_thread(command.tree_facts)
        if facts is None:
            return False
        if (
            facts.cpu_seconds - command.quiet_cpu_seconds > COMMAND_IDLE_CPU_SECONDS
            or facts.started != command.quiet_started_processes
        ):
            command.quiet_since = now
            command.quiet_cpu_seconds = facts.cpu_seconds
            command.quiet_started_processes = facts.started
            return False
        return True

    async def stop_command(self, reason: StopReason) -> None:
        """Interrupt the command with Ctrl+C, then kill every process still running."""
        command = self._require_command()
        if self.finished:
            return
        command.request_stop(reason)
        if not command.shell_exited:
            with contextlib.suppress(EOFError, OSError):
                await asyncio.to_thread(self._adapter.write, "\x03")
            await self._wait_or_sleep(command.exited, COMMAND_STOP_GRACE_SECONDS)
        await self.terminate(suppress_attention=False, reason=reason)

    async def _wait_or_sleep(self, event: asyncio.Event, seconds: float) -> None:
        """Wait until *event* is set or *seconds* pass on the session clock."""
        if event.is_set():
            return
        happened = asyncio.ensure_future(event.wait())
        pause = asyncio.ensure_future(self._services.sleep(seconds))
        try:
            await asyncio.wait({happened, pause}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (happened, pause):
                waiter.cancel()
            await asyncio.gather(happened, pause, return_exceptions=True)

    def hand_off(self, *, deliver: bool) -> None:
        """List the running command; with *deliver*, its result is delivered when it ends."""
        command = self._require_command()
        command.hidden = False
        command.delivers_result = deliver
        self._publish_state()

    def command_report(self) -> CommandReport:
        command = self._require_command()
        return command.report(self.terminal_id, self._services.monotonic())

    async def command_screen(self, lines: int) -> str:
        """The screen's newest non-blank rows."""
        async with self._lock:
            return (await self._screen.observe(lines)).tail

    def _require_command(self) -> CommandState:
        if self._command is None:
            raise TerminalManagerError(f"Terminal {self.terminal_id} does not run a command")
        return self._command

    async def _stop_after(self, seconds: float) -> None:
        try:
            await self._services.sleep(seconds)
            await self.stop_command("timeout")
        except asyncio.CancelledError:
            return

    async def _command_alive(self, *, read_timed_out: bool) -> bool:
        """Whether the command session still runs; records the shell's exit once."""
        command = self._require_command()
        now = self._services.monotonic()
        if not command.shell_exited:
            if self._shell_dead_at is None:
                if not read_timed_out and now < self._next_liveness_check:
                    return True
                self._next_liveness_check = now + _COMMAND_LIVENESS_SECONDS
                if await asyncio.to_thread(self._adapter.is_alive):
                    return True
                self._shell_dead_at = now
            # Read the shell's last output until it pauses, for a bounded time.
            if not read_timed_out and now - self._shell_dead_at < _COMMAND_DRAIN_SECONDS:
                return True
            async with self._lock:
                await self._record_shell_exit()
        if now < self._next_tree_poll:
            return True
        self._next_tree_poll = now + COMMAND_TREE_POLL_SECONDS
        facts = await asyncio.to_thread(command.tree_facts)
        return facts is not None and bool(facts.running)

    def _shell_exit_code(self) -> int | None:
        # A liveness check makes a stopped Windows shell's exit status known.
        with contextlib.suppress(Exception):
            self._adapter.is_alive()
        try:
            return self._adapter.exit_code()
        except Exception:
            return None

    async def _await_tree_end(self) -> None:
        """Wait while processes the command started still run without the terminal."""
        command = self._command
        if command is None:
            return
        while True:
            facts = await asyncio.to_thread(command.tree_facts)
            if facts is None or not facts.running or self._tree_killed.is_set():
                return
            await self._wait_or_sleep(self._tree_killed, COMMAND_TREE_POLL_SECONDS)

    async def _record_shell_exit(self) -> None:
        """Fix the command's outcome when its shell exits; the lock is held."""
        command = self._require_command()
        if command.shell_exited:
            return
        self._cancel_task(self._timeout_task)
        exit_code = await asyncio.to_thread(self._shell_exit_code)
        try:
            command.add_lines(await self._screen.commit_transcript())
        except Exception:
            _LOGGER.warning(
                "Could not render the final output of command terminal=%s",
                self.terminal_id,
                exc_info=True,
            )
        facts = await asyncio.to_thread(command.tree_facts)
        command.record_exit(exit_code, facts, self._services.monotonic())
        self.exit_code = exit_code
        report = command.report(self.terminal_id, self._services.monotonic())
        reason = command.stop_reason
        summary = (
            f"Command exited with code {exit_code}."
            if reason is None
            else f"Command stopped ({reason})."
        )
        self._set_attention(
            kind="exited",
            summary=summary,
            details={"exit_code": exit_code, "stop_reason": reason},
            deliver=command.delivers_result
            and not self._suppress_exit_attention
            and reason != "agent",
            body=command.formatter(report),
        )
        # The session's own end later adds no second attention.
        self._suppress_exit_attention = True

    # Attachment

    def attach(
        self,
        attachment: TerminalOwner,
        *,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None,
    ) -> bool:
        """Bind this Session to one Agent Session; True when the binding is new."""
        self._require_live()
        if self.attachment is not None and self.attachment != attachment:
            raise TerminalAlreadyAttachedError(
                "Terminal Session is already attached to another vBot Session."
            )
        changed = self.attachment is None
        if changed:
            self._observed = None
        self.attachment = attachment
        self._activity_origin_run_id = origin_run_id
        self._activity_execution_owner = execution_owner
        self._acknowledged_attention_revision = self._attention_revision
        restart = self._activity.attach(delivery_pending=self._delivery_pending())
        if restart is not None:
            self._restart_quiet(restart)
        if changed:
            self._publish_state()
        return changed

    def detach(self) -> None:
        """Clear the attachment and its delivery state without touching the process."""
        self._cancel_delivery()
        self.attachment = None
        self._activity_origin_run_id = None
        self._activity_execution_owner = None
        self._activity.detach()
        self._observed = None
        self._publish_state()

    def transfer(self, source: TerminalOwner, target: TerminalOwner) -> bool:
        """Move matching lifecycle and attachment scopes; reroute a pending delivery."""
        lifecycle_matches = self.lifecycle_owner == source
        attachment_matches = self.attachment == source
        if not lifecycle_matches and not attachment_matches:
            return False
        attention = self._attention
        pending = attachment_matches and attention is not None and self._delivery_running()
        if pending:
            self._cancel_delivery()
        if lifecycle_matches:
            self.lifecycle_owner = target
        if attachment_matches:
            self.attachment = target
        self._publish_state()
        if pending and attention is not None:
            self._schedule_delivery(attention, self._attention_body)
        return True

    # Input and size

    async def send_input(
        self,
        *,
        text: str | None,
        key: str | None,
        expected_screen_revision: int | None,
        origin_run_id: str,
        data: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> dict[str, Any]:
        """Write exact data or named terminal input from the attached Agent."""
        write_error: BaseException | None = None
        async with self._lock:
            self._require_live()
            self._check_revision(expected_screen_revision)
            bracketed_paste = (
                text is not None and ("\n" in text or "\r" in text) and self._bracketed_paste
            )
            chunks = input_chunks(data=data, text=text, key=key, bracketed_paste=bracketed_paste)
            if not chunks:
                # An empty write changes no activity, observation or queued input.
                return self._input_result(0, key, bracketed_paste=False, superseded=None)
            self._cancel_task(self._initial_input_task)
            superseded = self._attention_revision if self._attention is not None else None
            self._activity_origin_run_id = origin_run_id
            self._activity_execution_owner = execution_owner
            # Input invalidates an observation even when the program does
            # not echo it (for example, a password prompt).
            self._screen_revision += 1
            restart, changed = self._activity.input(
                notify=True, delivery_pending=self._delivery_pending()
            )
            for index, chunk in enumerate(chunks):
                if index:
                    await asyncio.sleep(TERMINAL_INPUT_KEY_DELAY_SECONDS)
                try:
                    await asyncio.to_thread(self._adapter.write, chunk)
                except (EOFError, OSError) as error:
                    write_error = error
                    break
            if write_error is None:
                self._restart_quiet(restart)
                self._output_event.set()
                if changed:
                    self._publish_state()
                return self._input_result(
                    sum(len(chunk) for chunk in chunks),
                    key,
                    bracketed_paste=bracketed_paste,
                    superseded=superseded,
                )
        await self._mark_finished(None)
        raise TerminalClosedError("Terminal Session is no longer running") from write_error

    async def send_operator_input(
        self,
        data: str,
        *,
        expected_screen_revision: int | None = None,
        expected_program: str | None = None,
    ) -> None:
        """Write exact operator bytes; *expected_program* must run in the process tree."""
        write_error: BaseException | None = None
        async with self._lock:
            self._require_live()
            # Output waits for the lock, so the checks below see the screen
            # and process the input reaches.
            if expected_program is not None and not await asyncio.to_thread(
                self._services.program_probe, self._adapter.pid, expected_program
            ):
                raise TerminalProgramNotRunningError(
                    f"{expected_program} is not running in this Terminal; the input was not written"
                )
            self._check_revision(expected_screen_revision)
            self._cancel_task(self._initial_input_task)
            self._screen_revision += 1
            restart, changed = self._activity.input(
                notify=self.attachment is not None, delivery_pending=self._delivery_pending()
            )
            try:
                await asyncio.to_thread(self._adapter.write, data)
            except (EOFError, OSError) as error:
                write_error = error
            else:
                self._restart_quiet(restart)
                self._output_event.set()
                if changed:
                    self._publish_state()
                return
        await self._mark_finished(None)
        raise TerminalClosedError("Terminal Session is no longer running") from write_error

    async def resize(self, columns: int, rows: int) -> dict[str, Any]:
        """Resize the PTY and the rendered screen together."""
        _validate_dimensions(columns, rows)
        async with self._lock:
            self._require_live()
            if (columns, rows) != (self._screen.columns, self._screen.rows):
                # A resize to the current dimensions would only make the
                # program repaint, so it is skipped.
                await asyncio.to_thread(self._adapter.resize, rows, columns)
                await self._screen.resize(columns, rows)
                self._resize_count += 1
                self._activity.resize(self._services.monotonic())
                self._publish_state()
            return {
                "terminal_id": self.terminal_id,
                "state": self.state,
                "columns": columns,
                "rows": rows,
                "screen_revision": self._screen_revision,
            }

    # Observation

    async def snapshot(
        self, *, lines: int, start_line: int | None, include_name: bool
    ) -> dict[str, Any]:
        """Return the screen, one history page and the facts an Agent result shows."""
        async with self._lock:
            page = await self._screen.page(start_line=start_line, limit=lines)
            screen = await self._screen.screen_text()
            info = self.info()
        data: dict[str, Any] = {
            "terminal_id": self.terminal_id,
            "state": info.state,
            "command": info.command,
            "title": info.title,
            "arguments": list(info.arguments),
            "workdir": model_path(info.cwd),
            "exit_code": info.exit_code,
            "started_at": info.started_at.isoformat(),
            "finished_at": info.finished_at.isoformat() if info.finished_at else None,
            "columns": info.columns,
            "rows": info.rows,
            "screen_revision": info.screen_revision,
            "attention_revision": info.attention_revision,
            "alternate_screen": info.alternate_screen,
            "screen": screen,
            "scrollback": page,
            "attention": _attention_data(info.attention),
            "log_file": model_path(info.log_path) if info.log_path is not None else None,
            "observation": TerminalObservation(
                info.screen_revision, self._resize_count, info.columns, info.rows
            ),
        }
        observed = self._observed
        if observed is not None and observed.resize_count < self._resize_count:
            data["size_change"] = {
                "previous_columns": observed.columns,
                "previous_rows": observed.rows,
                "notice": (
                    "The terminal was resized since your previous screen result. "
                    f"Previous size: {observed.columns} columns x {observed.rows} rows. "
                    f"Current size: {info.columns} columns x {info.rows} rows. "
                    "Use positions from the current screen."
                ),
            }
        if include_name:
            data["name"] = info.name
        command = self._command
        if command is not None:
            report = command.report(self.terminal_id, self._services.monotonic())
            data["command_outcome"] = {
                "exited": report.exited,
                "stop_reason": report.stop_reason,
                "nonzero_exits": list(report.nonzero_exits),
                "still_running": [
                    {"pid": process.pid, "name": process.name} for process in report.still_running
                ],
            }
        return data

    def acknowledge_screen(self, observation: TerminalObservation) -> None:
        """Remember a durably delivered screen without consuming a later resize."""
        observed = self._observed
        if observed is None or (observation.resize_count, observation.screen_revision) >= (
            observed.resize_count,
            observed.screen_revision,
        ):
            self._observed = observation

    def acknowledge_attention(self, revision: int) -> None:
        """Cancel the equivalent automatic delivery after a manual result is durable."""
        attention = self._attention
        if attention is None or attention.revision != revision:
            return
        self._acknowledged_attention_revision = max(self._acknowledged_attention_revision, revision)
        settled = self._settled
        if (
            attention.details.get("screen_revision") == self._screen_revision
            and settled is not None
            and settled[0] == self._screen_revision
        ):
            self._activity.acknowledge(settled[1])
        self._cancel_delivery()

    async def wait_for_attention(self, *, after_revision: int, timeout_ms: int) -> bool:
        """Wait for an attention revision above *after_revision*; True when it timed out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while self._attention_revision <= after_revision and not self.finished:
            self._attention_event.clear()
            if self._attention_revision > after_revision:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(self._attention_event.wait(), timeout=remaining)
            except TimeoutError:
                return True
        return False

    async def read_for_operator(self) -> dict[str, Any]:
        """Read the screen without changing any Agent's observation or binding."""
        async with self._lock:
            screen = await self._screen.screen_text()
            page = await self._screen.page(start_line=None, limit=_OPERATOR_READ_HISTORY_LINES)
            info = self.info()
        return {
            "terminal": self._services.operator_summary(info),
            "screen": screen,
            "scrollback": page,
            "bracketed_paste": self._bracketed_paste,
        }

    async def watch(self) -> AsyncGenerator[TerminalStreamEvent]:
        """Yield an authoritative VT snapshot followed by sequenced live events."""
        async with self._lock:
            after_sequence = self._stream_sequence
            ready: TerminalStreamEvent = {
                "type": "terminal_ready",
                "sequence": after_sequence,
                "terminal": self._services.operator_summary(self.info()),
                "ansi": await self._screen.ansi_snapshot(),
            }
        yield ready
        if self.finished:
            return
        async with contextlib.aclosing(
            self._stream.subscribe(after_sequence=after_sequence)
        ) as events:
            async for event in events:
                yield event

    async def runs_program(self, program: str) -> bool:
        """Whether *program* runs in this Session's process tree."""
        return await asyncio.to_thread(self._services.program_probe, self._adapter.pid, program)

    # Output and activity

    async def _read(self) -> None:
        loop = asyncio.get_running_loop()
        error: BaseException | None = None
        command = self._command is not None
        try:
            while True:
                try:
                    text = await loop.run_in_executor(
                        self._services.reader_executor, self._adapter.read, _READ_CHUNK_CHARS
                    )
                except TimeoutError:
                    if command:
                        if not await self._command_alive(read_timed_out=True):
                            break
                    elif not await asyncio.to_thread(self._adapter.is_alive):
                        break
                    continue
                if text:
                    async with self._lock:
                        await self._render_output(text)
                if command and not await self._command_alive(read_timed_out=False):
                    break
        except asyncio.CancelledError:
            raise
        except EOFError, OSError:
            pass
        except BaseException as caught:
            error = caught
        finally:
            if command and not self._termination_pending:
                # Output ended (POSIX: every holder of the terminal closed it);
                # processes the command left behind may still run without it.
                with contextlib.suppress(Exception):
                    async with self._lock:
                        await self._record_shell_exit()
                    await self._await_tree_end()
            await self._mark_finished(error)

    async def _render_output(self, text: str) -> None:
        if self._log_handle is not None:
            self._log_handle.write(text)
            self._log_handle.flush()
        # Viewers get the bytes first; the snapshot a new viewer takes waits
        # for this lock, so it already contains them.
        self._publish_output(text)
        update = await self._screen.feed(text)
        if self._command is not None:
            self._command.add_lines(update.transcript)
            self._last_output_at = self._services.monotonic()
        if update.responses:
            # Terminal protocol replies, not input: they start no activity.
            await asyncio.to_thread(self._adapter.write, update.responses)
        paste_disabled = self._bracketed_paste and not update.bracketed_paste
        facts_changed = (update.title, update.alternate_screen) != (
            self._title,
            self._alternate_screen,
        )
        self._title = update.title
        self._bracketed_paste = update.bracketed_paste
        self._alternate_screen = update.alternate_screen
        if update.alternate_exited:
            await self._publish_snapshot()
        if update.alternate_exited or paste_disabled:
            self._snapshot_on_settle = True
        effect = self._activity.output(
            self._services.monotonic(),
            attached=self.attachment is not None,
            delivery_pending=self._delivery_pending(),
        )
        if effect.changes_screen:
            self._screen_revision += 1
        if effect.restart is not None:
            self._restart_quiet(effect.restart)
        if effect.state_changed or facts_changed:
            self._publish_state()
        self._output_count += 1
        self._output_event.set()

    def _restart_quiet(self, restart: QuietRestart) -> None:
        if restart.supersede_delivery:
            self._cancel_delivery()
        self._cancel_task(self._settle_task)
        generation = restart.generation
        self._settle_task = self._background(
            self._settle_after_quiet(generation), f"settle:{generation}"
        )

    async def _settle_after_quiet(self, generation: int) -> None:
        try:
            await self._services.sleep(self._services.activity_quiet_seconds)
            async with self._lock:
                if generation != self._activity.generation or self.finished:
                    return
                if self._snapshot_on_settle:
                    self._snapshot_on_settle = False
                    await self._publish_snapshot()
                observation = await self._screen.observe(TERMINAL_DELIVERY_TAIL_LINES)
                effect = self._activity.settle(
                    generation, observation.signature, attached=self.attachment is not None
                )
                if effect is None:
                    return
                self._settled = (self._screen_revision, observation.signature)
                if not effect.record:
                    return
                details = {"screen_revision": self._screen_revision}
                self._set_attention(
                    kind="output_settled",
                    summary=(
                        "Terminal output has been quiet after recent activity. Inspect the "
                        "current screen; this does not imply that the program finished or "
                        "requires input."
                    ),
                    details=details,
                    deliver=effect.deliver,
                    screen_tail=observation.tail,
                )
        except asyncio.CancelledError:
            return

    async def _send_initial_input(self, text: str, *, origin_run_id: str) -> None:
        """Wait until the program's start screen is quiet before sending its first task."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS
        seen_output = 0
        quiet_since: float | None = None
        try:
            while not self.finished:
                now = loop.time()
                if now >= deadline:
                    break
                if self._output_count != seen_output:
                    seen_output = self._output_count
                    quiet_since = now
                elif (
                    quiet_since is not None
                    and now - quiet_since >= TERMINAL_INITIAL_INPUT_QUIET_SECONDS
                ):
                    async with self._lock:
                        has_screen = bool(await self._screen.screen_text())
                    if has_screen:
                        break
                    quiet_since = now
                self._output_event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._output_event.wait(), timeout=0.1)
            if self.finished or self.attachment is None:
                return
            await self.send_input(
                data=None,
                text=text,
                key="enter",
                expected_screen_revision=None,
                origin_run_id=origin_run_id,
                execution_owner=self.execution_owner,
            )
        except asyncio.CancelledError:
            return

    async def _mark_finished(self, error: BaseException | None) -> None:
        # A root process EOF cannot confirm that captured descendants exited.
        if self._termination_pending:
            return
        self._cancel_task(self._initial_input_task)
        self._cancel_task(self._settle_task)
        async with self._lock:
            if self.finished:
                return
            if self._command is not None:
                await self._record_shell_exit()
                self._command.tree_ended()
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(self._adapter.close)
            self.finished_at = self.finished_at or _utc_now()
            if self._command is None:
                self.exit_code = await asyncio.to_thread(self._adapter.exit_code)
            self._activity.finish(error=error is not None)
            try:
                await self._publish_snapshot()
            except Exception:
                # A failed renderer must not keep the Session from finishing.
                _LOGGER.warning(
                    "Could not render the final screen of terminal=%s",
                    self.terminal_id,
                    exc_info=True,
                )
            if self._suppress_exit_attention:
                self._publish_state()
            elif error is None:
                self._set_attention(
                    kind="exited",
                    summary=f"Terminal process exited with code {self.exit_code}.",
                    details={"exit_code": self.exit_code},
                )
            else:
                self._set_attention(
                    kind="error",
                    summary="Terminal transport or rendering failed.",
                    details={"error": str(error)},
                )
            self._attention_event.set()
            self._output_event.set()
            self._finish_files()

    # Attention and delivery

    def _set_attention(
        self,
        *,
        kind: AttentionKind,
        summary: str,
        details: dict[str, Any],
        deliver: bool = True,
        screen_tail: str = "",
        body: str | None = None,
    ) -> None:
        self._cancel_delivery()
        self._attention_revision += 1
        revision = self._attention_revision
        attention = TerminalAttention(
            revision=revision,
            kind=kind,
            notice_id=f"terminal:{self.terminal_id}:attention:{revision}",
            summary=summary,
            details=details,
            created_at=_utc_now(),
        )
        self._attention = attention
        self._attention_body = (
            body
            if body is not None
            else _attention_body(self.terminal_id, self.state, attention, screen_tail=screen_tail)
        )
        self._attention_event.set()
        if deliver:
            self._schedule_delivery(attention, self._attention_body)
        self._publish_state()

    def _schedule_delivery(self, attention: TerminalAttention, body: str) -> None:
        if self._services.trigger_service is None or self.attachment is None:
            return
        self._delivery_task = self._background(
            self._deliver(attention, body), f"attention:{attention.revision}"
        )

    async def _deliver(self, attention: TerminalAttention, body: str) -> None:
        trigger_service = self._services.trigger_service
        attachment = self.attachment
        origin_run_id = self._activity_origin_run_id or self.origin_run_id
        if trigger_service is None or attachment is None or origin_run_id is None:
            return
        delivery = trigger_service.submit_completion(
            attachment.agent_id,
            attachment.session_id,
            notice_id=attention.notice_id,
            origin_run_id=origin_run_id,
            body=body,
            project_id=attachment.project_id,
            execution_owner=self._activity_execution_owner,
        )
        try:
            await delivery
        except RunAdmissionBlockedError:
            # The activity's execution owner closed (for example its temporary
            # group): an expected end of its lifecycle, not a delivery fault.
            _LOGGER.debug(
                "Terminal attention dropped for a closed execution owner (terminal=%s revision=%s)",
                self.terminal_id,
                attention.revision,
            )
            return
        if self._attention is attention:
            self._attention = replace(attention, delivered=True)

    def _delivery_running(self) -> bool:
        task = self._delivery_task
        return task is not None and not task.done()

    def _delivery_pending(self) -> bool:
        """An ``output_settled`` delivery is on its way and not yet received."""
        attention = self._attention
        return (
            attention is not None
            and attention.kind == "output_settled"
            and not attention.delivered
            and self._delivery_running()
        )

    def _cancel_delivery(self) -> None:
        task = self._delivery_task
        attention = self._attention
        attachment = self.attachment
        if task is None or task.done() or attention is None or attachment is None:
            return
        if self._services.trigger_service is not None:
            self._services.trigger_service.cancel_completion(
                attachment.agent_id,
                attachment.session_id,
                notice_id=attention.notice_id,
                project_id=attachment.project_id,
            )
        task.cancel()

    # Stream

    def _publish_output(self, text: str) -> None:
        self._stream_sequence += 1
        self._stream.publish(
            {"type": "terminal_output", "sequence": self._stream_sequence, "data": text}
        )

    async def _publish_snapshot(self) -> None:
        ansi = await self._screen.ansi_snapshot()
        self._stream_sequence += 1
        self._stream.publish(
            {
                "type": "terminal_snapshot",
                "sequence": self._stream_sequence,
                "terminal": self._services.operator_summary(self.info()),
                "ansi": ansi,
            }
        )

    def _publish_state(self) -> None:
        self._stream_sequence += 1
        self._stream.publish(
            {
                "type": "terminal_state",
                "sequence": self._stream_sequence,
                "terminal": self._services.operator_summary(self.info()),
            }
        )
        if not self.hidden:
            self._services.changed(self.terminal_id)

    # Helpers

    def _require_live(self) -> None:
        if self.finished or self.finished_at is not None:
            raise TerminalClosedError("Terminal Session is no longer running")

    def _check_revision(self, expected: int | None) -> None:
        if expected is not None and expected != self._screen_revision:
            raise TerminalStaleScreenError(
                "Terminal screen changed; inspect status before sending this input"
            )

    def _input_result(
        self, characters: int, key: str | None, *, bracketed_paste: bool, superseded: int | None
    ) -> dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "state": self.state,
            "characters_sent": characters,
            "key": key,
            "bracketed_paste": bracketed_paste,
            "superseded_attention_revision": superseded,
            "screen_revision": self._screen_revision,
        }

    def _finish_files(self) -> None:
        if self._command is not None:
            self._command.close()
        if self._log_handle is not None:
            with contextlib.suppress(OSError):
                self._log_handle.close()
            self._log_handle = None
        if self._log_lease is not None:
            self._log_lease.finish()
            self._log_lease = None
        cleanup, self._cleanup = self._cleanup, None
        if cleanup is not None:
            try:
                cleanup()
            except OSError:
                _LOGGER.warning(
                    "Could not remove the launch files of terminal=%s", self.terminal_id
                )

    def _background(self, coroutine: Coroutine[Any, Any, None], label: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=f"terminal:{self.terminal_id}:{label}")
        task.add_done_callback(
            lambda done: log_background_task_result(
                done, f"Terminal {label} failed for terminal={self.terminal_id}"
            )
        )
        return task

    @staticmethod
    def _cancel_task(task: asyncio.Task[Any] | None) -> None:
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()


def _attention_body(
    terminal_id: str, state: str, attention: TerminalAttention, *, screen_tail: str
) -> str:
    heading = {
        "output_settled": "Terminal output settled",
        "exited": "Terminal process exited",
        "error": "Terminal failure",
    }[attention.kind]
    sections = [
        f"### Terminal Session — {heading}",
        f"Terminal id: {terminal_id}",
        f"State: {state}",
        f"Attention revision: {attention.revision}",
        attention.summary,
    ]
    if attention.kind == "output_settled":
        sections.extend(
            (
                (
                    "Decide from the screen tail below whether to act or keep waiting; "
                    "quiet output does not prove completion. For prompt input, pass the "
                    "screen_revision below as expected_screen_revision. Use status only for "
                    "missing screen/history context. Send replies to this terminal_id."
                ),
                f"screen_revision: {attention.details['screen_revision']}",
                "",
                "```",
                screen_tail,
                "```",
            )
        )
    elif attention.details:
        sections.append(json.dumps(attention.details, ensure_ascii=False, indent=2))
    body = "\n".join(sections)
    if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS:
        return body
    return body[:TERMINAL_NOTICE_MESSAGE_CAP_CHARS] + "\n[attention details truncated]"
