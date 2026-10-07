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
from typing import Any, Literal, TextIO

from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease
from core.tools import terminal_backend
from core.tools.terminal_backend import TerminalAdapter
from core.tools.terminal_emulator import EmulatorUpdate
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._terminal_activity import QuietRestart, TerminalActivity
from ._terminal_command import (
    COMMAND_IDLE_CPU_SECONDS,
    COMMAND_IDLE_SECONDS,
    COMMAND_LEFTOVER_QUIET_SECONDS,
    COMMAND_LOG_REFRESH_SECONDS,
    COMMAND_STOP_GRACE_SECONDS,
    COMMAND_TREE_POLL_SECONDS,
    CommandReport,
    CommandState,
    StopReason,
)
from ._terminal_input import input_chunks
from ._terminal_process_tree import ProcessTreeFacts
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
    WaitEnded,
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
# After the shell exited, and after the last process of a command ended, its
# last output is read for at most this long.
_COMMAND_DRAIN_SECONDS = 1.0
# A command's progress tail is published at most this often.
_COMMAND_PROGRESS_SECONDS = 0.5
_COMMAND_PROGRESS_LINES = 40
# Output and input must pause this long before the idle CPU baseline is taken.
_COMMAND_IDLE_BASELINE_SECONDS = 1.0
# While a wait finds no idle baseline yet, it checks for one this often.
_COMMAND_QUIET_POLL_SECONDS = 0.5
# After a match, a command's wait lingers this long for its exit: a final line
# such as "BUILD OK" then reports the exit code instead of a running command.
_COMMAND_MATCH_EXIT_SECONDS = 2.0
# Labels in Agent notices are cut to this many characters.
_NOTICE_LABEL_CHARS = 60
# Failures that persist across chunks or polls, keyed by (terminal id, kind):
# logged when they start and when they end, not on every repetition.
_CONDITIONS = LoggedConditions()

CommandWaitOutcome = Literal["exited", "deadline", "idle"]


