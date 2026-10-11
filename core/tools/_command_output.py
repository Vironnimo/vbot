"""A command's output as clean lines: the bounded transcript and the live log file.

The command writes bytes to one pipe (stdout and stderr together). They are
decoded as UTF-8 incrementally (invalid bytes become U+FFFD) and cleaned the way
a terminal would show their final state, without any terminal behind them:

- Escape sequences are removed: CSI, two-character sequences, and the control
  strings OSC, DCS, SOS, PM and APC (titles, hyperlinks), also in their C1
  forms. Parsing is bounded, so an unterminated sequence cannot hide output: a
  line break or a character that cannot continue a sequence ends it. An ESC
  that cannot start a control, before a lowercase letter, a control character
  or non-ASCII text, is data and shows as the text ``\\x1b``: PowerShell writes
  one for ```e`` in a double-quoted string, as in a Markdown span ```expected```.
- ``\\r\\n`` ends a line like ``\\n``. A lone ``\\r`` restarts the current line: the
  next text replaces it, so a redrawn progress line keeps its final state, and a
  line that ends with ``\\r`` keeps its text.
- A backspace erases the character before it on the current line.
- Other C0 controls and DEL are removed; tabs stay.

Completed lines enter the transcript, which keeps the first lines and the
newest ones in memory, and the log file, which keeps every line whole. The line
still being written shows at the end of the log file at each flush and is
replaced as it changes; a very long line is written to the file in parts while
only its beginning stays in memory.
"""

from __future__ import annotations

import codecs
import contextlib
import re
from collections import deque
from typing import BinaryIO

from ._command_types import (
    COMMAND_HEAD_LINES,
    COMMAND_MEMORY_LINE_MAX_CHARS,
    COMMAND_RECENT_LINES,
    COMMAND_TAIL_LINES,
    COMMAND_TAIL_SPARE_LINES,
    CommandTranscript,
)

# Characters that may start or be part of a terminal control in decoded text.
_TEXT_CONTROL = re.compile(r"[\x1b\x80-\x9f]")
# Characters that act on the current line, or are dropped: C0 except tab, and DEL.
_LINE_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# Legitimate control strings (titles, hyperlinks) and CSI parameters are short.
# Longer unterminated sequences are treated as garbage so they cannot hide output.
_MAX_STRING_CHARACTERS = 4096
_MAX_SEQUENCE_CHARACTERS = 64
_VISIBLE_ESCAPE = "\\x1b"


class _EscapeFilter:
    """Incremental UTF-8 decoding with terminal controls removed (bounded state)."""

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "text"
        self._osc = False
        self._length = 0

    def decode(self, data: bytes, *, final: bool = False) -> str:
        text = self._utf8.decode(data, final=final)
        output: list[str] = []
        index = 0
        while index < len(text):
            if self._state == "text":
                match = _TEXT_CONTROL.search(text, index)
                stop = len(text) if match is None else match.start()
                output.append(text[index:stop])
                index = stop
                if match is None:
                    break
            if self._consume(text[index], output):
                index += 1
        return "".join(output)

    def _consume(self, char: str, output: list[str]) -> bool:
        """Advance the parser; ``False`` means the character is processed again."""
        if self._state == "string":
            if char == "\x9c" or (self._osc and char == "\x07"):
                self._state = "text"
            elif char == "\x1b":
                self._state = "string_escape"
            elif char in "\r\n" or self._length >= _MAX_STRING_CHARACTERS:
                self._state = "text"
                return False
            else:
                self._length += 1
            return True
        if self._state == "string_escape":
            if char == "\\":
                self._state = "text"
                return True
            # Any other escape ends the string and begins a new sequence.
            self._start("escape")
            return False
        if self._state == "csi":
            if "\x40" <= char <= "\x7e":
                self._state = "text"
            elif "\x20" <= char <= "\x3f" and self._length < _MAX_SEQUENCE_CHARACTERS:
                self._length += 1
            else:
                self._state = "text"
                return False
            return True
        if self._state == "escape":
            if char == "[":
                self._start("csi")
            elif char in "]PX^_":
                self._start("string", osc=char == "]")
            elif "\x20" <= char <= "\x2f" and self._length < _MAX_SEQUENCE_CHARACTERS:
                self._length += 1
            elif "\x30" <= char <= "\x5f" or (self._length and "\x60" <= char <= "\x7e"):
                self._state = "text"
            elif not self._length:
                # Terminals practically never send ESC before a lowercase letter,
                # a control character or non-ASCII text, so this ESC is data.
                self._state = "text"
                output.append(_VISIBLE_ESCAPE)
                return False
            else:
                self._state = "text"
                return False
            return True
        if char == "\x1b":
            self._start("escape")
        elif char == "\x9b":
            self._start("csi")
        elif char in "\x90\x98\x9d\x9e\x9f":
            self._start("string", osc=char == "\x9d")
        # Other C1 controls are dropped.
        return True

    def _start(self, state: str, *, osc: bool = False) -> None:
        self._state = state
        self._osc = osc
        self._length = 0


