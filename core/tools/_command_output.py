"""A command's output on the Event Loop: its log file, transcript and newest lines.

Every byte the command writes is appended to its log file unchanged, as it
arrives (``CommandLog``). Writes run through the blocking runner, off the Event
Loop, one at a time in the order of the output.

Cleaning the output costs CPU time in proportion to what the command prints
(``_command_cleaning``). Each command's ``OutputCleaner`` therefore runs on a
worker of the manager's output host, a subinterpreter with its own GIL
(``_interpreter_host``). ``CommandOutput`` hands the cleaner one chunk at a
time and follows its bounded lines (``OutputLines``): the first lines, the
newest ones, the line count and the line still being written. The transcript
and the newest lines are read from them, without waiting for the worker. When
cleaning fails, the lines go on with the output decoded but not cleaned.

Commands share their host's workers; the reader of a command reads the next
chunk only once the previous one is written and cleaned, so a command that
prints heavily has one chunk at a time in its worker's queue and delays another
command by at most that chunk. Terminal screens render on their own host's
workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

from . import _command_cleaning, _command_decoding
from ._command_cleaning import OutputCleaner, OutputLines
from ._command_decoding import CommandOutputDecoder, DecoderState
from ._command_types import (
    COMMAND_HEAD_CHARS,
    COMMAND_RECENT_LINES,
    COMMAND_TAIL_CHARS,
    CommandTranscript,
    LogLoss,
)
from ._interpreter_host import DEFAULT_WORKERS, HostedObject, InterpreterHost


def command_output_host(*, workers: int = DEFAULT_WORKERS) -> InterpreterHost:
    """A host that cleans commands' output on subinterpreter workers."""
    return InterpreterHost(
        (_command_decoding, _command_cleaning), name="command output", workers=workers
    )


