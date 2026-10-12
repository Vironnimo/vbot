"""A command's output on the Event Loop: its transcript, newest lines and log file.

Cleaning the output costs CPU time in proportion to what the command prints
(``_command_cleaning``). Each command's ``OutputCleaner`` therefore runs on a
worker of the manager's output host, a subinterpreter with its own GIL
(``_interpreter_host``), which also writes the log file. ``CommandOutput``
hands the cleaner one chunk at a time and keeps what each call returns: the
first lines, the newest ones, the line count and the line still being written.
The transcript and the newest lines are read from that state, without waiting
for the worker.

Commands share their host's workers; the reader of a command reads the next
chunk only once the previous one is cleaned, so a command that prints heavily
has one chunk at a time in its worker's queue and delays another command by at
most that chunk. Terminal screens render on their own host's workers.
"""

from __future__ import annotations

import contextlib
from collections import deque

from . import _command_cleaning
from ._command_cleaning import OutputCleaner, OutputUpdate
from ._command_types import (
    COMMAND_HEAD_LINES,
    COMMAND_MEMORY_LINE_MAX_CHARS,
    COMMAND_RECENT_LINES,
    COMMAND_TAIL_LINES,
    COMMAND_TAIL_SPARE_LINES,
    CommandTranscript,
)
from ._interpreter_host import DEFAULT_WORKERS, HostedObject, InterpreterHost

# Lines after the head; enough for the transcript tail and the newest lines.
_KEPT_LINES = max(COMMAND_RECENT_LINES, COMMAND_TAIL_LINES + COMMAND_TAIL_SPARE_LINES)


def command_output_host(*, workers: int = DEFAULT_WORKERS) -> InterpreterHost:
    """A host that cleans commands' output on subinterpreter workers."""
    return InterpreterHost((_command_cleaning,), name="command output", workers=workers)


class CommandOutput:
    """The clean output of one command: transcript, newest lines and log file.

    Used from the Event Loop. ``feed``, ``end`` and ``flush_log`` apply in the
    order they are called. Once cleaning fails, later output is discarded and
    the transcript says so.
    """

    def __init__(self, cleaner: HostedObject) -> None:
        self._cleaner = cleaner
        self._head: list[str] = []
        self._recent: deque[str] = deque(maxlen=_KEPT_LINES)
        self._total_lines = 0
        self._current: str | None = None
        self._log_pending = False
        self._ended = False
        self._failed = False
        self._closed = False

    @classmethod
    def open(cls, host: InterpreterHost, log_fd: int | None) -> CommandOutput:
        """Clean a command's output on *host*, writing every line to *log_fd*.

        The output owns the file descriptor from now on. Raises RuntimeError
        when the host is closed; the descriptor stays the caller's then.
        """
        return cls(
            host.open(
                OutputCleaner,
                log_fd,
                head_lines=COMMAND_HEAD_LINES,
                kept_lines=_KEPT_LINES,
                line_max_chars=COMMAND_MEMORY_LINE_MAX_CHARS,
            )
        )

    # Feeding

    async def feed(self, data: bytes) -> None:
        """Clean output bytes; returns once their lines are in the transcript.

        Raises when cleaning fails; from then on the output is ``failed``.
        """
        if not data or self._ended or self._failed:
            return
        try:
            update = await self._cleaner.call("feed", data)
        except Exception:
            self._failed = True
            raise
        self._apply(update)

    async def end(self) -> None:
        """The output ended: complete the line still being written."""
        if self._ended:
            return
        self._ended = True
        if not self._failed:
            self._apply(await self._cleaner.call("end"))

    @property
    def failed(self) -> bool:
        """Cleaning failed: later output is discarded, and the transcript and
        the log file end where cleaning stopped."""
        return self._failed

    def _apply(self, update: OutputUpdate) -> None:
        self._head.extend(update.head_added)
        self._recent.extend(update.recent_added)
        self._total_lines = update.total_lines
        self._current = update.current_line
        self._log_pending = update.log_pending

    # Reading

    def transcript(self) -> CommandTranscript:
        """The head and tail of the output; the line still being written is the last."""
        after = list(self._recent)
        total = self._total_lines
        if self._current is not None:
            after.append(self._current)
            total += 1
        after_head = total - len(self._head)
        # The tail keeps its spare lines only while no line is left out.
        if after_head <= COMMAND_TAIL_LINES + COMMAND_TAIL_SPARE_LINES:
            tail = after[len(after) - after_head :] if after_head else []
        else:
            tail = after[-COMMAND_TAIL_LINES:]
        return CommandTranscript(
            head=tuple(self._head),
            tail=tuple(tail),
            omitted_lines=total - len(self._head) - len(tail),
            total_lines=total,
            processing_failed=self._failed,
        )

    def latest(self, lines: int) -> str:
        """The newest *lines* lines (at most ``COMMAND_RECENT_LINES``) as text."""
        count = max(0, min(lines, COMMAND_RECENT_LINES))
        if not count:
            return ""
        newest = list(self._recent)
        if self._total_lines - len(self._head) <= len(newest):
            newest = [*self._head, *newest]
        if self._current is not None:
            newest.append(self._current)
        return "\n".join(newest[-count:])

    # Log file

    @property
    def log_pending(self) -> bool:
        """Output reached the log file's buffer or changed its current line since
        the last flush."""
        return self._log_pending and not self._closed and not self._failed

    async def flush_log(self) -> None:
        """Flush the log file and show the current line at its end."""
        if self._closed or self._failed:
            return
        self._log_pending = False
        await self._cleaner.call("flush_log")

    def close(self) -> None:
        """Flush and close the log file and release the cleaner, after the calls
        made before; does not wait."""
        if self._closed:
            return
        self._closed = True
        # A stopped host closed the file with its interpreter.
        with contextlib.suppress(RuntimeError):
            self._cleaner.call_nowait("close_log")
        self._cleaner.close()


__all__ = ["CommandOutput", "command_output_host"]
