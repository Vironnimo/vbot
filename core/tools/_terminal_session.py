"""One interactive Terminal Session: process I/O, screen, activity and delivery."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import re
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal

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
    TERMINAL_NOTICE_MESSAGE_CAP_CHARS,
    TERMINAL_REPAINT_WINDOW_SECONDS,
    TERMINAL_STATUS_MAX_LINES,
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
# A screen drawn anew from the log replays it in pieces of this size; the
# last piece, at most the second size, replays under the Session lock.
_REPLAY_PIECE_BYTES = 1 << 20
_REPLAY_LOCKED_BYTES = 1 << 16
# A failed redraw is tried again after twice the quiet period, doubling up
# to this many seconds.
_REDRAW_RETRY_MAX_SECONDS = 300.0
# Once the program has ended, its final screen is drawn anew for at most this long.
_FINAL_REDRAW_SECONDS = 10.0
# The latest clears of the screen recorded for acknowledgements that arrive late.
_CLEARS_KEPT = 64
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
    # A new screen of this size, for one drawn anew from the log.
    open_screen: Callable[[int, int], Awaitable[TerminalScreen]]


@dataclass(frozen=True, slots=True)
class TerminalObservation:
    """The screen a screen-bearing result showed, remembered once it is durable."""

    screen_revision: int
    resize_count: int
    columns: int
    rows: int
    # Visible-cell signature: the Agent saw this screen, so its idle boundary wakes nobody.
    signature: str
    # The history line number just below the last screen line shown: the
    # Agent has seen every line above it that scrolls into history.
    seen_line: int
    # Clears of the screen before it was shown; a later clear erased what it
    # showed from the clear's first line on.
    clears: int = 0


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
        log_path: Path | None,
        log_handle: BinaryIO | None,
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
        # Physical writes stay ordered even if their caller is cancelled.
        # Reading output must never wait for a blocked PTY write.
        self._writer_task: asyncio.Task[BaseException | None] | None = None
        self._input_writes = 0
        self._observer = observer
        self._activity = TerminalActivity(
            repaint_window=TERMINAL_REPAINT_WINDOW_SECONDS,
            quiet_seconds=services.activity_quiet_seconds,
            idle_seconds=services.idle_seconds,
        )
        self._title = ""
        self._bracketed_paste = False
        # The program set application cursor mode: cursor keys send SS3.
        self._application_cursor = False
        # The program asked for focus reports; it is told the focus is lost.
        self._focus_reporting = False
        self._alternate_screen = False
        self._screen_revision = 0
        self._resize_count = 0
        self._observed: TerminalObservation | None = None
        # The attached Agent has seen every history line above this number,
        # in a result or a delivery; None before it saw any screen.
        self._seen_line: int | None = None
        # Clears of the screen so far, and the first line each of the latest
        # erased: a screen shown before a clear acknowledges only the lines
        # above it.
        self._clear_count = 0
        self._clears: deque[tuple[int, int]] = deque(maxlen=_CLEARS_KEPT)
        self._output_count = 0
        # Quiet boundaries so far: output paused for the quiet period after
        # activity. Replies to input end at the next one.
        self._quiet_boundaries = 0
        # A quiet boundary showed something: the program's first screen.
        self._screen_shown = False
        self._snapshot_on_settle = False
        self._attention: TerminalAttention | None = None
        self._attention_body = ""
        # The history line below the screen the pending attention brings,
        # and the clears before that screen.
        self._attention_seen_line = 0
        self._attention_clears = 0
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
        self._settle_task: asyncio.Task[None] | None = None
        self._sample_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._stream_sequence = 0
        self._stream = _new_terminal_stream()
        self._log_path = log_path
        self._log_handle = log_handle
        # Writing the log failed: it ends there, and results that name it say so;
        # from _log_end_line on (the line output went to then), when known.
        self._log_failed = False
        self._log_end_line: int | None = None
        self._log_lease = log_lease
        self._cleanup = cleanup
        self._last_output_at = services.monotonic()
        # Set once vBot killed the process tree.
        self._tree_killed = asyncio.Event()
        # Why output can no longer be read: the Session's failure once its
        # processes have ended.
        self._read_failure: Exception | None = None
        # Why the screen misses output: drawing it failed. Results say so until
        # a screen drawn anew from the log replaced it (``_redraw_screen``).
        self._render_failure: Exception | None = None
        # The screen shows the output as it comes; False after a failed draw.
        self._screen_current = True
        # What a screen drawn anew replays: the log holds every byte of output,
        # _log_size of them so far, and the resizes (log offset, columns, rows)
        # and the last input happened at these log offsets.
        self._log_size = 0
        self._first_size = (screen.columns, screen.rows)
        self._resizes: list[tuple[int, int, int]] = []
        self._input_offset: int | None = None
        # The log offset of the first output the screen missed.
        self._missed_from = 0
        self._redraw_task: asyncio.Task[None] | None = None

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

    def start(self) -> None:
        """Start reading output."""
        self._reader_task = self._background(self._read(), "reader")
        self._publish_state()

    async def terminate(self, *, suppress_attention: bool) -> None:
        """Stop the process tree; a failure leaves the Session unfinished for a retry."""
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
        for task in (self._reader_task, self._settle_task, self._sample_task, self._redraw_task):
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
                self._settle_task,
                self._sample_task,
                self._delivery_task,
                self._redraw_task,
                self._writer_task,
            )
            if task is not None and not task.done()
        ]

    def release(self) -> None:
        """Drop a finished Session's retained resources when it leaves the catalog."""
        self._cancel_delivery()
        self._cancel_task(self._redraw_task)
        self._finish_files()
        self._screen.close()
        for kind in ("render", "skipped", "end_check", "process_sample"):
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

        The first screen shows once the program printed something visible
        and its output then paused for the quiet period (the start-up's quiet
        boundary, where its idle boundary is decided too), or once the
        program ended. A terminal's own set-up sequences draw nothing, so
        they show no first screen.
        """
        services = self._services
        while not self.finished and not self._screen_shown:
            change = self._change
            now = services.monotonic()
            if now >= deadline:
                return
            await self._wait_or_sleep(change, deadline - now)

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
            self._seen_line = None
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
        self._seen_line = None
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
            self._schedule_delivery(
                attention, self._attention_body, self._attention_seen_line, self._attention_clears
            )
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
        Text the program got is reported even when its key was not sent
        (``key_unsent``: the program had ended, or the PTY refused the key),
        so the Agent does not type it again.
        """
        write_error: BaseException | None = None
        text_sent = 0
        async with self._input_lock:
            async with self._input_state():
                self._require_live()
                self._check_revision(expected_screen_revision)
                bracketed_paste = (
                    text is not None and ("\n" in text or "\r" in text) and self._bracketed_paste
                )
                chunks = input_chunks(
                    data=data,
                    text=text,
                    key=key,
                    bracketed_paste=bracketed_paste,
                    application_cursor=self._application_cursor,
                )
                if not chunks:
                    # An empty write changes no activity or observation.
                    return self._input_result(0, key, pasted=False, superseded=None)
                superseded = self._attention_revision if self._attention is not None else None
                self._activity_origin_run_id = origin_run_id
                self._activity_execution_owner = execution_owner
                # Input invalidates an observation even when the program does
                # not echo it (for example, a password prompt).
                self._screen_revision += 1
                # A wait's pattern matches what the program prints below this line.
                self._input_offset = self._log_size
                await self._mark_input()
                running = await self._running_before_input()
                *typed, last = chunks
                if typed:
                    writing = self._queue_input(typed[0], running, notify=True)
            if typed:
                write_error = await asyncio.shield(writing)
                if write_error is None:
                    text_sent = len(typed[0])
            if text_sent:
                await self._await_text_taken(self._services.monotonic())
            if write_error is None:
                async with self._lock:
                    if text_sent and (self.finished or self.finished_at is not None):
                        # The program ended on the text, as on "q" for quit.
                        return self._input_result(
                            text_sent,
                            key,
                            pasted=bracketed_paste,
                            superseded=superseded,
                            key_unsent="ended",
                        )
                    self._require_live()

                    def current_key() -> str:
                        # The queue may wait behind a previous write whose
                        # caller was cancelled. Use the mode at its own turn.
                        nonlocal last
                        (last,) = input_chunks(
                            data=None,
                            text=None,
                            key=key,
                            application_cursor=self._application_cursor,
                        )
                        return last

                    writing = self._queue_input(current_key if key else last, running, notify=True)
                write_error = await asyncio.shield(writing)
                if write_error is None:
                    async with self._lock:
                        if self._focus_reporting and last.endswith("\r"):
                            # The program that works on the submitted input,
                            # possibly one it starts, learns that its user is away.
                            self._queue_write(FOCUS_OUT_REPORT)
                        return self._input_result(
                            text_sent + len(last),
                            key,
                            pasted=bracketed_paste,
                            superseded=superseded,
                        )
        failure = await self._write_failure()
        if text_sent:
            return self._input_result(
                text_sent,
                key,
                pasted=bracketed_paste,
                superseded=superseded,
                key_unsent="ended" if isinstance(failure, TerminalClosedError) else "refused",
            )
        raise failure from write_error

    @contextlib.asynccontextmanager
    async def _input_state(self) -> AsyncGenerator[None]:
        """Lock input preparation after earlier physical writes have finished.

        In particular, a cancelled caller can leave its write running. Wait
        without holding the state lock, then check revision/program guards
        against the screen and program that the new input will reach.
        """
        while True:
            await self._lock.acquire()
            writing = self._writer_task
            if writing is None or writing.done():
                break
            self._lock.release()
            await asyncio.wait({writing})
        try:
            yield
        finally:
            self._lock.release()

    def _queue_input(
        self, chunk: str | Callable[[], str], running: frozenset[int] | None, *, notify: bool
    ) -> asyncio.Task[BaseException | None]:
        """Start an input cycle and queue its write; the state lock is held.

        The caller awaits the write outside the lock, so a program that
        prints while reading input cannot block its own output reader.
        """
        restart, changed = self._activity.input(
            self._services.monotonic(),
            notify=notify,
            delivery_pending=self._delivery_pending(),
            running=running,
        )
        writing = self._queue_write(chunk, input_chunk=True)
        self._restart_quiet(restart)
        self._output_event.set()
        self._signal_change()
        if changed:
            self._publish_state()
        return writing

    def _queue_write(
        self, text: str | Callable[[], str], *, input_chunk: bool = False
    ) -> asyncio.Task[BaseException | None]:
        """Serialize Agent, operator and protocol writes without blocking the reader.

        Session-owned tasks keep a cancelled caller's in-flight thread in
        order. Protocol replies only enqueue: awaiting them in the reader
        would deadlock a program that fills both PTY buffers.
        """
        previous = self._writer_task
        if input_chunk:
            self._input_writes += 1

        async def write() -> BaseException | None:
            try:
                if previous is not None and not previous.done():
                    await asyncio.wait({previous})
                async with self._lock:
                    if self.finished:
                        return EOFError("Terminal Session is no longer running")
                    chunk = text() if callable(text) else text
                try:
                    await asyncio.to_thread(self._adapter.write, chunk)
                except (EOFError, OSError) as error:
                    return error
                return None
            finally:
                if input_chunk:
                    async with self._lock:
                        self._input_writes -= 1
                        if not self._input_writes and not self.finished:
                            # Output may have advanced the generation while
                            # the write blocked. Settle the current one only.
                            self._restart_quiet(QuietRestart(self._activity.generation, False))
                        self._output_event.set()
                        self._signal_change()

        task = asyncio.create_task(write(), name=f"terminal:{self.terminal_id}:writer")

        def completed(done: asyncio.Task[BaseException | None]) -> None:
            if self._writer_task is done:
                self._writer_task = None
            _log_task_failure(
                done, f"Terminal input writing failed for terminal={self.terminal_id}"
            )

        task.add_done_callback(completed)
        self._writer_task = task
        return task

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
        async with self._input_lock:
            async with self._input_state():
                self._require_live()
                # Check the screen and program before enqueuing the input.
                if expected_program is not None and not await asyncio.to_thread(
                    self._services.program_probe, self._adapter.pid, expected_program
                ):
                    raise TerminalProgramNotRunningError(
                        f"{expected_program} is not running in this Terminal; "
                        "the input was not written"
                    )
                self._check_revision(expected_screen_revision)
                self._screen_revision += 1
                running = await self._running_before_input()
                writing = self._queue_input(data, running, notify=self.attachment is not None)
            write_error = await asyncio.shield(writing)
            if write_error is None:
                return
        raise await self._write_failure() from write_error

    async def _write_failure(self) -> TerminalManagerError:
        """The error for input the PTY refused, by whether the program still runs.

        Even after the program ended, its last output may still be in flight.
        The reader owns the natural end and closes the log after draining it;
        cancelling this input must not cancel that reader.
        """
        try:
            alive = await asyncio.to_thread(self._adapter.is_alive)
        except Exception:
            alive = True
        if alive:
            return TerminalManagerError(
                f"The input could not be written to terminal {self.terminal_id}"
            )
        reader = self._reader_task
        if reader is not None and not reader.done():
            # Waiting observes completion without propagating cancellation
            # either way: a kill may cancel the reader, not this input/Run.
            await asyncio.wait({reader})
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
                self._resizes.append((self._log_size, columns, rows))
                try:
                    await self._screen.resize(columns, rows)
                except Exception as error:
                    self._render_failed(error, self._log_size)
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
        """Return the screen, one history page and the facts an Agent result shows.

        ``screen`` marks reverse-video text and suggestions, ``cursor`` is the visible
        cursor's row and column (``ScreenObservation``); ``seen_line`` is the
        history line above which the attached Agent has seen every line (None
        before it saw any screen). ``log_file`` names the output log, and
        ``log_complete`` says whether it holds all output, and
        ``log_end_line`` the first history line it lacks when known. ``screen_behind``
        is the rendering error while the screen misses output.
        """
        async with self._lock:
            page = await self._screen.page(start_line=start_line, limit=lines)
            observation = await self._screen.observe()
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
            "screen": observation.screen,
            "cursor": observation.cursor,
            "scrollback": page,
            "attention": _attention_data(info.attention),
            "log_file": self._log_file(),
            "log_complete": not self._log_failed,
            "log_end_line": self._log_end_line,
            "screen_behind": None if self._render_failure is None else str(self._render_failure),
            "seen_line": self._seen_line,
            "idle_reason": self._activity.idle_reason,
            "observation": TerminalObservation(
                info.screen_revision,
                self._resize_count,
                info.columns,
                info.rows,
                observation.signature,
                _seen_through(observation, alternate=info.alternate_screen),
                self._clear_count,
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
        return data

    def acknowledge_screen(self, observation: TerminalObservation) -> None:
        """Remember a durably delivered screen without consuming a later resize.

        The Agent has seen it: an idle boundary showing the same screen wakes
        nobody, and an idle delivery of that screen on its way is cancelled
        (its boundary can come before the result is durable).
        """
        self._saw_lines(observation.seen_line, observation.clears)
        observed = self._observed
        if observed is None or (observation.resize_count, observation.screen_revision) >= (
            observed.resize_count,
            observed.screen_revision,
        ):
            self._observed = observation
            self._activity.acknowledge(observation.signature)
        attention = self._attention
        if (
            attention is not None
            and attention.kind == "idle"
            and attention.details.get("screen_revision") == observation.screen_revision
        ):
            self.acknowledge_attention(attention.revision)

    def _saw_lines(self, line: int, clears: int) -> None:
        """The attached Agent has now seen every history line above *line* of a
        screen shown after *clears* clears.

        A later clear erased that screen from its first line on, so the lines
        drawn there since are new to the Agent (code review R8: a late
        acknowledgement undid a clear). When those clears are no longer
        recorded, what the Agent saw stays as it was.
        """
        if clears < self._clear_count:
            later = [cleared for count, cleared in self._clears if count > clears]
            if len(later) < self._clear_count - clears:
                return
            line = min(line, *later)
        self._seen_line = max(self._seen_line or 0, line)

    def _cleared(self, line: int) -> None:
        """A clear erased the screen from history line *line* on: the Agent has
        not seen the lines drawn there next, although they get the same numbers."""
        self._clear_count += 1
        self._clears.append((self._clear_count, line))
        if self._seen_line is not None:
            self._seen_line = min(self._seen_line, line)

    def _log_file(self) -> str | None:
        """The output log a result can name; ``_log_failed`` says it ends early."""
        if self._log_path is None:
            return None
        return model_path(self._log_path)

    def acknowledge_attention(self, revision: int) -> None:
        """Cancel the equivalent automatic delivery after a manual result is durable."""
        attention = self._attention
        if (
            attention is None
            or attention.revision != revision
            or revision <= self._acknowledged_attention_revision
        ):
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

    async def _render_output(self, received: str) -> None:
        """Log, stream and render one chunk of output; the lock is held.

        A failing log or renderer degrades the Session instead of ending it:
        its output keeps being read, so the program neither stalls nor ends.
        """
        data, text = _output_forms(received)
        offset = self._log_size
        if self._write_log(data):
            self._log_end_line = await self._output_line()
        # Viewers get the bytes first; the snapshot a new viewer takes waits
        # for this lock, so it already contains them.
        self._publish_output(text)
        update = await self._render(text, offset)
        self._last_output_at = self._services.monotonic()
        facts_changed = False
        if update is not None:
            if update.cleared_line is not None:
                self._cleared(update.cleared_line)
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

    def _write_log(self, data: bytes) -> bool:
        """Append output to the log as the terminal received it; True when this
        write failed, which ends the log.

        Results that name the log then say that it lacks the output from
        there on. Reading the output goes on.
        """
        handle = self._log_handle
        if handle is None:
            return False
        try:
            handle.write(data)
            handle.flush()
            self._log_size += len(data)
        except OSError, ValueError:
            _LOGGER.warning(
                "Terminal output log write failed; the log ends here (terminal=%s)",
                self.terminal_id,
                exc_info=True,
            )
            self._log_handle = None
            self._log_failed = True
            with contextlib.suppress(OSError, ValueError):
                handle.close()
            return True
        return False

    async def _output_line(self) -> int | None:
        """The history line the next output goes to: the cursor's line, also
        while the program hides the cursor; None when the screen cannot tell,
        as while it misses output.

        A full-screen view keeps no history, so the primary screen's lines
        before it are complete.
        """
        if self._render_failure is not None:
            return None
        try:
            observation = await self._screen.observe()
        except Exception:
            return None
        if self._alternate_screen:
            return observation.screen_start_line
        return observation.cursor_line

    async def _render(self, text: str, offset: int) -> EmulatorUpdate | None:
        """Feed *text*, the output from log offset *offset* on, to the screen;
        None when the screen does not show it.

        Once drawing failed, the screen misses output, and results say so. A
        screen drawn anew from the log replaces it (``_redraw_screen``), which
        replays the log, so the output goes only there meanwhile. Without a
        complete log, the screen goes on with the new output; it lacks the
        output it missed until the program ends.
        """
        if self._render_failure is not None and self._redrawable():
            self._start_redraw()
            return None
        try:
            update = await self._screen.feed(text)
        except Exception as error:
            self._render_failed(error, offset)
            return None
        self._screen_current = True
        if update.skipped and _CONDITIONS.started((self.terminal_id, "skipped")):
            # A sequence the screen could not apply; it goes on with the rest.
            _LOGGER.warning(
                "Terminal screen skipped %d operations it could not apply (terminal=%s): %s",
                update.skipped,
                self.terminal_id,
                update.skipped_error,
            )
        return update

    def _render_failed(self, error: Exception, missed_from: int) -> None:
        """The screen missed the output from log offset *missed_from* on, or
        cannot be used; with a complete log, a screen drawn anew replaces it."""
        self._screen_current = False
        if self._render_failure is None:
            self._render_failure = error
            self._missed_from = missed_from
            if _CONDITIONS.started((self.terminal_id, "render")):
                _LOGGER.warning(
                    "Terminal screen rendering failed; the program keeps running and its "
                    "screen falls behind its output (terminal=%s)",
                    self.terminal_id,
                    exc_info=error,
                )
        if self._redrawable():
            self._start_redraw()

    async def _mark_input(self) -> None:
        """Mark the screen at the Agent's input; a screen drawn anew replays the mark."""
        if not self._screen_current:
            return
        try:
            await self._screen.mark_input()
        except Exception as error:
            self._render_failed(error, self._log_size)

    def _redrawable(self) -> bool:
        """The log holds every byte of output, so a new screen can replay it."""
        return self._log_path is not None and self._log_handle is not None and not self._log_failed

    def _start_redraw(self) -> None:
        task = self._redraw_task
        if task is None or task.done():
            self._redraw_task = self._background(self._redraw_screen(), "redraw")

    async def _redraw_screen(self) -> None:
        """Replace the screen that missed output with one drawn anew from the log;
        a failed attempt is tried again later while the Session runs."""
        services = self._services
        failures = 0
        while self._render_failure is not None and self._redrawable() and not self.finished:
            try:
                if await self._redraw_once():
                    return
            except Exception:
                _LOGGER.warning(
                    "Terminal screen could not be drawn anew from its log (terminal=%s)",
                    self.terminal_id,
                    exc_info=failures == 0,
                )
            failures += 1
            delay = services.activity_quiet_seconds * 2**failures
            await services.sleep(min(delay, _REDRAW_RETRY_MAX_SECONDS))

    async def _redraw_once(self) -> bool:
        """Draw a new screen from the log and put it in place of the screen;
        False when a redraw is no longer needed or possible.

        The log holds every byte of output, and the resizes and the last input
        are recorded at log offsets, so the new screen reaches the state the
        screen would have had without the failure. Output, input and resizes
        go on meanwhile: the replay follows them through the log, and finishes
        under the lock once little is left, so nothing comes between it and
        the swap.
        """
        path = self._log_path
        if path is None:
            return False
        columns, rows = self._first_size
        screen = await self._services.open_screen(columns, rows)
        replay = _LogReplay(screen, path, missed_from=self._missed_from)
        try:
            while True:
                async with self._lock:
                    if self._render_failure is None or not self._redrawable() or self.finished:
                        screen.close()
                        return False
                    end = self._log_size
                    resizes = tuple(self._resizes)
                    input_offset = self._input_offset
                    if end - replay.offset <= _REPLAY_LOCKED_BYTES:
                        await replay.run(end, resizes, input_offset)
                        await replay.finish()
                        await self._replace_screen(replay)
                        return True
                await replay.run(end, resizes, input_offset)
        except BaseException:
            screen.close()
            raise
        finally:
            replay.close()

    async def _replace_screen(self, replay: _LogReplay) -> None:
        """Put the screen *replay* drew in place of the one that missed output; the
        lock is held."""
        old, self._screen = self._screen, replay.screen
        old.close()
        self._render_failure = None
        self._screen_current = True
        update = replay.update
        if update is not None:
            if replay.cleared_line is not None:
                self._cleared(replay.cleared_line)
            # Replies to queries the program sent long ago would arrive as input now.
            await self._apply_update(update._replace(responses="", alternate_exited=False))
        # Its content can differ from what any result showed.
        self._screen_revision += 1
        if _CONDITIONS.ended((self.terminal_id, "render")):
            _LOGGER.info("Terminal screen drawn anew from its log (terminal=%s)", self.terminal_id)
        settle = self._settle_task
        if settle is None or settle.done():
            # The quiet boundary the screen missed while it was behind.
            generation = self._activity.generation
            self._settle_task = self._background(
                self._settle_after_quiet(generation), f"settle:{generation}"
            )
        self._publish_state()
        self._signal_change()

    async def _final_redraw(self) -> None:
        """Before the end is recorded, draw the final screen anew from the log."""
        task = self._redraw_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await asyncio.wait_for(self._redraw_once(), _FINAL_REDRAW_SECONDS)
        except Exception:
            _LOGGER.warning(
                "The final screen of terminal=%s could not be drawn anew from its log",
                self.terminal_id,
                exc_info=True,
            )

    async def _apply_update(self, update: EmulatorUpdate) -> bool:
        """Apply what rendering found; True when the title or the alternate screen changed."""
        if update.responses:
            # Terminal protocol replies, not input: they start no activity. Whether
            # a program that can no longer read them still runs is checked apart.
            self._queue_write(update.responses)
        paste_disabled = self._bracketed_paste and not update.bracketed_paste
        facts_changed = (update.title, update.alternate_screen) != (
            self._title,
            self._alternate_screen,
        )
        self._title = update.title
        self._bracketed_paste = update.bracketed_paste
        self._application_cursor = update.application_cursor
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
                if observation.screen and not self._screen_shown:
                    # The first screen also shows when the program's first
                    # output came right after a resize and counts as a redraw.
                    self._screen_shown = True
                    self._signal_change()
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
            seen=observed is not None and observed.screen_revision == self._screen_revision,
            blank=not observation.screen,
        )
        if effect is None:
            return False
        if effect.record:
            self._set_attention(
                kind="idle",
                summary="The program is idle.",
                details={"screen_revision": self._screen_revision, "reason": effect.reason},
                deliver=effect.deliver,
                observation=observation,
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
        if not self._screen_current or self._input_writes:
            return None
        try:
            if self._snapshot_on_settle:
                self._snapshot_on_settle = False
                await self._publish_snapshot()
            return await self._screen.observe()
        except Exception as error:
            self._render_failed(error, self._log_size)
            return None

    async def _mark_finished(self) -> None:
        """Record the end once no process of the Session runs any longer.

        A failure to read its output, if any, becomes its outcome. A screen
        that missed output is drawn anew from the log first; one that still
        misses output says so.
        """
        # A root process EOF cannot confirm that captured descendants exited.
        if self._termination_pending:
            return
        self._cancel_task(self._settle_task)
        self._cancel_task(self._sample_task)
        if self._render_failure is not None and self._redrawable() and not self.finished:
            await self._final_redraw()
        async with self._lock:
            if self.finished:
                return
            self.finished_at = self.finished_at or _utc_now()
            self.exit_code = await asyncio.to_thread(self._adapter.exit_code)
            # Release the terminal (on Windows also its console host and reader thread).
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self._adapter.close)
            error = self._read_failure
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
                observation: ScreenObservation | None = None
                with contextlib.suppress(Exception):
                    observation = await self._screen.observe()
                if error is None:
                    self._set_attention(
                        kind="exited",
                        summary=f"The program exited with code {self.exit_code}.",
                        details={"exit_code": self.exit_code},
                        observation=observation,
                    )
                else:
                    self._set_attention(
                        kind="error",
                        summary="The terminal failed.",
                        details={"error": str(error)},
                        observation=observation,
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
        observation: ScreenObservation | None,
    ) -> None:
        """Record attention, with the message that brings it and the screen it shows."""
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
        behind = None
        if self._render_failure is not None:
            behind = screen_behind_note(
                str(self._render_failure), self._log_file(), complete=not self._log_failed
            )
        unseen = None
        if observation is not None:
            unseen = unseen_lines_note(
                self.terminal_id,
                unseen_from=self._seen_line or 0,
                shown_from=observation.screen_start_line,
                first_line=observation.first_line,
                log=self._log_file(),
                log_complete=not self._log_failed,
                log_end_line=self._log_end_line,
                seen=True,
            )
        self._attention_body = _attention_body(
            self.terminal_id,
            self._notice_label(),
            attention,
            observation=observation,
            unseen=unseen,
            behind=behind,
            idle_seconds=self._services.idle_seconds,
        )
        self._attention_seen_line = (
            _seen_through(observation, alternate=self._alternate_screen)
            if observation is not None
            else 0
        )
        self._attention_clears = self._clear_count
        if kind in {"exited", "error"} and self._exit_acknowledged:
            # A result already showed the end.
            deliver = False
        if deliver:
            self._schedule_delivery(
                attention, self._attention_body, self._attention_seen_line, self._attention_clears
            )
        self._publish_state()
        self._signal_change()

    def _notice_label(self) -> str:
        info = self.info()
        label = " ".join((info.name or info.program).split())
        if len(label) > _NOTICE_LABEL_CHARS:
            return label[: _NOTICE_LABEL_CHARS - 3] + "..."
        return label

    def _schedule_delivery(
        self, attention: TerminalAttention, body: str, seen_line: int, clears: int
    ) -> None:
        """Deliver *body*, which shows the screen down to history line *seen_line*
        as it was after *clears* clears."""
        if self._services.trigger_service is None or self.attachment is None:
            return
        self._delivery_task = self._background(
            self._deliver(attention, body, seen_line, clears), f"attention:{attention.revision}"
        )

    async def _deliver(
        self, attention: TerminalAttention, body: str, seen_line: int, clears: int
    ) -> None:
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
        if self.attachment == attachment:
            self._saw_lines(seen_line, clears)
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
        pasted: bool,
        superseded: int | None,
        key_unsent: Literal["ended", "refused"] | None = None,
    ) -> dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "state": self.state,
            "characters_sent": characters,
            "key": key,
            # Why the key after typed text was not sent: the program had
            # ended, or the PTY refused it while the program runs.
            "key_unsent": key_unsent,
            # Multiline text went to the program as one bracketed paste.
            "pasted": pasted,
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
            with contextlib.suppress(OSError, ValueError):
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
    observation: ScreenObservation | None,
    unseen: str | None,
    idle_seconds: float,
    behind: str | None = None,
) -> str:
    """The message that tells the attached Agent its interactive program needs a look.

    It shows the whole screen; when the message would exceed its cap, the
    screen's top rows are left out, and the message says how many.
    """
    rows = observation.screen.split("\n") if observation is not None and observation.screen else []
    cursor = observation.cursor if observation is not None else None
    left_out = 0
    while True:
        body = _compose_attention(
            terminal_id,
            label,
            attention,
            rows=rows[left_out:],
            readable=observation is not None,
            cursor=cursor,
            left_out=left_out,
            unseen=unseen,
            behind=behind,
            idle_seconds=idle_seconds,
        )
        if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS or left_out >= len(rows):
            break
        left_out += 1
    if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS:
        return body
    return body[:TERMINAL_NOTICE_MESSAGE_CAP_CHARS] + "\n[message cut]"