class CommandLog:
    """A command's log file: every output byte, appended unchanged as it arrives.

    Owns the file descriptor *fd*; writes run through *run_blocking*, off the
    Event Loop. A failing write closes the file (``error``), and the output
    goes on without it; the file keeps what was written before.
    """

    def __init__(self, fd: int, run_blocking: Callable[..., Awaitable[Any]]) -> None:
        self._fd: int | None = fd
        self._run_blocking = run_blocking
        self._writing: asyncio.Task[None] | None = None
        self._closing = False
        self._kept_bytes = 0
        self._kept_lines = 0
        self.error: BaseException | None = None

    @property
    def loss(self) -> LogLoss | None:
        """What the file misses once writing it failed; None while it holds every byte."""
        if self.error is None:
            return None
        return LogLoss(self.error, kept_bytes=self._kept_bytes, kept_lines=self._kept_lines)

    async def write(self, data: bytes) -> None:
        """Append *data*; returns once it is written or writing failed."""
        fd = self._fd
        if fd is None or self._closing:
            return
        writing = asyncio.create_task(self._write(fd, data))
        self._writing = writing
        writing.add_done_callback(self._written)
        # A cancelled caller leaves the write running; the file closes after it.
        with contextlib.suppress(Exception):
            await asyncio.shield(writing)

    def pending(self) -> list[asyncio.Task[None]]:
        """The write in progress, if any."""
        writing = self._writing
        return [writing] if writing is not None and not writing.done() else []

    def close(self) -> None:
        """Close the file once the write in progress, if any, ended; does not wait."""
        self._closing = True
        if self._writing is None or self._writing.done():
            self._close_file()

    async def _write(self, fd: int, data: bytes) -> None:
        written, error = await self._run_blocking(_write_all, fd, data)
        self._kept_bytes += written
        self._kept_lines += data.count(b"\n", 0, written)
        if error is not None:
            raise error

    def _written(self, writing: asyncio.Task[None]) -> None:
        error = None if writing.cancelled() else writing.exception()
        if error is not None and self.error is None:
            self.error = error
            self._close_file()
        elif self._closing:
            self._close_file()

    def _close_file(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _write_all(fd: int, data: bytes) -> tuple[int, Exception | None]:
    """Write *data*: the bytes written, and the error that stopped the write, if any."""
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            written += os.write(fd, view[written:])
        except Exception as error:
            return written, error
    return written, None


class CommandOutput:
    """One command's output: its log file, transcript and newest lines.

    Used from the Event Loop. ``feed`` and ``end`` apply in the order they are
    called. Once cleaning fails, the transcript goes on with the output
    decoded but not cleaned.
    """

    def __init__(self, cleaner: HostedObject, log: CommandLog | None) -> None:
        self._cleaner = cleaner
        self._log = log
        self._lines = OutputLines(head_chars=COMMAND_HEAD_CHARS, tail_chars=COMMAND_TAIL_CHARS)
        self._uncleaned: _Uncleaned | None = None
        # The interpretation and pending bytes after the last acknowledged update.
        self._decoder_state: DecoderState | None = None
        self._ended = False
        self._closed = False

    @classmethod
    def open(
        cls,
        host: InterpreterHost,
        log_fd: int | None,
        run_blocking: Callable[..., Awaitable[Any]],
    ) -> CommandOutput:
        """Clean a command's output on *host*; append its bytes to *log_fd*.

        The output owns the file descriptor from now on and writes it through
        *run_blocking*. Raises RuntimeError when the host is closed; the
        descriptor stays the caller's then.
        """
        cleaner = host.open(
            OutputCleaner, head_chars=COMMAND_HEAD_CHARS, tail_chars=COMMAND_TAIL_CHARS
        )
        return cls(cleaner, None if log_fd is None else CommandLog(log_fd, run_blocking))

    # Feeding

    async def feed(self, data: bytes) -> None:
        """Append output bytes to the log file, then clean them into the transcript.

        Raises when cleaning fails; these bytes and all later ones enter the
        transcript uncleaned.
        """
        if not data or self._ended:
            return
        if self._log is not None:
            await self._log.write(data)
        if self._uncleaned is not None:
            self._uncleaned.feed(data)
            return
        try:
            update = await self._cleaner.call("feed", data)
        except Exception:
            self._uncleaned = _Uncleaned(self._lines, self._decoder_state)
            self._uncleaned.feed(data)
            raise
        self._lines.apply(update)
        self._decoder_state = update.decoder_state

    async def end(self) -> None:
        """The output ended: complete the line still being written.

        Raises when cleaning fails; the line is completed as it was.
        """
        if self._ended:
            return
        self._ended = True
        if self._uncleaned is None:
            try:
                update = await self._cleaner.call("end")
            except Exception:
                self._uncleaned = _Uncleaned(self._lines, self._decoder_state)
                self._uncleaned.end()
                raise
            self._lines.apply(update)
            return
        self._uncleaned.end()

    @property
    def log_loss(self) -> LogLoss | None:
        """What the log file misses once writing it failed; None while it holds
        every byte, and without one."""
        return None if self._log is None else self._log.loss

    # Reading

    def transcript(self) -> CommandTranscript:
        """The first and the last lines; the line still being written is the last."""
        head, head_cut, tail, tail_cut, total = self._lines.snapshot()
        return CommandTranscript(
            head=head, tail=tail, total_lines=total, head_cut=head_cut, tail_cut=tail_cut
        )

    def latest(self, lines: int) -> str:
        """The newest *lines* lines (at most ``COMMAND_RECENT_LINES``) as text."""
        return self._lines.latest(min(lines, COMMAND_RECENT_LINES))

    # Closing

    def pending_tasks(self) -> list[asyncio.Task[None]]:
        """The log write in progress, if any."""
        return [] if self._log is None else self._log.pending()

    def close(self) -> None:
        """Release the cleaner, and close the log file once its write in
        progress ended; does not wait."""
        if self._closed:
            return
        self._closed = True
        self._cleaner.close()
        if self._log is not None:
            self._log.close()


class _Uncleaned:
    """Output after cleaning failed: decoded and split into lines, not cleaned.

    Decoding goes on from the cleaner's last acknowledged state, including its
    encoding and undecided bytes, so a character split across the failure stays whole.
    """

    def __init__(self, lines: OutputLines, decoder_state: DecoderState | None) -> None:
        self._decoder = CommandOutputDecoder(track_lines=False)
        if decoder_state is not None:
            self._decoder.setstate(decoder_state)
        self._lines = lines

    def feed(self, data: bytes) -> None:
        self._add(self._decoder.decode(data).decode("utf-8"))

    def end(self) -> None:
        self._add(self._decoder.decode(b"", final=True).decode("utf-8"))
        self._lines.end()

    def _add(self, text: str) -> None:
        if text:
            self._lines.add_text(text.replace("\r\n", "\n"))


__all__ = ["CommandLog", "CommandOutput", "command_output_host"]
