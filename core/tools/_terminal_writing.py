"""Interactive Terminal input and ordered PTY writes."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal

from core.runs import RunExecutionOwner
from core.tools.terminal_backend import TerminalAdapter
from core.tools.terminal_emulator import (
    FOCUS_IN_REPORT,
    FOCUS_OUT_REPORT,
    EmulatorUpdate,
)
from core.utils.logging import get_logger

from ._terminal_activity import (
    QuietRestart,
    TerminalActivity,
)
from ._terminal_attention import TerminalNotices
from ._terminal_input import input_chunks
from ._terminal_output import TerminalOutput
from ._terminal_state import (
    TerminalClosedError,
    TerminalInfo,
    TerminalManagerError,
    TerminalProgramNotRunningError,
    TerminalSessionServices,
    TerminalStaleScreenError,
    _log_task_failure,
)
from ._terminal_waits import TerminalWaits

_ECHO_PAUSE_SHARE = 0.125
_ECHO_LIMIT_SHARE = 0.5
_LOGGER = get_logger("tools.terminal_manager")


class TerminalWriting:
    """Own ordered physical writes, exact/typed input, live modes and paste pacing."""

    def __init__(
        self,
        terminal_id: str,
        *,
        adapter: TerminalAdapter,
        output: TerminalOutput,
        notices: TerminalNotices,
        waits: TerminalWaits,
        activity: TerminalActivity,
        services: TerminalSessionServices,
        lock: asyncio.Lock,
        info: Callable[[], TerminalInfo],
        reader: Callable[[], asyncio.Task[None] | None],
        before_input: Callable[[], Awaitable[frozenset[int] | None]],
        restart: Callable[[QuietRestart], None],
        publish: Callable[[], None],
        output_event: asyncio.Event,
        execution_owner: RunExecutionOwner | None,
    ) -> None:
        self.terminal_id = terminal_id
        self._adapter = adapter
        self._output = output
        self._notices = notices
        self._waits = waits
        self._activity = activity
        self._services = services
        self._lock = lock
        self._info = info
        self._reader = reader
        self._before_input = before_input
        self._restart_quiet = restart
        self._publish_state = publish
        self._output_event = output_event
        self._input_lock = asyncio.Lock()
        self.writer_task: asyncio.Task[BaseException | None] | None = None
        self.writes = 0
        self.bracketed_paste = False
        self.application_cursor = False
        self.focus_reporting = False
        self.focused = False
        self.origin_run_id: str | None = None
        self.execution_owner = execution_owner
        self.last_output_at = services.monotonic()

    def observe_modes(self, update: EmulatorUpdate) -> bool:
        paste_disabled = self.bracketed_paste and not update.bracketed_paste
        self.bracketed_paste = update.bracketed_paste
        self.application_cursor = update.application_cursor
        self.focus_reporting = update.focus_reporting
        if FOCUS_OUT_REPORT in update.responses:
            self.focused = False
        return paste_disabled

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
                self.require_live()
                self._check_revision(expected_screen_revision)
                bracketed_paste = (
                    text is not None and ("\n" in text or "\r" in text) and self.bracketed_paste
                )
                chunks = input_chunks(
                    data=data,
                    text=text,
                    key=key,
                    bracketed_paste=bracketed_paste,
                    application_cursor=self.application_cursor,
                )
                if not chunks:
                    # An empty write changes no activity or observation.
                    return self._input_result(0, key, pasted=False, superseded=None)
                superseded = self._notices.revision if self._notices.current is not None else None
                self.origin_run_id = origin_run_id
                self.execution_owner = execution_owner
                # Input invalidates an observation even when the program does
                # not echo it (for example, a password prompt).
                self._output.revision += 1
                # A wait's pattern matches what the program prints below this line.
                self._output.input_offset = self._output.log.size
                await self._output.mark_input()
                running = await self._before_input()
                if self.focus_reporting and not self.focused:
                    # Like a user typing, the Agent has the focus until it
                    # saw the reply; programs ignore keys or drop the focused
                    # widget while the terminal has no focus.
                    self.focused = True
                    self.queue_protocol(FOCUS_IN_REPORT)
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
                    if text_sent and (
                        self._activity.finished or self._info().finished_at is not None
                    ):
                        # The program ended on the text, as on "q" for quit.
                        return self._input_result(
                            text_sent,
                            key,
                            pasted=bracketed_paste,
                            superseded=superseded,
                            key_unsent="ended",
                        )
                    self.require_live()

                    def current_key() -> str:
                        # The queue may wait behind a previous write whose
                        # caller was cancelled. Use the mode at its own turn.
                        nonlocal last
                        (last,) = input_chunks(
                            data=None,
                            text=None,
                            key=key,
                            application_cursor=self.application_cursor,
                        )
                        return last

                    writing = self._queue_input(current_key if key else last, running, notify=True)
                write_error = await asyncio.shield(writing)
                if write_error is None:
                    async with self._lock:
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

    async def release_focus(self) -> None:
        """Tell a program that Agent input gave the focus that its user is away
        again, once the Agent has the reply: programs that notify only an absent
        user then declare the end of the work the input started. A quiet
        program's redraw for the lost focus shows nothing new."""
        async with self._lock:
            if not self.focused or self._activity.finished:
                return
            self.focused = False
            if self.focus_reporting:
                self._activity.expect_redraw(self._services.monotonic())
                self.queue_protocol(FOCUS_OUT_REPORT)

    @contextlib.asynccontextmanager
    async def _input_state(self) -> AsyncGenerator[None]:
        """Lock input preparation after earlier physical writes have finished.

        In particular, a cancelled caller can leave its write running. Wait
        without holding the state lock, then check revision/program guards
        against the screen and program that the new input will reach.
        """
        while True:
            await self._lock.acquire()
            writing = self.writer_task
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
            delivery_pending=self._notices.pending(),
            running=running,
        )
        writing = self.queue_protocol(chunk, input_chunk=True)
        self._restart_quiet(restart)
        self._output_event.set()
        self._waits.signal_change()
        if changed:
            self._publish_state()
        return writing

    def queue_protocol(
        self, text: str | Callable[[], str], *, input_chunk: bool = False
    ) -> asyncio.Task[BaseException | None]:
        """Serialize Agent, operator and protocol writes without blocking the reader.

        Session-owned tasks keep a cancelled caller's in-flight thread in
        order. Protocol replies only enqueue: awaiting them in the reader
        would deadlock a program that fills both PTY buffers.
        """
        previous = self.writer_task
        if input_chunk:
            self.writes += 1

        async def write() -> BaseException | None:
            try:
                if previous is not None and not previous.done():
                    await asyncio.wait({previous})
                async with self._lock:
                    if self._activity.finished:
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
                        self.writes -= 1
                        if not self.writes and not self._activity.finished:
                            # Output may have advanced the generation while
                            # the write blocked. Settle the current one only.
                            self._restart_quiet(QuietRestart(self._activity.generation, False))
                        self._output_event.set()
                        self._waits.signal_change()

        task = asyncio.create_task(write(), name=f"terminal:{self.terminal_id}:writer")

        def completed(done: asyncio.Task[BaseException | None]) -> None:
            if self.writer_task is done:
                self.writer_task = None
            _log_task_failure(
                done, f"Terminal input writing failed for terminal={self.terminal_id}"
            )

        task.add_done_callback(completed)
        self.writer_task = task
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
        while not self._activity.finished:
            change = self._waits.change
            now = services.monotonic()
            quiet_since = max(typed_at, self.last_output_at)
            if now - quiet_since >= pause or now >= limit:
                return
            await self._waits.wait_or_sleep(change, min(limit, quiet_since + pause) - now)

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
                self.require_live()
                # Check the screen and program before enqueuing the input.
                if expected_program is not None and not await asyncio.to_thread(
                    self._services.program_probe, self._adapter.pid, expected_program
                ):
                    raise TerminalProgramNotRunningError(
                        f"{expected_program} is not running in this Terminal; "
                        "the input was not written"
                    )
                self._check_revision(expected_screen_revision)
                self._output.revision += 1
                running = await self._before_input()
                writing = self._queue_input(
                    data, running, notify=self._info().attachment is not None
                )
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
        reader = self._reader()
        if reader is not None and not reader.done():
            # Waiting observes completion without propagating cancellation
            # either way: a kill may cancel the reader, not this input/Run.
            await asyncio.wait({reader})
        return TerminalClosedError("Terminal Session is no longer running")

    def require_live(self) -> None:
        if self._activity.finished or self._info().finished_at is not None:
            raise TerminalClosedError("Terminal Session is no longer running")

    def _check_revision(self, expected: int | None) -> None:
        if expected is not None and expected != self._output.revision:
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
            "state": self._activity.state,
            "characters_sent": characters,
            "key": key,
            # Why the key after typed text was not sent: the program had
            # ended, or the PTY refused it while the program runs.
            "key_unsent": key_unsent,
            # Multiline text went to the program as one bracketed paste.
            "pasted": pasted,
            "superseded_attention_revision": superseded,
            "screen_revision": self._output.revision,
            # A wait for the reply ends at the next quiet boundary after this count.
            "quiet_boundaries": self._waits.quiet_boundaries,
        }