def _compose_attention(
    terminal_id: str,
    label: str,
    attention: TerminalAttention,
    *,
    rows: list[str],
    readable: bool,
    cursor: tuple[int, int] | None,
    left_out: int,
    unseen: str | None,
    behind: str | None,
    idle_seconds: float,
) -> str:
    if not readable:
        screen = "(the screen could not be read)"
    elif rows:
        screen = _fenced("\n".join(rows))
    else:
        screen = "(the screen is empty)"
    if attention.kind == "idle":
        reason: IdleReason | None = attention.details.get("reason")
        shown = (
            "Screen"
            if cursor is None
            else f"Screen (cursor at row {cursor[0]}, column {cursor[1]})"
        )
        if reason == "fallback":
            headline = (
                f"Terminal {terminal_id} ({label}) has printed nothing for {idle_seconds:g} "
                f"seconds; its program still runs and can still be working. {shown}:"
            )
        else:
            headline = (
                f"Terminal {terminal_id} ({label}) went idle; its program still runs. {shown}:"
            )
        closing = [
            f'To type into it, call terminal with action "input", terminal_id "{terminal_id}", '
            'your text as text and key "enter". Its next new screen arrives the same way.'
        ]
    elif attention.kind == "exited":
        headline = (
            f"The program in terminal {terminal_id} ({label}) exited with code "
            f"{attention.details.get('exit_code')}. Last screen:"
        )
        closing = []
    else:
        headline = (
            f"Terminal {terminal_id} ({label}) failed and its program no longer runs: "
            f"{attention.details.get('error')}. Last screen:"
        )
        closing = []
    lines = [headline, screen]
    if left_out:
        status = json.dumps({"action": "status", "terminal_id": terminal_id})
        lines.append(
            f"The top {_count(left_out, 'line')} of the screen {_are(left_out)} left out; "
            f"to see the whole screen, call terminal {status}."
        )
    if behind:
        lines.append(behind)
    if unseen:
        lines.append(unseen)
    lines.extend(closing)
    return "\n".join(lines)


