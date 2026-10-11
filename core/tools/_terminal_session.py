"""One interactive Terminal Session: process I/O, screen, activity and delivery."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease
from core.tools import terminal_backend
from core.tools.terminal_backend import TerminalAdapter
from core.tools.terminal_emulator import FOCUS_OUT_REPORT, EmulatorUpdate, ScreenObservation
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._terminal_activity import (
    PROCESS_SAMPLE_SECONDS,
    IdleReason,
    ProcessSample,
    QuietRestart,
    TerminalActivity,
)
from ._terminal_input import input_chunks
from ._terminal_render_host import TerminalScreen
from ._terminal_state import (
    TERMINAL_DELIVERY_TAIL_LINES,
    TERMINAL_INITIAL_INPUT_QUIET_SECONDS,
    TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS,
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
    WaitEnded,
    _attention_data,
    _new_terminal_stream,
    _utc_now,
    _validate_dimensions,
)
from .process_tree import TREE_POLL_SECONDS, ProcessTree

_LOGGER = get_logger("tools.terminal_manager")
_READ_CHUNK_CHARS = 4096
_OPERATOR_READ_HISTORY_LINES = 30
# Labels in Agent notices are cut to this many characters.
_NOTICE_LABEL_CHARS = 60
# A key typed after text follows once the output paused for this share of the
# activity quiet period (0.25 s), at the latest this share after the text (1 s).
# Codex took an Enter 0.1 s after a 150-character text as part of a paste and
# did not submit it; 0.3 s later it submitted (measured 2026-10-11).
_ECHO_PAUSE_SHARE = 0.125
_ECHO_LIMIT_SHARE = 0.5
# Failures that persist across chunks or polls, keyed by (terminal id, kind):
# logged when they start and when they end, not on every repetition.
_CONDITIONS = LoggedConditions()


@dataclass(frozen=True, slots=True)
class TerminalSessionServices:
    """What every Terminal Session of one manager shares."""

    trigger_service: Any | None
    reader_executor: ThreadPoolExecutor
    activity_quiet_seconds: float
    idle_seconds: float
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
    # Visible-cell signature: the Agent saw this screen, so its idle boundary wakes nobody.
    signature: str


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
        observer: ProcessTree | None = None,
    ) -> None:
        """*observer* watches the program's process tree, whose CPU time and new
        processes show work its output does not."""
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
        # Serializes input, so nothing types between a text and the key after it.
        self._input_lock = asyncio.Lock()
        self._observer = observer
        self._activity = TerminalActivity(
            awaiting_initial_input=awaiting_initial_input,
            startup_silence=owner is not None and not awaiting_initial_input,
            repaint_window=TERMINAL_REPAINT_WINDOW_SECONDS,
            quiet_seconds=services.activity_quiet_seconds,
            idle_seconds=services.idle_seconds,
        )
        self._title = ""
        self._bracketed_paste = False
        # The program asked for focus reports; it is told the focus is lost.
        self._focus_reporting = False
        self._alternate_screen = False
        self._screen_revision = 0
        self._resize_count = 0
        self._observed: TerminalObservation | None = None
        self._output_count = 0
        # Quiet boundaries so far: output paused for the quiet period after
        # activity. Replies to input and start end at the next one.
        self._quiet_boundaries = 0
        # Quiet boundaries when start's text was typed; None until it is typed.
        self._initial_input_quiet: int | None = None
        self._snapshot_on_settle = False
        self._attention: TerminalAttention | None = None
        self._attention_body = ""
        self._attention_revision = 0
        self._acknowledged_attention_revision = 0
        self._activity_origin_run_id: str | None = None
        self._activity_execution_owner = execution_owner
        self._suppress_exit_attention = False
        # vBot, the user or an Agent stopped the program.
        self._stopped = False
        # A result durably showed the program's end: its exit is not delivered.
        self._exit_acknowledged = False
        # Set and replaced whenever output, input, attention or the program's
        # end changes what a waiter can observe.
        self._change = asyncio.Event()
        self._termination_pending = False
        self._termination_targets: list[Any] = []
        self._output_event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._initial_input_task: asyncio.Task[None] | None = None
        self._settle_task: asyncio.Task[None] | None = None
        self._sample_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._stream_sequence = 0
        self._stream = _new_terminal_stream()
        self._log_path = log_path
        self._log_handle = log_handle
        self._log_lease = log_lease
        self._cleanup = cleanup
        self._last_output_at = services.monotonic()
        # Set once vBot killed the process tree.
        self._tree_killed = asyncio.Event()
        # Why output can no longer be read, and why the screen fell behind it;
        # either becomes the Session's failure once its processes have ended.
        self._read_failure: Exception | None = None
        self._render_failure: Exception | None = None

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
            log_path=self._log_path,
            stopped=self._stopped,
        )

    # Lifecycle

    def start(self, *, initial_text: str | None) -> None:
        """Start reading output and, for an Agent start with text, its first input."""
        self._reader_task = self._background(self._read(), "reader")
        if initial_text is not None and self.origin_run_id is not None:
            self._initial_input_task = self._background(
                self._send_initial_input(initial_text, origin_run_id=self.origin_run_id),
                "initial-input",
            )
            self._initial_input_task.add_done_callback(lambda _task: self._signal_change())
        self._publish_state()

    async def terminate(self, *, suppress_attention: bool) -> None:
        """Stop the process tree; a failure leaves the Session unfinished for a retry."""
        self._cancel_task(self._initial_input_task)
        if not self.finished:
            self._stopped = True
        if suppress_attention:
            self._suppress_exit_attention = True
            self._cancel_delivery()
        self._cancel_task(self._settle_task)
        self._cancel_task(self._sample_task)
        if self._termination_pending or not self.finished:
            self._termination_pending = True
            try:
                await asyncio.to_thread(self._kill_tree)
            except OSError as error:
                raise TerminalManagerError(
                    f"Could not stop terminal {self.terminal_id}: the operating system did not "
                    "end its processes, so they can still be running"
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
            await self._mark_finished()

    def stop_now(self) -> bool:
        """Synchronously stop the process for Runtime shutdown; False if the tree survived."""
        self._suppress_exit_attention = True
        self._cancel_delivery()
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
            self._sample_task,
        ):
            self._cancel_task(task)
        self._finish_files()
        return True

    def _kill_tree(self) -> None:
        """Kill the process tree (blocking); OSError when that is not confirmed."""
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
                self._sample_task,
                self._delivery_task,
            )
            if task is not None and not task.done()
        ]

    def release(self) -> None:
        """Drop a finished Session's retained resources when it leaves the catalog."""
        self._cancel_delivery()
        self._finish_files()
        self._screen.close()
        for kind in ("render", "end_check", "process_sample"):
            _CONDITIONS.ended((self.terminal_id, kind))

    # Waiting

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

    async def wait_for_program(
        self,
        *,
        deadline: float,
        pattern: re.Pattern[str] | None,
        after_revision: int,
        on_match: Callable[[str], None] | None = None,
    ) -> WaitEnded:
        """Wait until the program exits, its output matches *pattern*, it goes idle
        showing a new screen, or *deadline* (a ``monotonic`` time) passes.

        Output printed before the call counts for *pattern*, except output
        before the Agent's last input and that input's echo. The program went
        idle when an ``idle`` attention above *after_revision* exists; with
        *pattern* that does not end the wait. *on_match* receives the line
        where the match starts.
        """
        services = self._services
        match_from: int | None = None
        while True:
            change = self._change
            if self.finished:
                return "exited"
            if pattern is not None:
                matched, match_from = await self._output_matches(pattern, match_from)
                if matched is not None:
                    if on_match is not None:
                        on_match(matched)
                    return "matched"
            attention = self._attention
            if (
                pattern is None
                and attention is not None
                and attention.kind == "idle"
                and attention.revision > after_revision
            ):
                return "idle"
            now = services.monotonic()
            if now >= deadline:
                return "timeout"
            await self._wait_or_sleep(change, deadline - now)

    async def wait_for_reply(self, *, deadline: float, after_quiet: int) -> WaitEnded:
        """Wait until the output pauses after input, the program exits, or *deadline* passes.

        *after_quiet* is the ``quiet_boundaries`` count the input reported:
        the output paused once a quiet boundary follows it.
        """
        while True:
            change = self._change
            if self.finished:
                return "exited"
            if self._quiet_boundaries > after_quiet:
                return "quiet"
            now = self._services.monotonic()
            if now >= deadline:
                return "timeout"
            await self._wait_or_sleep(change, deadline - now)

    async def wait_started(self, *, deadline: float) -> None:
        """Wait until an Agent start shows its first screen, at the latest *deadline*.

        The screen is shown once start-up output has paused for the quiet
        period, once the output after start's text settled, or once the
        program ended.
        """
        services = self._services
        while not self.finished:
            change = self._change
            now = services.monotonic()
            if now >= deadline:
                return
            initial_input = self._initial_input_task
            if initial_input is not None:
                if initial_input.done():
                    typed_at = self._initial_input_quiet
                    if typed_at is None or self._quiet_boundaries > typed_at:
                        return
                pause = deadline - now
            else:
                silent = now - self._last_output_at
                if silent >= services.activity_quiet_seconds:
                    return
                pause = min(deadline - now, services.activity_quiet_seconds - silent)
            await self._wait_or_sleep(change, pause)

    def acknowledge_exit(self) -> None:
        """A result durably showed the program's end, so its exit is not delivered."""
        self._exit_acknowledged = True
        attention = self._attention
        if attention is not None and attention.kind in {"exited", "error"}:
            self._acknowledged_attention_revision = max(
                self._acknowledged_attention_revision, attention.revision
            )
            self._cancel_delivery()

    async def _output_matches(
        self, pattern: re.Pattern[str], from_line: int | None
    ) -> tuple[str | None, int]:
        """The line where a match in the output from *from_line* on starts, or None;
        also the line to check from next.

        Without *from_line*, output printed before the wait counts, except
        output before the Agent's last input and its echo. Lines above the
        screen no longer change, so a later check starts at the logical line
        just above the screen (a match can span two lines).
        """
        async with self._lock:
            found = await self._screen.pattern_text(start_line=from_line)
        text = found["text"]
        match = pattern.search(text)
        if match is None:
            return None, int(found["next_line"])
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.start())
        return text[start : len(text) if end < 0 else end], int(found["next_line"])

    async def _await_end(self) -> None:
        """Wait until the Session's process has ended, or vBot killed it.

        Output has ended or can no longer be read, so the process is polled
        until it exits.
        """
        while not self._tree_killed.is_set():
            if await self._processes_ended():
                return
            await self._wait_or_sleep(self._tree_killed, TREE_POLL_SECONDS)

    async def _processes_ended(self) -> bool:
        """Whether the Session's process has ended; False while that is unknown."""
        key = (self.terminal_id, "end_check")
        try:
            ended = not await asyncio.to_thread(self._adapter.is_alive)
        except Exception:
            if _CONDITIONS.started(key):
                _LOGGER.warning(
                    "Terminal process check failed; the Session stays live until its "
                    "processes are confirmed ended (terminal=%s)",
                    self.terminal_id,
                    exc_info=True,
                )
            return False
        if _CONDITIONS.ended(key):
            _LOGGER.info("Terminal process check recovered (terminal=%s)", self.terminal_id)
        return ended

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
        """Write exact data or named terminal input from the attached Agent.

        A key typed after text follows once the program took the text
        (``_await_text_taken``); other input waits until both are written.
        """
        write_error: BaseException | None = None
        async with self._input_lock:
            async with self._lock:
                self._require_live()
                self._check_revision(expected_screen_revision)
                bracketed_paste = (
                    text is not None and ("\n" in text or "\r" in text) and self._bracketed_paste
                )
                chunks = input_chunks(
                    data=data, text=text, key=key, bracketed_paste=bracketed_paste
                )
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
                # A wait's pattern matches what the program prints below this line.
                await self._screen.mark_input()
                running = await self._running_before_input()
                *typed, last = chunks
                typed_at = self._services.monotonic()
                if typed:
                    write_error = await self._write_input(typed[0], running)
            if typed and write_error is None:
                await self._await_text_taken(typed_at)
            if write_error is None:
                async with self._lock:
                    self._require_live()
                    write_error = await self._write_input(last, running)
                    if write_error is None:
                        if self._focus_reporting and last.endswith("\r"):
                            # The program that works on the submitted input,
                            # possibly one it starts, learns that its user is away.
                            with contextlib.suppress(EOFError, OSError):
                                await asyncio.to_thread(self._adapter.write, FOCUS_OUT_REPORT)
                        return self._input_result(
                            sum(len(chunk) for chunk in chunks),
                            key,
                            bracketed_paste=bracketed_paste,
                            superseded=superseded,
                        )
        raise await self._write_failure() from write_error

    async def _write_input(
        self, chunk: str, running: frozenset[int] | None
    ) -> BaseException | None:
        """Write one chunk of Agent input, which starts a cycle; the lock is held.

        *running* are the processes that ran before the input. Returns the
        error when the PTY refused the chunk.
        """
        restart, changed = self._activity.input(
            self._services.monotonic(),
            notify=True,
            delivery_pending=self._delivery_pending(),
            running=running,
        )
        try:
            await asyncio.to_thread(self._adapter.write, chunk)
        except (EOFError, OSError) as error:
            return error
        self._restart_quiet(restart)
        self._output_event.set()
        self._signal_change()
        if changed:
            self._publish_state()
        return None

    async def _await_text_taken(self, typed_at: float) -> None:
        """Wait until the program took the text typed at *typed_at*: until its
        output paused for the echo pause, at most the echo limit after the text.

        Programs that read fast input as a paste take a key that arrives
        right after it as part of the paste (Codex inserts such an Enter
        instead of submitting). The lock is not held, so the output renders.
        """
        services = self._services
        pause = services.activity_quiet_seconds * _ECHO_PAUSE_SHARE
        limit = typed_at + services.activity_quiet_seconds * _ECHO_LIMIT_SHARE
        while not self.finished:
            change = self._change
            now = services.monotonic()
            quiet_since = max(typed_at, self._last_output_at)
            if now - quiet_since >= pause or now >= limit:
                return
            await self._wait_or_sleep(change, min(limit, quiet_since + pause) - now)

    async def send_operator_input(
        self,
        data: str,
        *,
        expected_screen_revision: int | None = None,
        expected_program: str | None = None,
    ) -> None:
        """Write exact operator bytes; *expected_program* must run in the process tree."""
        write_error: BaseException | None = None
        async with self._input_lock, self._lock:
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
            running = await self._running_before_input()
            restart, changed = self._activity.input(
                self._services.monotonic(),
                notify=self.attachment is not None,
                delivery_pending=self._delivery_pending(),
                running=running,
            )
            try:
                await asyncio.to_thread(self._adapter.write, data)
            except (EOFError, OSError) as error:
                write_error = error
            else:
                self._restart_quiet(restart)
                self._output_event.set()
                self._signal_change()
                if changed:
                    self._publish_state()
                return
        raise await self._write_failure() from write_error

    async def _write_failure(self) -> TerminalManagerError:
        """The error for input the PTY refused, by whether the program still runs.

        A program that has ended finishes the Session now.
        """
        try:
            alive = await asyncio.to_thread(self._adapter.is_alive)
        except Exception:
            alive = True
        if alive:
            return TerminalManagerError(
                f"The input could not be written to terminal {self.terminal_id}"
            )
        await self._mark_finished()
        return TerminalClosedError("Terminal Session is no longer running")

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
            signature = (await self._screen.observe(0)).signature
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
                info.screen_revision, self._resize_count, info.columns, info.rows, signature
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
        data["program"] = info.program
        data["stopped"] = info.stopped
        initial_input = self._initial_input_task
        data["initial_input_pending"] = initial_input is not None and not initial_input.done()
        typed_at = self._initial_input_quiet
        # Start's text was typed and the output after it has not settled yet.
        data["initial_output_pending"] = (
            typed_at is not None and self._quiet_boundaries <= typed_at and not self.finished
        )
        return data

    def acknowledge_screen(self, observation: TerminalObservation) -> None:
        """Remember a durably delivered screen without consuming a later resize.

        The Agent has seen it: an idle boundary showing the same screen wakes nobody.
        """
        observed = self._observed
        if observed is None or (observation.resize_count, observation.screen_revision) >= (
            observed.resize_count,
            observed.screen_revision,
        ):
            self._observed = observation
            self._activity.acknowledge(observation.signature)

    def acknowledge_attention(self, revision: int) -> None:
        """Cancel the equivalent automatic delivery after a manual result is durable."""
        attention = self._attention
        if attention is None or attention.revision != revision:
            return
        self._acknowledged_attention_revision = max(self._acknowledged_attention_revision, revision)
        self._cancel_delivery()

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
        """Read output, then keep the Session live until its processes have ended.

        The end of output - EOF, a closed PTY, or a failure to read or handle
        it - is no end of the program: the Session finishes only once its
        process has ended, or once vBot killed it, which ends the wait or
        cancels this task.
        """
        ended = False
        try:
            await self._read_output()
            await self._await_end()
            ended = True
        except asyncio.CancelledError:
            # Only a kill or a shutdown cancels the reader, after it stopped the processes.
            ended = True
            raise
        finally:
            if ended:
                await self._mark_finished()

    async def _read_output(self) -> None:
        """Read and render output until it ends or the program has ended.

        An unexpected failure ends only the reading: it becomes the Session's
        failure, and the program keeps running until it ends or is stopped.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    text = await loop.run_in_executor(
                        self._services.reader_executor, self._adapter.read, _READ_CHUNK_CHARS
                    )
                except TimeoutError:
                    if not await asyncio.to_thread(self._adapter.is_alive):
                        return
                    continue
                if text:
                    async with self._lock:
                        await self._render_output(text)
        except EOFError, OSError:
            # Output ended (POSIX: every holder of the terminal closed it).
            return
        except Exception as error:
            self._read_failure = error
            _LOGGER.error(
                "Terminal output reading failed; the program keeps running until it ends "
                "or is stopped (terminal=%s)",
                self.terminal_id,
                exc_info=True,
            )

    async def _render_output(self, text: str) -> None:
        """Log, stream and render one chunk of output; the lock is held.

        A failing log or renderer degrades the Session instead of ending it:
        its output keeps being read, so the program neither stalls nor ends.
        """
        self._write_log(text)
        # Viewers get the bytes first; the snapshot a new viewer takes waits
        # for this lock, so it already contains them.
        self._publish_output(text)
        update = await self._render(text)
        self._last_output_at = self._services.monotonic()
        facts_changed = False
        if update is not None:
            facts_changed = await self._apply_update(update)
        effect = self._activity.output(
            self._services.monotonic(),
            attached=self.attachment is not None,
            delivery_pending=self._delivery_pending(),
            signals=() if update is None else update.signals,
        )
        if effect.changes_screen:
            self._screen_revision += 1
        if effect.restart is not None:
            self._restart_quiet(effect.restart)
        if effect.state_changed or facts_changed:
            self._publish_state()
        self._output_count += 1
        self._output_event.set()
        self._signal_change()

    def _write_log(self, text: str) -> None:
        handle = self._log_handle
        if handle is None:
            return
        try:
            handle.write(text)
            handle.flush()
        except OSError:
            _LOGGER.warning(
                "Terminal output log write failed; the log ends here (terminal=%s)",
                self.terminal_id,
                exc_info=True,
            )
            self._log_handle = None
            with contextlib.suppress(OSError):
                handle.close()

    async def _render(self, text: str) -> EmulatorUpdate | None:
        """Feed *text* to the screen; None when that failed, which leaves the screen behind."""
        try:
            update = await self._screen.feed(text)
        except Exception as error:
            self._render_failed(error)
            return None
        if self._render_failure is not None:
            self._render_failure = None
            if _CONDITIONS.ended((self.terminal_id, "render")):
                _LOGGER.info("Terminal screen rendering recovered (terminal=%s)", self.terminal_id)
        return update

    def _render_failed(self, error: Exception) -> None:
        """The screen missed output; it follows the output again once a later chunk renders."""
        self._render_failure = error
        if _CONDITIONS.started((self.terminal_id, "render")):
            _LOGGER.warning(
                "Terminal screen rendering failed; the program keeps running and its screen "
                "falls behind its output (terminal=%s)",
                self.terminal_id,
                exc_info=error,
            )

    async def _apply_update(self, update: EmulatorUpdate) -> bool:
        """Apply what rendering found; True when the title or the alternate screen changed."""
        if update.responses:
            # Terminal protocol replies, not input: they start no activity. Whether
            # a program that can no longer read them still runs is checked apart.
            with contextlib.suppress(EOFError, OSError):
                await asyncio.to_thread(self._adapter.write, update.responses)
        paste_disabled = self._bracketed_paste and not update.bracketed_paste
        facts_changed = (update.title, update.alternate_screen) != (
            self._title,
            self._alternate_screen,
        )
        self._title = update.title
        self._bracketed_paste = update.bracketed_paste
        self._focus_reporting = update.focus_reporting
        self._alternate_screen = update.alternate_screen
        if update.alternate_exited:
            try:
                await self._publish_snapshot()
            except Exception:
                # Viewers resynchronize at the next snapshot instead.
                _LOGGER.warning(
                    "Could not render the snapshot of terminal=%s", self.terminal_id, exc_info=True
                )
        if update.alternate_exited or paste_disabled:
            self._snapshot_on_settle = True
        return facts_changed

    def _signal_change(self) -> None:
        """Wake every waiter: output, input, attention or the program's end changed."""
        self._change.set()
        self._change = asyncio.Event()

    def _restart_quiet(self, restart: QuietRestart) -> None:
        if restart.supersede_delivery:
            self._cancel_delivery()
        self._cancel_task(self._settle_task)
        generation = restart.generation
        self._settle_task = self._background(
            self._settle_after_quiet(generation), f"settle:{generation}"
        )
        sampler = self._sample_task
        if (
            self._observer is not None
            and self._activity.cycle_open
            and (sampler is None or sampler.done())
        ):
            self._sample_task = self._background(self._sample_cycle(), "sample")

    async def _settle_after_quiet(self, generation: int) -> None:
        """Run the quiet boundary of one activity generation, then its idle boundary
        when the program declared or showed the end of its work, or else at the
        latest at the fallback (``TerminalActivity``)."""
        services = self._services
        try:
            await services.sleep(services.activity_quiet_seconds)
            async with self._lock:
                observation = await self._observe_unchanged(generation)
                if observation is None:
                    return
                settled = self._activity.settle(generation, observation.signature)
                if settled is None:
                    return
                if settled == "activity":
                    self._quiet_boundaries += 1
                    self._signal_change()
                if settled == "repaint" or self._settle_idle(generation, observation):
                    return
            # Process samples can end the cycle earlier; without them, nothing
            # but the fallback can.
            fallback_at = self._activity.fallback_at(generation)
            if fallback_at is None:
                return
            await services.sleep(max(0.0, fallback_at - services.monotonic()))
            async with self._lock:
                observation = await self._observe_unchanged(generation)
                if observation is not None:
                    self._settle_idle(generation, observation)
        except asyncio.CancelledError:
            return

    async def _sample_cycle(self) -> None:
        """Sample the program's processes while a cycle of work runs; once its output
        is quiet, each sample can end the cycle."""
        services = self._services
        try:
            while True:
                await services.sleep(PROCESS_SAMPLE_SECONDS)
                sample = await self._process_sample()
                async with self._lock:
                    activity = self._activity
                    if self.finished or not activity.cycle_open:
                        return
                    now = services.monotonic()
                    if sample is not None:
                        activity.sample(now, sample)
                    if not activity.quiet(now):
                        continue
                    generation = activity.generation
                    observation = await self._observe_unchanged(generation)
                    if observation is not None and self._settle_idle(generation, observation):
                        return
        except asyncio.CancelledError:
            return

    def _settle_idle(self, generation: int, observation: ScreenObservation) -> bool:
        """End the cycle of *generation* if its program is idle; the lock is held.

        Records and delivers the idle screen when the Agent has not seen it.
        """
        observed = self._observed
        effect = self._activity.idle(
            generation,
            self._services.monotonic(),
            observation.signature,
            attached=self.attachment is not None,
            seen=observed is not None and observed.screen_revision == self._screen_revision,
        )
        if effect is None:
            return False
        if effect.record:
            self._set_attention(
                kind="idle",
                summary="The program is idle.",
                details={"screen_revision": self._screen_revision, "reason": effect.reason},
                deliver=effect.deliver,
                screen_tail=observation.tail,
            )
        else:
            self._publish_state()
            self._signal_change()
        return True

    async def _process_sample(self) -> ProcessSample | None:
        """The program's processes now; None without a process tree or when it failed."""
        tree = self._observer
        if tree is None:
            return None
        key = (self.terminal_id, "process_sample")
        try:
            facts = await asyncio.to_thread(tree.facts)
        except Exception:
            if _CONDITIONS.started(key):
                _LOGGER.warning(
                    "Terminal process sampling failed; its activity relies on output and "
                    "declared signals (terminal=%s)",
                    self.terminal_id,
                    exc_info=True,
                )
            return None
        if _CONDITIONS.ended(key):
            _LOGGER.info("Terminal process sampling recovered (terminal=%s)", self.terminal_id)
        return ProcessSample(
            frozenset(process.pid for process in facts.running),
            facts.cpu_seconds,
            facts.started,
        )

    async def _running_before_input(self) -> frozenset[int] | None:
        """The processes that run before input starts a cycle: a process started
        later is the cycle's work. The sample also starts the CPU measurement,
        so CPU work shows from the cycle's first periodic sample on."""
        sample = await self._process_sample()
        if sample is None:
            return None
        self._activity.sample(self._services.monotonic(), sample)
        return sample.running

    async def _observe_unchanged(self, generation: int) -> ScreenObservation | None:
        """The screen while *generation* is still the latest activity; the lock is held.

        None once newer activity, the program's end or a rendering failure
        (a screen behind its output cannot show that the output paused)
        made the boundary moot.
        """
        if generation != self._activity.generation or self.finished:
            return None
        if self._render_failure is not None:
            return None
        if self._snapshot_on_settle:
            self._snapshot_on_settle = False
            await self._publish_snapshot()
        try:
            return await self._screen.observe(TERMINAL_DELIVERY_TAIL_LINES)
        except Exception as error:
            self._render_failed(error)
            return None

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
            sent = await self.send_input(
                data=None,
                text=text,
                key="enter",
                expected_screen_revision=None,
                origin_run_id=origin_run_id,
                execution_owner=self.execution_owner,
            )
            self._initial_input_quiet = int(sent["quiet_boundaries"])
        except asyncio.CancelledError:
            return

    async def _mark_finished(self) -> None:
        """Record the end once no process of the Session runs any longer.

        A failure to read or render its output, if any, becomes its outcome.
        """
        # A root process EOF cannot confirm that captured descendants exited.
        if self._termination_pending:
            return
        self._cancel_task(self._initial_input_task)
        self._cancel_task(self._settle_task)
        self._cancel_task(self._sample_task)
        async with self._lock:
            if self.finished:
                return
            self.finished_at = self.finished_at or _utc_now()
            self.exit_code = await asyncio.to_thread(self._adapter.exit_code)
            # Release the terminal (on Windows also its console host and reader thread).
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self._adapter.close)
            error = self._read_failure or self._render_failure
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
            else:
                tail = ""
                with contextlib.suppress(Exception):
                    tail = (await self._screen.observe(TERMINAL_DELIVERY_TAIL_LINES)).tail
                if error is None:
                    self._set_attention(
                        kind="exited",
                        summary=f"The program exited with code {self.exit_code}.",
                        details={"exit_code": self.exit_code},
                        screen_tail=tail,
                    )
                else:
                    self._set_attention(
                        kind="error",
                        summary="The terminal failed.",
                        details={"error": str(error)},
                        screen_tail=tail,
                    )
            self._output_event.set()
            self._signal_change()
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
        self._attention_body = _attention_body(
            self.terminal_id,
            self._notice_label(),
            attention,
            screen_tail=screen_tail,
            idle_seconds=self._services.idle_seconds,
        )
        if kind in {"exited", "error"} and self._exit_acknowledged:
            # A result already showed the end.
            deliver = False
        if deliver:
            self._schedule_delivery(attention, self._attention_body)
        self._publish_state()
        self._signal_change()

    def _notice_label(self) -> str:
        info = self.info()
        label = " ".join((info.name or info.program).split())
        if len(label) > _NOTICE_LABEL_CHARS:
            return label[: _NOTICE_LABEL_CHARS - 3] + "..."
        return label

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
        # The Agent received it: a wait ends only at a later attention.
        self._acknowledged_attention_revision = max(
            self._acknowledged_attention_revision, attention.revision
        )
        if self._attention is attention:
            self._attention = replace(attention, delivered=True)

    def _delivery_running(self) -> bool:
        task = self._delivery_task
        return task is not None and not task.done()

    def _delivery_pending(self) -> bool:
        """An ``idle`` delivery is on its way and not yet received."""
        attention = self._attention
        return (
            attention is not None
            and attention.kind == "idle"
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
        self,
        characters: int,
        key: str | None,
        *,
        bracketed_paste: bool,
        superseded: int | None,
    ) -> dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "state": self.state,
            "characters_sent": characters,
            "key": key,
            "bracketed_paste": bracketed_paste,
            "superseded_attention_revision": superseded,
            "screen_revision": self._screen_revision,
            # A wait for the reply ends at the next quiet boundary after this count.
            "quiet_boundaries": self._quiet_boundaries,
        }

    def _finish_files(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.close()
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
            lambda done: _log_task_failure(
                done, f"Terminal {label} failed for terminal={self.terminal_id}"
            )
        )
        return task

    @staticmethod
    def _cancel_task(task: asyncio.Task[Any] | None) -> None:
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()


