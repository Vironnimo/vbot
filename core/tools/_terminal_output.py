"""Terminal rendering, raw output ordering, observations and log replay."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import re
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

from core.tools.terminal_backend import TerminalAdapter
from core.tools.terminal_emulator import (
    EmulatorUpdate,
)
from core.utils.logging import get_logger
from core.utils.paths import model_path

from ._terminal_log import TerminalLog
from ._terminal_notices import (
    _seen_through,
)
from ._terminal_render_host import TerminalScreen
from ._terminal_state import (
    _CONDITIONS,
    TerminalInfo,
    TerminalObservation,
    TerminalSessionServices,
    _attention_data,
)

_READ_CHUNK_CHARS = 4096
_REPLAY_PIECE_BYTES = 1 << 20
_REPLAY_LOCKED_BYTES = 1 << 16
_REDRAW_RETRY_MAX_SECONDS = 5.0
_FINAL_REDRAW_SECONDS = 1.0
_CLEARS_KEPT = 64
_SURROGATE = re.compile("[\ud800-\udfff]")

_LOGGER = get_logger("tools.terminal_manager")


class TerminalOutput:
    """Own the rendered screen, raw log and recoverable observation history."""

    def __init__(
        self,
        terminal_id: str,
        screen: TerminalScreen,
        log: TerminalLog,
        *,
        lock: asyncio.Lock,
        services: TerminalSessionServices,
        finished: Callable[[], bool],
        background: Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]],
        replaced: Callable[[EmulatorUpdate | None, int | None], Awaitable[None]],
    ) -> None:
        self.terminal_id = terminal_id
        self.screen = screen
        self.log = log
        self._lock = lock
        self._services = services
        self._finished = finished
        self._background = background
        self._replaced = replaced
        self.failure: Exception | None = None
        self.read_failure: Exception | None = None
        self.current = True
        self._first_size = (screen.columns, screen.rows)
        self.resizes: list[tuple[int, int, int]] = []
        self.input_offset: int | None = None
        self._missed_from = 0
        self.redraw_task: asyncio.Task[None] | None = None
        self.observed: TerminalObservation | None = None
        self.seen_line: int | None = None
        self.clear_count = 0
        self._clears: deque[tuple[int, int]] = deque(maxlen=_CLEARS_KEPT)
        self.revision = 0
        self.resize_count = 0

    async def append(
        self, received: str, *, alternate: bool, publish: Callable[[str], None]
    ) -> EmulatorUpdate | None:
        data, text = _output_forms(received)
        offset = self.log.size
        if await self.log.append(data):
            self.log.end_line = await self.output_line(alternate=alternate)
        publish(text)
        return await self.render(text, offset)

    async def _replace_screen(self, replay: _LogReplay) -> None:
        old, self.screen = self.screen, replay.screen
        old.close()
        self.failure = None
        self.current = True
        await self._replaced(replay.update, replay.cleared_line)

    def acknowledge(self, observation: TerminalObservation) -> bool:
        self.saw_lines(observation.seen_line, observation.clears)
        observed = self.observed
        if observed is None or (observation.resize_count, observation.screen_revision) >= (
            observed.resize_count,
            observed.screen_revision,
        ):
            self.observed = observation
            return True
        return False

    async def read(
        self,
        adapter: TerminalAdapter,
        reader_executor: ThreadPoolExecutor,
        consume: Callable[[str], Awaitable[None]],
    ) -> None:
        """Read and render output until it ends or the program has ended.

        An unexpected failure ends only the reading: it becomes the Session's
        failure, and the program keeps running until it ends or is stopped.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    text = await loop.run_in_executor(
                        reader_executor, adapter.read, _READ_CHUNK_CHARS
                    )
                except TimeoutError:
                    if not await asyncio.to_thread(adapter.is_alive):
                        return
                    continue
                if text:
                    async with self._lock:
                        await consume(text)
        except EOFError, OSError:
            # Output ended (POSIX: every holder of the terminal closed it).
            return
        except Exception as error:
            self.read_failure = error
            _LOGGER.error(
                "Terminal output reading failed; the program keeps running until it ends "
                "or is stopped (terminal=%s)",
                self.terminal_id,
                exc_info=True,
            )

    async def snapshot(
        self, info: TerminalInfo, idle_reason: str | None, *, lines: int, start_line: int | None
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
        page = await self.screen.page(start_line=start_line, limit=lines)
        observation = await self.screen.observe()
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
            "log_file": self.log_file(),
            "log_complete": not self.log.failed,
            "log_end_line": self.log.end_line,
            "screen_behind": None if self.failure is None else str(self.failure),
            "seen_line": self.seen_line,
            "idle_reason": idle_reason,
            "observation": TerminalObservation(
                info.screen_revision,
                self.resize_count,
                info.columns,
                info.rows,
                observation.signature,
                _seen_through(observation, alternate=info.alternate_screen),
                self.clear_count,
            ),
        }
        observed = self.observed
        if observed is not None and observed.resize_count < self.resize_count:
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
        data["call_name"] = info.call_name
        data["program"] = info.program
        data["stopped"] = info.stopped
        return data

    def saw_lines(self, line: int, clears: int) -> None:
        """The attached Agent has now seen every history line above *line* of a
        screen shown after *clears* clears.

        A later clear erased that screen from its first line on, so the lines
        drawn there since are new to the Agent (code review R8: a late
        acknowledgement undid a clear). When those clears are no longer
        recorded, what the Agent saw stays as it was.
        """
        if clears < self.clear_count:
            later = [cleared for count, cleared in self._clears if count > clears]
            if len(later) < self.clear_count - clears:
                return
            line = min(line, *later)
        self.seen_line = max(self.seen_line or 0, line)

    def cleared(self, line: int) -> None:
        """A clear erased the screen from history line *line* on: the Agent has
        not seen the lines drawn there next, although they get the same numbers."""
        self.clear_count += 1
        self._clears.append((self.clear_count, line))
        if self.seen_line is not None:
            self.seen_line = min(self.seen_line, line)

    def log_file(self) -> str | None:
        """The output log a result can name; ``_log_failed`` says it ends early."""
        if self.log.path is None:
            return None
        return model_path(self.log.path)

    async def output_line(self, *, alternate: bool) -> int | None:
        """The history line the next output goes to: the cursor's line, also
        while the program hides the cursor; None when the screen cannot tell,
        as while it misses output.

        A full-screen view keeps no history, so the primary screen's lines
        before it are complete.
        """
        if self.failure is not None:
            return None
        try:
            observation = await self.screen.observe()
        except Exception:
            return None
        if alternate:
            return observation.screen_start_line
        return observation.cursor_line

    async def render(self, text: str, offset: int) -> EmulatorUpdate | None:
        """Feed *text*, the output from log offset *offset* on, to the screen;
        None when the screen does not show it.

        Once drawing failed, the screen misses output, and results say so. A
        screen drawn anew from the log replaces it (``_redraw_screen``), which
        replays the log, so the output goes only there meanwhile. Without a
        complete log, the screen goes on with the new output; it lacks the
        output it missed until the program ends.
        """
        if self.failure is not None and self.redrawable():
            self._start_redraw()
            return None
        try:
            update = await self.screen.feed(text)
        except Exception as error:
            self.render_failed(error, offset)
            return None
        self.current = True
        if update.skipped and _CONDITIONS.started((self.terminal_id, "skipped")):
            # A sequence the screen could not apply; it goes on with the rest.
            _LOGGER.warning(
                "Terminal screen skipped %d operations it could not apply (terminal=%s): %s",
                update.skipped,
                self.terminal_id,
                update.skipped_error,
            )
        return update

    def render_failed(self, error: Exception, missed_from: int) -> None:
        """The screen missed the output from log offset *missed_from* on, or
        cannot be used; with a complete log, a screen drawn anew replaces it."""
        self.current = False
        if self.failure is None:
            self.failure = error
            self._missed_from = missed_from
            if _CONDITIONS.started((self.terminal_id, "render")):
                _LOGGER.warning(
                    "Terminal screen rendering failed; the program keeps running and its "
                    "screen falls behind its output (terminal=%s)",
                    self.terminal_id,
                    exc_info=error,
                )
        if self.redrawable():
            self._start_redraw()

    async def mark_input(self) -> None:
        """Mark the screen at the Agent's input; a screen drawn anew replays the mark."""
        if not self.current:
            return
        try:
            await self.screen.mark_input()
        except Exception as error:
            self.render_failed(error, self.log.size)

    def redrawable(self) -> bool:
        """The log holds every byte of output, so a new screen can replay it."""
        return self.log.complete

    def _start_redraw(self) -> None:
        task = self.redraw_task
        if task is None or task.done():
            self.redraw_task = self._background(self._redraw_screen(), "redraw")

    async def _redraw_screen(self) -> None:
        """Replace the screen that missed output with one drawn anew from the log;
        a failed attempt is tried again later while the Session runs."""
        services = self._services
        failures = 0
        while self.failure is not None and self.redrawable() and not self._finished():
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
            delay = services.activity_quiet_seconds * 2 ** min(failures, 10)
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
        path = self.log.path
        if path is None:
            return False
        columns, rows = self._first_size
        screen = await self._services.open_screen(columns, rows)
        replay = _LogReplay(screen, path, missed_from=self._missed_from)
        try:
            while True:
                async with self._lock:
                    if self.failure is None or not self.redrawable() or self._finished():
                        screen.close()
                        return False
                    end = self.log.size
                    resizes = tuple(self.resizes)
                    input_offset = self.input_offset
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

    async def final_redraw(self) -> None:
        """Before the end is recorded, draw the final screen anew from the log."""
        task = self.redraw_task
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