def screen_behind_note(error: str, log: str | None, *, complete: bool) -> str:
    """The screen misses output: drawing it failed."""
    note = (
        f"This screen is out of date: drawing the program's output failed ({error}), so the "
        "screen lacks some of that output."
    )
    if log is None:
        return note
    if complete:
        return f"{note} The output is in the log {log}."
    return f"{note} The log {log} has the output only until writing it failed."


def unseen_lines_note(
    terminal_id: str,
    *,
    unseen_from: int,
    shown_from: int,
    first_line: int,
    log: str | None,
    seen: bool,
    log_complete: bool = True,
    log_end_line: int | None = None,
) -> str | None:
    """Say how many output lines above what a message shows are left out, and
    where to read them; None when none are.

    Lines are history line numbers: *unseen_from* is the first line the
    reader has not seen (0 for all output), *shown_from* the first line the
    message shows, *first_line* the oldest line the terminal keeps. With
    *seen*, the reader is the attached Agent, whose seen lines are known.
    Without *log_complete*, the log ends where writing it failed: it lacks
    *log_end_line* and the lines after it, when that line is known.
    """
    count = shown_from - unseen_from
    if count <= 0:
        return None
    lines = _count(count, "earlier line")
    what = f"{lines} you have not seen" if seen else lines
    kept_from = max(unseen_from, first_line)
    kept = max(0, shown_from - kept_from)
    if log and log_complete:
        # The log has every line; the page is the newest of them the terminal
        # keeps, right above what the message shows (user decision D1).
        newest = min(kept, TERMINAL_STATUS_MAX_LINES)
        page = _status_page(terminal_id, shown_from - newest, newest)
        if newest == count:
            where = f"to read them, call {page}, or read the log {log}"
        else:
            where = f"to read them, read the log {log}" + (
                f"; the newest {newest} of them can also be read with {page}" if newest else ""
            )
        return f"{what} {_are(count)} not shown; {where}."
    page = _status_page(terminal_id, kept_from, TERMINAL_STATUS_MAX_LINES)
    if log and not log_complete:
        if log_end_line is not None:
            where = _partial_log_sources(
                count, kept, max(0, min(shown_from, log_end_line) - unseen_from), page, log
            )
            return f"{what} {_are(count)} not shown; {where}."
        log = f"{log}, which ends where writing it failed"
    if kept == count:
        where = f"to read them, call {page}" + (f", or read the log {log}" if log else "")
    elif log:
        where = f"to read them, read the log {log}" + (
            f"; the newest {kept} of them can also be read with {page}" if kept else ""
        )
    elif kept:
        where = f"the newest {kept} of them can be read with {page}; the others are no longer kept"
    else:
        where = "they are no longer kept"
    return f"{what} {_are(count)} not shown; {where}."