class CommandOutput:
    """The clean output of one command: transcript, newest lines and log file.

    Not thread-safe; the command's reader feeds it on the Event Loop. Writes
    to the log file are buffered and reach the file at ``flush_log``; a failing
    write closes the file, and the transcript goes on without it.
    """

    def __init__(self, log_file: BinaryIO | None = None) -> None:
        self._filter = _EscapeFilter()
        self._head: list[str] = []
        # Lines after the head; enough for the transcript tail and the newest lines.
        self._recent: deque[str] = deque(
            maxlen=max(COMMAND_RECENT_LINES, COMMAND_TAIL_LINES + COMMAND_TAIL_SPARE_LINES)
        )
        self._total_lines = 0
        # The current line: text not yet in the file, and, once part of a long
        # line went to the file, that part's length and the line's beginning.
        self._partial: list[str] = []
        self._partial_chars = 0
        self._spilled_chars = 0
        self._spilled_prefix = ""
        # A lone CR was seen: the next text replaces the current line.
        self._restart = False
        self._file = log_file
        # Bytes of final content: completed lines and the spilled part of the
        # current line. The current line begins at ``_line_start``; ``_shown``
        # bytes after the final content show its unwritten rest.
        self._final_bytes = 0
        self._line_start = 0
        self._shown = 0
        self._log_dirty = False
        self._ended = False

    # Feeding

    def feed(self, data: bytes) -> None:
        """Add output bytes; complete lines enter the transcript and the log file."""
        if data:
            self._apply(self._filter.decode(data))

    def end(self) -> None:
        """The output ended: decode what is left and complete the current line."""
        if self._ended:
            return
        self._ended = True
        self._apply(self._filter.decode(b"", final=True))
        if self._partial_chars or self._spilled_chars:
            self._complete_line()

    def _apply(self, text: str) -> None:
        position = 0
        for match in _LINE_CONTROL.finditer(text):
            if match.start() > position:
                self._append(text[position : match.start()])
            control = match.group()
            if control == "\n":
                self._complete_line()
            elif control == "\r":
                self._restart = True
            elif control == "\b":
                self._erase()
            position = match.end()
        if position < len(text):
            self._append(text[position:])

    def _append(self, text: str) -> None:
        if self._restart:
            self._restart = False
            self._clear_line()
        self._partial.append(text)
        self._partial_chars += len(text)
        self._log_dirty = True
        if self._partial_chars > COMMAND_MEMORY_LINE_MAX_CHARS:
            self._spill()

    def _erase(self) -> None:
        # After a lone CR the cursor is at the line's start: nothing to erase.
        # Text already written to the file stays.
        if self._restart or not self._partial_chars:
            return
        while self._partial and not self._partial[-1]:
            self._partial.pop()
        self._partial[-1] = self._partial[-1][:-1]
        self._partial_chars -= 1
        self._log_dirty = True

    def _clear_line(self) -> None:
        self._partial.clear()
        self._partial_chars = 0
        if self._spilled_chars:
            self._spilled_chars = 0
            self._spilled_prefix = ""
            self._truncate_log(self._line_start)
        self._log_dirty = True

    def _spill(self) -> None:
        """Write the long current line's text so far to the file; keep its beginning."""
        text = "".join(self._partial)
        if not self._spilled_chars:
            self._spilled_prefix = text[:COMMAND_MEMORY_LINE_MAX_CHARS]
        self._spilled_chars += len(text)
        self._partial.clear()
        self._partial_chars = 0
        self._write_final(text)

    def _complete_line(self) -> None:
        text = "".join(self._partial)
        line = self._spilled_prefix if self._spilled_chars else text
        self._write_final(text + "\n")
        self._partial.clear()
        self._partial_chars = 0
        self._spilled_chars = 0
        self._spilled_prefix = ""
        self._restart = False
        self._line_start = self._final_bytes
        self._remember(line[:COMMAND_MEMORY_LINE_MAX_CHARS])

    def _remember(self, line: str) -> None:
        if len(self._head) < COMMAND_HEAD_LINES:
            self._head.append(line)
        else:
            self._recent.append(line)
        self._total_lines += 1

    # Reading

    def transcript(self) -> CommandTranscript:
        """The head and tail of the output; the line still being written is the last."""
        current = self._current_line()
        after = list(self._recent)
        total = self._total_lines
        if current is not None:
            after.append(current)
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
        )

    def latest(self, lines: int) -> str:
        """The newest *lines* lines (at most ``COMMAND_RECENT_LINES``) as text."""
        count = max(0, min(lines, COMMAND_RECENT_LINES))
        if not count:
            return ""
        newest = list(self._recent)
        if self._total_lines - len(self._head) <= len(newest):
            newest = [*self._head, *newest]
        current = self._current_line()
        if current is not None:
            newest.append(current)
        return "\n".join(newest[-count:])

    def _current_line(self) -> str | None:
        if self._spilled_chars:
            return self._spilled_prefix
        if not self._partial_chars:
            return None
        return "".join(self._partial)[:COMMAND_MEMORY_LINE_MAX_CHARS]

    # Log file

    @property
    def log_pending(self) -> bool:
        """Output reached the log file's buffer or changed its current line since
        the last flush."""
        return self._log_dirty and self._file is not None

    def flush_log(self) -> None:
        """Flush the log file and show the current line at its end."""
        if self._file is None or not self._log_dirty:
            return
        self._log_dirty = False
        data = "".join(self._partial).encode("utf-8", "replace")
        try:
            if data or self._shown:
                self._file.seek(self._final_bytes)
                self._file.write(data)
                self._file.truncate()
                self._shown = len(data)
            self._file.flush()
        except OSError:
            self._close_file()

    def close_log(self) -> None:
        """Flush and close the log file; the output must have ended."""
        self.flush_log()
        self._close_file()

    def _write_final(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        self._log_dirty = True
        if self._file is not None:
            try:
                if self._shown:
                    self._file.seek(self._final_bytes)
                    self._file.truncate()
                    self._shown = 0
                self._file.write(data)
            except OSError:
                self._close_file()
        self._final_bytes += len(data)

    def _truncate_log(self, offset: int) -> None:
        self._final_bytes = offset
        if self._file is not None:
            try:
                self._file.seek(offset)
                self._file.truncate()
                self._shown = 0
            except OSError:
                self._close_file()

    def _close_file(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


__all__ = ["CommandOutput"]