def _attention_body(
    terminal_id: str,
    label: str,
    attention: TerminalAttention,
    *,
    screen_tail: str,
    idle_seconds: float,
) -> str:
    """The message that tells the attached Agent its interactive program needs a look."""
    if attention.kind == "idle":
        reason: IdleReason | None = attention.details.get("reason")
        if reason == "fallback":
            headline = (
                f"Terminal {terminal_id} ({label}) has printed nothing for {idle_seconds:g} "
                "seconds; its program still runs and can still be working. Screen:"
            )
        else:
            headline = (
                f"Terminal {terminal_id} ({label}) went idle; its program still runs. Screen:"
            )
        lines = [
            headline,
            _fenced(screen_tail),
            f'To type into it, call terminal with action "input", terminal_id "{terminal_id}", '
            'your text as text and key "enter". When it shows something new and goes idle '
            "again, that screen arrives as another message.",
        ]
    elif attention.kind == "exited":
        lines = [
            f"The program in terminal {terminal_id} ({label}) exited with code "
            f"{attention.details.get('exit_code')}. Last screen lines:",
            _fenced(screen_tail),
        ]
    else:
        lines = [
            f"Terminal {terminal_id} ({label}) failed and its program no longer runs: "
            f"{attention.details.get('error')}. Last screen lines:",
            _fenced(screen_tail),
        ]
    body = "\n".join(lines)
    if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS:
        return body
    return body[:TERMINAL_NOTICE_MESSAGE_CAP_CHARS] + "\n[message cut]"


def _fenced(text: str) -> str:
    """Screen text in a fence that no line of it can close."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}" if text else "(the screen is empty)"


def _log_task_failure(task: asyncio.Task[Any], message: str) -> None:
    if task.cancelled() or (error := task.exception()) is None:
        return
    _LOGGER.error("%s: %s", message, error, exc_info=(type(error), error, error.__traceback__))