def _status_page(terminal_id: str, start_line: int, lines: int) -> str:
    """The status call that reads *lines* history lines from *start_line*."""
    page = {
        "action": "status",
        "terminal_id": terminal_id,
        "start_line": start_line,
        "lines": lines,
    }
    return "terminal " + json.dumps(page)


def _partial_log_sources(count: int, kept: int, logged: int, page: str, log: str) -> str:
    """Where *count* unseen lines can be read when writing the log failed: the
    oldest *logged* of them are in the log, the newest *kept* in the terminal;
    the others are lost."""
    if kept == count:
        return f"to read them, call {page}" + (
            f", or read the log {log}" if logged == count else ""
        )
    unkept = count - kept
    in_log = min(logged, unkept)
    lost = unkept - in_log
    parts: list[str] = []
    if in_log == count:
        parts.append(f"to read them, read the log {log}")
    elif in_log:
        parts.append(f"the oldest {_lines(in_log)} {_are(in_log)} in the log {log}")
    if lost == count:
        parts.append(f"they are no longer kept, and writing the log {log} failed before them")
    elif lost:
        subject = f"{'the next' if in_log else 'the oldest'} {_lines(lost)}"
        named = "the log" if in_log else f"the log {log}"
        them = "it" if lost == 1 else "them"
        parts.append(
            f"{subject} {_are(lost)} no longer kept, and writing {named} failed before {them}"
        )
    if kept:
        parts.append(f"the newest {_lines(kept)} can be read with {page}")
    return "; ".join(parts)