@dataclass(slots=True)
class _QuietWatch:
    """A command's idle tracking: the activity count its quiet period belongs to,
    and the time, tree CPU and started processes when its baseline was taken."""

    activity_count: int = -1
    since: float | None = None
    cpu_seconds: float = 0.0
    started_processes: int = 0


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
        # Quiet boundaries so far: output paused for the quiet period after activity.
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
        # vBot, the user or an Agent stopped the interactive program.
        self._stopped = False
        # A result durably showed the program's end: its exit is not delivered.
        self._exit_acknowledged = False
        # Set and replaced whenever output, input, attention or the program's
        # end changes what a waiter can observe.
        self._change = asyncio.Event()
        # A command's exit attention, held until the session has finished.
        self._command_attention: dict[str, Any] | None = None
        self._termination_pending = False
        self._termination_targets: list[Any] = []
        self._output_event = asyncio.Event()
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
        # A command's log file lacks output rendered after it last showed the pending rows.
        self._log_stale = False
        self._log_shown_at = -COMMAND_LOG_REFRESH_SECONDS
        # Output and input: a command is idle only while neither happens.
        self._activity_count = 0
        self._last_activity_at = self._last_output_at
        self._quiet = _QuietWatch()
        self._shell_dead_at: float | None = None
        self._tree_dead_at: float | None = None
        # Set once vBot killed the process tree.
        self._tree_killed = asyncio.Event()
        # Why output can no longer be read, and why the screen fell behind it;
        # either becomes the Session's failure once its processes have ended.
        self._read_failure: Exception | None = None
        self._render_failure: Exception | None = None
        # Set once the session finished: none of its processes runs any longer.
        self._finished_event = asyncio.Event()
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
            stopped=self._stopped
            if self._command is None
            else self._command.stop_reason is not None,
            shell_command=None if self._command is None else self._command.command,
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
            self._initial_input_task.add_done_callback(lambda _task: self._signal_change())
        self._publish_state()

    async def terminate(
        self, *, suppress_attention: bool, reason: StopReason | None = None
    ) -> None:
        """Stop the process tree; a failure leaves the Session unfinished for a retry.

        For a command, *reason* records why vBot stopped it (the Agent's own
        kill is ``agent``).
        """
        self._cancel_task(self._initial_input_task)
        if not self.finished:
            self._stopped = True
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
        for kind in ("render", "end_check"):
            _CONDITIONS.ended((self.terminal_id, kind))

    # Commands

    async def wait_command(
        self,
        *,
        deadline: float | None,
        idle_seconds: float | None,
        progress: Callable[[str], Awaitable[None]] | None,
    ) -> CommandWaitOutcome:
        """Wait until the command ends, *deadline* passes or it turns idle.

        The command ends once its shell exited and every process it left
        running ended or went quiet (``_settle_leftovers``).

        *deadline* is a ``monotonic`` time. Idle means no output and less
        than a trace of CPU time in the whole process tree for *idle_seconds*;
        only a command whose shell still runs turns idle.
        *progress* receives the screen's newest rows while output arrives.
        """
        command = self._require_command()
        services = self._services
        published_output = -1
        next_progress = 0.0
        while not command.has_ended:
            now = services.monotonic()
            if deadline is not None and now >= deadline:
                return "deadline"
            if (
                idle_seconds is not None
                and not command.shell_exited
                and await self._command_idle(command, idle_seconds)
            ):
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
            await self._wait_or_sleep(command.ended, pause)
        return "exited"

    async def command_idle_seconds(self) -> float | None:
        """How long the running command has been idle, counted from its last output or input.

        None while it works: it printed output, got input, used CPU or
        started a process within ``COMMAND_IDLE_SECONDS``, or once its shell exited.
        """
        command = self._require_command()
        if command.shell_exited or not await self._command_idle(command, COMMAND_IDLE_SECONDS):
            return None
        return self._services.monotonic() - self._last_activity_at

    async def _command_idle(self, command: CommandState, idle_seconds: float) -> bool:
        """Whether the command printed nothing, got no input and its tree used no CPU
        for *idle_seconds*."""
        if not await self._quiet_baseline(command):
            return False
        quiet = self._quiet
        now = self._services.monotonic()
        if quiet.since is None or now - quiet.since < idle_seconds:
            return False
        activity = self._activity_count
        facts = await asyncio.to_thread(command.tree_facts)
        if facts is None or activity != self._activity_count:
            return False
        if (
            abs(facts.cpu_seconds - quiet.cpu_seconds) > COMMAND_IDLE_CPU_SECONDS
            or facts.started != quiet.started_processes
        ):
            # CPU time or a new process is work, and so is an exit that took its
            # CPU time out of the sum (POSIX counts running processes only): the
            # quiet period starts again.
            quiet.since = now
            quiet.cpu_seconds = facts.cpu_seconds
            quiet.started_processes = facts.started
            return False
        return True

    async def _quiet_baseline(self, command: CommandState) -> bool:
        """Take the CPU baseline of the current quiet period; True once it is taken.

        Output or input ends a quiet period; the next one's baseline is taken
        once both have paused for a moment. Every check of the command's
        idleness shares it: the reader, a wait, and a status.
        """
        quiet = self._quiet
        if quiet.activity_count != self._activity_count:
            quiet.activity_count = self._activity_count
            quiet.since = None
        if quiet.since is not None:
            return True
        now = self._services.monotonic()
        if now - self._last_activity_at < _COMMAND_IDLE_BASELINE_SECONDS:
            return False
        activity = self._activity_count
        facts = await asyncio.to_thread(command.tree_facts)
        if facts is None or activity != self._activity_count:
            return False
        if quiet.since is None:
            quiet.since = now
            quiet.cpu_seconds = facts.cpu_seconds
            quiet.started_processes = facts.started
        return True

    def _note_activity(self) -> None:
        """Output arrived or input was written: a command is not idle now."""
        self._activity_count += 1
        self._last_activity_at = self._services.monotonic()

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

    async def wait_finished(self) -> None:
        """Wait until none of the session's processes runs any longer."""
        await self._finished_event.wait()

    async def wait_for_program(
        self,
        *,
        deadline: float,
        pattern: re.Pattern[str] | None,
        after_revision: int,
        on_match: Callable[[str], None] | None = None,
    ) -> WaitEnded:
        """Wait until the program exits, its output matches *pattern*, its new output
        settles, or *deadline* (a ``monotonic`` time) passes.

        A command exits when it ends; its wait ends only then, at a match,
        or at the deadline. After a match, a command's wait lingers
        briefly and ends ``exited`` when the command ends within that time.
        Output printed before the call counts for
        *pattern*, except output before the Agent's last input and that
        input's echo. An interactive program's new output settled when an
        ``output_settled`` attention above *after_revision* exists; with
        *pattern* that does not end the wait. *on_match* receives the line
        where the match starts.
        """
        command = self._command
        services = self._services
        match_from: int | None = None
        while True:
            change = self._change
            if self.finished or (command is not None and command.has_ended):
                return "exited"
            if pattern is not None:
                matched, match_from = await self._output_matches(pattern, match_from)
                if matched is not None:
                    if on_match is not None:
                        on_match(matched)
                    if command is None:
                        return "matched"
                    return await self._linger_for_exit(command, deadline)
            attention = self._attention
            if (
                command is None
                and pattern is None
                and attention is not None
                and attention.kind == "output_settled"
                and attention.revision > after_revision
            ):
                return "quiet"
            now = services.monotonic()
            if now >= deadline:
                return "timeout"
            pause = deadline - now
            if command is not None and not await self._quiet_baseline(command):
                # Output can pause without a change to wake this wait: take the
                # idle baseline in time, so the result can tell an idle command.
                pause = min(pause, _COMMAND_QUIET_POLL_SECONDS)
            await self._wait_or_sleep(change, pause)

    async def _linger_for_exit(self, command: CommandState, deadline: float) -> WaitEnded:
        """After a match, wait briefly for the command's exit; ``exited`` if it came."""
        services = self._services
        until = min(deadline, services.monotonic() + _COMMAND_MATCH_EXIT_SECONDS)
        while True:
            change = self._change
            if self.finished or command.has_ended:
                return "exited"
            now = services.monotonic()
            if now >= until:
                return "matched"
            await self._wait_or_sleep(change, until - now)

    async def wait_for_reply(self, *, deadline: float, after_quiet: int) -> WaitEnded:
        """Wait until the output settles after input, the program exits, or *deadline* passes.

        *after_quiet* is the ``quiet_boundaries`` count the input reported:
        the output settled once a quiet boundary follows it.
        """
        command = self._command
        while True:
            change = self._change
            if self.finished or (command is not None and command.has_ended):
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

    def command_report(self) -> CommandReport:
        command = self._require_command()
        return command.report(self.terminal_id, self._services.monotonic())

    async def command_view(self, rows: int) -> tuple[CommandReport, str]:
        """The command's report and the output that follows its transcript.

        While the shell runs, that output is the screen's newest *rows* rows;
        after it exited, the rows the transcript does not hold yet. Output is
        rendered and the transcript extended under the session lock, which
        this holds, so the two join without gap or overlap.
        """
        command = self._require_command()
        async with self._lock:
            if self.finished and command.has_ended:
                following = ""
            elif command.shell_exited:
                following = "\n".join(await self._screen.pending_transcript())
            else:
                following = (await self._screen.observe(rows)).tail
            return command.report(self.terminal_id, self._services.monotonic()), following

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
        if self._tree_dead_at is not None:
            # Read the processes' last output until it pauses, for a bounded time.
            return not read_timed_out and now - self._tree_dead_at < _COMMAND_DRAIN_SECONDS
        if now < self._next_tree_poll:
            return True
        self._next_tree_poll = now + COMMAND_TREE_POLL_SECONDS
        facts = await asyncio.to_thread(command.tree_facts)
        # A tree that cannot be inspected now may still run.
        if facts is None:
            return True
        if facts.running:
            await self._settle_leftovers(command, facts)
            return True
        self._tree_dead_at = now
        return not read_timed_out

    async def _settle_leftovers(self, command: CommandState, facts: ProcessTreeFacts) -> None:
        """End the command once the processes its exited shell left running went
        quiet: no output, no new process and no CPU for
        ``COMMAND_LEFTOVER_QUIET_SECONDS``, like a server waiting for requests.

        Processes that work hold the command until they end or go quiet, so a
        program the shell did not wait for, such as a GUI-subsystem program run
        from PowerShell, ends the command only with its own output.
        """
        if command.has_ended or not await self._command_idle(
            command, COMMAND_LEFTOVER_QUIET_SECONDS
        ):
            return
        async with self._lock:
            await self._record_command_end(facts)

    async def _keep_idle_baseline(self) -> None:
        """While output pauses, take the idle baseline, so a status or wait can tell
        an idle command."""
        command = self._command
        if command is not None and not command.shell_exited:
            await self._quiet_baseline(command)

    def _shell_exit_code(self) -> int | None:
        # A liveness check makes a stopped Windows shell's exit status known.
        with contextlib.suppress(Exception):
            self._adapter.is_alive()
        try:
            return self._adapter.exit_code()
        except Exception:
            return None

    async def _await_end(self) -> None:
        """Wait until the Session's processes have ended, or vBot killed them.

        Output has ended or can no longer be read, so the processes are polled:
        an interactive program until it exits, a command until its shell exited
        and every process it started ended.
        """
        while not self._tree_killed.is_set():
            if await self._processes_ended():
                return
            await self._wait_or_sleep(self._tree_killed, COMMAND_TREE_POLL_SECONDS)

    async def _processes_ended(self) -> bool:
        """Whether every process of the Session has ended; False while that is unknown."""
        key = (self.terminal_id, "end_check")
        try:
            ended = await self._check_processes_ended()
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

    async def _check_processes_ended(self) -> bool:
        if await asyncio.to_thread(self._adapter.is_alive):
            return False
        command = self._command
        if command is None:
            return True
        async with self._lock:
            await self._record_shell_exit()
        tree = command.tree
        if tree is None:
            return True
        # Raises when the tree cannot be inspected.
        facts = await asyncio.to_thread(tree.facts)
        if facts.running:
            await self._settle_leftovers(command, facts)
            return False
        return True

    async def _commit_output(self) -> None:
        """Add the screen's output the transcript does not hold yet; the lock is held."""
        command = self._require_command()
        try:
            command.add_lines(await self._screen.commit_transcript())
            # Every rendered row is final now; none is pending.
            command.show_pending(())
        except Exception:
            _LOGGER.warning(
                "Could not render the final output of command terminal=%s",
                self.terminal_id,
                exc_info=True,
            )

    async def _record_shell_exit(self) -> None:
        """Fix the shell's exit code when it exits; the lock is held.

        Without processes left running, the command ends with it. Otherwise
        it ends once they ended or went quiet, and the timeout stays armed:
        it stops them.
        """
        command = self._require_command()
        if command.shell_exited:
            return
        exit_code = await asyncio.to_thread(self._shell_exit_code)
        await self._commit_output()
        facts = await asyncio.to_thread(command.tree_facts)
        command.record_exit(exit_code, facts)
        self.exit_code = exit_code
        if facts is None or not facts.running:
            await self._record_command_end(facts)
        self._signal_change()

    async def _record_command_end(self, facts: ProcessTreeFacts | None) -> None:
        """Fix the command's outcome and set its exit attention; the lock is held.

        *facts* show the processes that still run, quiet ones the shell left
        running; their later output still enters the transcript.
        """
        command = self._require_command()
        await self._commit_output()
        if command.has_ended:
            return
        command.record_end(facts, now=self._services.monotonic())
        report = command.report(self.terminal_id, self._services.monotonic())
        exit_code = command.exit_code
        reason = command.stop_reason
        summary = (
            f"Command exited with code {exit_code}."
            if reason is None
            else f"Command stopped ({reason})."
        )
        attention: dict[str, Any] = {
            "kind": "exited",
            "summary": summary,
            "details": {"exit_code": exit_code, "stop_reason": reason},
            "deliver": command.delivers_result
            and not self._suppress_exit_attention
            and not self._exit_acknowledged
            and reason != "agent",
            "body": command.formatter(report),
        }
        # The session's own end later adds no second attention.
        self._suppress_exit_attention = True
        if report.still_running:
            # Processes the command started keep the terminal live: tell it now.
            self._set_attention(**attention)
        else:
            # The session finishes next; a waiter woken then sees it exited.
            self._command_attention = attention
        self._signal_change()

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
            # A wait's pattern matches what the program prints below this line.
            await self._screen.mark_input()
            self._note_activity()
            # A command's result is delivered when it ends, not when its output settles.
            restart, changed = self._activity.input(
                notify=self._command is None, delivery_pending=self._delivery_pending()
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
                self._signal_change()
                if changed:
                    self._publish_state()
                return self._input_result(
                    sum(len(chunk) for chunk in chunks),
                    key,
                    bracketed_paste=bracketed_paste,
                    superseded=superseded,
                )
        raise await self._write_failure() from write_error

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
            self._note_activity()
            restart, changed = self._activity.input(
                notify=self.attachment is not None and self._command is None,
                delivery_pending=self._delivery_pending(),
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

        An interactive program that has ended finishes the Session now. A
        command's Session finishes once every process it started has ended,
        which the reader notices.
        """
        try:
            alive = await asyncio.to_thread(self._adapter.is_alive)
        except Exception:
            alive = True
        if alive:
            return TerminalManagerError(
                f"The input could not be written to terminal {self.terminal_id}"
            )
        if self._command is None:
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
        data["program"] = info.program
        data["stopped"] = info.stopped
        data["kind"] = info.kind
        initial_input = self._initial_input_task
        data["initial_input_pending"] = initial_input is not None and not initial_input.done()
        typed_at = self._initial_input_quiet
        # Start's text was typed and the output after it has not settled yet.
        data["initial_output_pending"] = (
            typed_at is not None and self._quiet_boundaries <= typed_at and not self.finished
        )
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
        process has ended (a command's: its whole tree), or once vBot killed
        it, which ends the wait or cancels this task.
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
        """Read and render output until it ends or the command's processes ended.

        An unexpected failure ends only the reading: it becomes the Session's
        failure, and the program keeps running until it ends or is stopped.
        """
        loop = asyncio.get_running_loop()
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
                            return
                        if self._log_stale:
                            async with self._lock:
                                await self._refresh_log()
                        await self._keep_idle_baseline()
                    elif not await asyncio.to_thread(self._adapter.is_alive):
                        return
                    continue
                if text:
                    async with self._lock:
                        await self._render_output(text)
                        await self._refresh_log()
                if command and not await self._command_alive(read_timed_out=False):
                    return
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
        self._log_stale = self._command is not None
        self._note_activity()
        facts_changed = False
        if update is not None:
            facts_changed = await self._apply_update(update)
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

    async def _refresh_log(self) -> None:
        """Show the command's rendered rows that are not final yet in its log file,
        at most every ``COMMAND_LOG_REFRESH_SECONDS``; the lock is held."""
        command = self._command
        now = self._services.monotonic()
        if (
            command is None
            or not self._log_stale
            or now - self._log_shown_at < COMMAND_LOG_REFRESH_SECONDS
        ):
            return
        self._log_stale = False
        self._log_shown_at = now
        try:
            command.show_pending(await self._screen.pending_transcript())
        except Exception:
            _LOGGER.warning(
                "Could not render the pending output of command terminal=%s",
                self.terminal_id,
                exc_info=True,
            )

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
        if self._command is not None:
            self._command.add_lines(update.transcript)
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

    async def _settle_after_quiet(self, generation: int) -> None:
        try:
            await self._services.sleep(self._services.activity_quiet_seconds)
            async with self._lock:
                # A screen behind its output cannot show that the output settled.
                if (
                    generation != self._activity.generation
                    or self.finished
                    or self._render_failure is not None
                ):
                    return
                if self._snapshot_on_settle:
                    self._snapshot_on_settle = False
                    await self._publish_snapshot()
                try:
                    observation = await self._screen.observe(TERMINAL_DELIVERY_TAIL_LINES)
                except Exception as error:
                    self._render_failed(error)
                    return
                effect = self._activity.settle(
                    generation, observation.signature, attached=self.attachment is not None
                )
                if effect is None:
                    return
                self._settled = (self._screen_revision, observation.signature)
                if not effect.record:
                    return
                self._quiet_boundaries += 1
                if self._command is not None:
                    # A command's output is its result, delivered when it ends.
                    self._signal_change()
                    return
                details = {"screen_revision": self._screen_revision}
                self._set_attention(
                    kind="output_settled",
                    summary="Output settled after activity.",
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
        async with self._lock:
            if self.finished:
                return
            command = self._command
            if command is not None:
                await self._record_shell_exit()
                facts = await asyncio.to_thread(command.tree_facts)
                command.tree_ended()
                await self._record_command_end(
                    None if facts is None else replace(facts, running=())
                )
            self.finished_at = self.finished_at or _utc_now()
            if self._command is None:
                self.exit_code = await asyncio.to_thread(self._adapter.exit_code)
            # Release the terminal (on Windows also its console host and reader
            # thread) and the timeout, which has nothing left to stop.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self._adapter.close)
            self._cancel_task(self._timeout_task)
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
            if self._command_attention is not None:
                attention, self._command_attention = self._command_attention, None
                self._set_attention(**attention)
            elif self._suppress_exit_attention:
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
            self._finished_event.set()
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
            else _attention_body(
                self.terminal_id, self._notice_label(), attention, screen_tail=screen_tail
            )
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
            # A wait for the reply ends at the next quiet boundary after this count.
            "quiet_boundaries": self._quiet_boundaries,
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
    terminal_id: str, label: str, attention: TerminalAttention, *, screen_tail: str
) -> str:
    """The message that tells the attached Agent its interactive program needs a look."""
    if attention.kind == "output_settled":
        lines = [
            f"The output of terminal {terminal_id} ({label}) settled after activity; "
            "this does not mean the program finished. Screen:",
            _fenced(screen_tail),
            f'To type into it, call terminal with action "input", terminal_id "{terminal_id}", '
            'your text as text and key "enter". When its output settles again, the new screen '
            "arrives as another message.",
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