def _lines(number: int) -> str:
    """Some of the lines a note counts: "line" or "7 lines"."""
    return "line" if number == 1 else f"{number} lines"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _are(number: int) -> str:
    return "is" if number == 1 else "are"


def _seen_through(observation: ScreenObservation, *, alternate: bool) -> int:
    """The history line below the last screen row *observation* shows.

    Showing the screen shows its rows, which scroll into history as these
    lines; a full-screen view (alternate screen) shows none of them.
    """
    if alternate or not observation.screen:
        return observation.screen_start_line
    return observation.screen_start_line + observation.screen.count("\n") + 1


# A UTF-16 surrogate: a byte that was no UTF-8 (POSIX decodes such bytes as
# lone surrogates, surrogateescape), or half of a character that never came.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _output_forms(received: str) -> tuple[bytes, str]:
    """The bytes the terminal received for one chunk of output, and its text.

    The log keeps the bytes as they came; the screen and viewers show U+FFFD
    for a byte that was no UTF-8.
    """
    if received.isascii():
        return received.encode("ascii"), received
    try:
        data = received.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        data = received.encode("utf-8", "surrogatepass")
    return data, _SURROGATE.sub("\ufffd", received)


class _LogReplay:
    """Draws a new screen from a Session's output log, piece by piece."""

    def __init__(self, screen: TerminalScreen, path: Path, *, missed_from: int) -> None:
        self.screen = screen
        self._path = path
        self._file: BinaryIO | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self._missed_from = missed_from
        # The log offset replayed up to, and how many resizes were applied.
        self.offset = 0
        self._resized = 0
        self._marked: int | None = None
        # The update of the last output replayed, and the first line a clear
        # erased in the output the old screen missed.
        self.update: EmulatorUpdate | None = None
        self.cleared_line: int | None = None

    async def run(
        self, end: int, resizes: Sequence[tuple[int, int, int]], input_offset: int | None
    ) -> None:
        """Replay the log up to offset *end*, with each resize and the input mark
        where they happened."""
        while True:
            while self._resized < len(resizes) and resizes[self._resized][0] <= self.offset:
                _at, columns, rows = resizes[self._resized]
                await self.screen.resize(columns, rows)
                self._resized += 1
            if (
                input_offset is not None
                and input_offset <= self.offset
                and self._marked != input_offset
            ):
                await self.screen.mark_input()
                self._marked = input_offset
            if self.offset >= end:
                return
            stop = min(end, self.offset + _REPLAY_PIECE_BYTES)
            upcoming = resizes[self._resized][0] if self._resized < len(resizes) else None
            for boundary in (upcoming, input_offset, self._missed_from):
                if boundary is not None and self.offset < boundary < stop:
                    stop = boundary
            data = await asyncio.to_thread(self._read, self.offset, stop - self.offset)
            if len(data) != stop - self.offset:
                raise OSError(f"the log ends at byte {self.offset + len(data)}, before byte {stop}")
            missed = self.offset >= self._missed_from
            self.offset = stop
            text = _SURROGATE.sub("\ufffd", self._decoder.decode(data))
            if not text:
                continue
            update = await self.screen.feed(text)
            self.update = update
            cleared = update.cleared_line
            if missed and cleared is not None:
                self.cleared_line = (
                    cleared if self.cleared_line is None else min(self.cleared_line, cleared)
                )

    def _read(self, offset: int, size: int) -> bytes:
        if self._file is None:
            self._file = self._path.open("rb")
        self._file.seek(offset)
        return self._file.read(size)

    async def finish(self) -> None:
        """Render any incomplete UTF-8 tail before handing the screen over.

        Piece boundaries keep decoder state; only the final swap ends replay.
        The raw log keeps these bytes, and the screen shows replacement
        characters just as it does for undecodable live output.
        """
        text = _SURROGATE.sub("\ufffd", self._decoder.decode(b"", final=True))
        if text:
            self.update = await self.screen.feed(text)

    def close(self) -> None:
        """Close the log; the screen stays open."""
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


def _fenced(text: str) -> str:
    """Screen text in a fence that no line of it can close."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}"


def _log_task_failure(task: asyncio.Task[Any], message: str) -> None:
    if task.cancelled() or (error := task.exception()) is None:
        return
    _LOGGER.error("%s: %s", message, error, exc_info=(type(error), error, error.__traceback__))
