"""A command's output cleaned into lines, kept bounded in memory.

Standard library only: ``CommandOutput`` runs an ``OutputCleaner`` per command
on a subinterpreter worker (``_interpreter_host``), off the Event Loop, and
follows its lines on the Event Loop (``OutputLines``).

The command writes bytes to one pipe (stdout and stderr together). They are
decoded as UTF-8 incrementally (invalid bytes become U+FFFD) and cleaned the way
a terminal would show their final state, without any terminal behind them:

- Escape sequences are removed: CSI, two-character sequences, and the control
  strings OSC, DCS, SOS, PM and APC (titles, hyperlinks), each started by ESC.
  Parsing is bounded, so an unterminated sequence cannot hide output: a line
  break or a character that cannot continue a sequence ends it. An ESC that
  cannot start a control, before a lowercase letter, a control character or
  non-ASCII text, is data and shows as the text ``\\x1b``: PowerShell writes one
  for ```e`` in a double-quoted string, as in a Markdown span ```expected```.
- The C1 code points U+0080-U+009F are removed one by one and never start a
  sequence: a UTF-8 terminal does not interpret them, and they occur as text,
  such as UTF-8 decoded as Latin-1 (``Ã\\x9f`` for ``ß``), whose rest of the line
  a C1 sequence start would swallow.
- ``\\r\\n`` ends a line like ``\\n``. A lone ``\\r`` restarts the current line: the
  next text replaces it, so a redrawn progress line keeps its final state, and a
  line that ends with ``\\r`` keeps its text.
- A backspace erases the character before it on the current line.
- Other C0 controls and DEL are removed; tabs stay.

Only the transcript is cleaned: the log file has the bytes as the command wrote
them (``_command_output``). The lines are bounded (``OutputLines``): the first
ones within one character budget, the newest ones within another, and the start
and the end of the line still being written, so a command that prints gigabytes,
or one endless line, holds a fixed amount of memory.
"""

from __future__ import annotations

import codecs
import itertools
import re
from collections import deque
from collections.abc import Sequence
from typing import NamedTuple

# Characters that start a terminal control, or are dropped (C1), in decoded text.
_TEXT_CONTROL = re.compile(r"[\x1b\x80-\x9f]")
# Characters that act on the current line, or are dropped: C0 except tab, and DEL.
_LINE_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# The same without the line feed: text without them is plain lines.
_LINE_CONTROL_BUT_LF = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Legitimate control strings (titles, hyperlinks) and CSI parameters are short.
# Longer unterminated sequences are treated as garbage so they cannot hide output.
_MAX_STRING_CHARACTERS = 4096
_MAX_SEQUENCE_CHARACTERS = 64
_VISIBLE_ESCAPE = "\\x1b"


class OutputUpdate(NamedTuple):
    """How a call changed the cleaner's ``OutputLines``, for ``OutputLines.apply``."""

    # Lines that entered the head, and whether the head takes no more.
    head_added: tuple[str, ...]
    head_closed: bool
    # The start and the length of a first line too long for the head, once known.
    first_line: tuple[str, int] | None
    # What the tail added (``TailLines.take_added``).
    tail_added: tuple[str, ...]
    tail_cut: int
    tail_restart: bool
    # Completed lines in all.
    total_lines: int
    # The line still being written, if any: its end, its start unless the end
    # holds all of it (then empty), and its length.
    current_line: tuple[str, str, int] | None


class TailLines:
    """The newest whole lines within *limit* characters, lines joined by line breaks.

    A line longer than *limit* replaces them all with its last *limit*
    characters; ``cut`` counts the characters before them. What was added
    since the last ``take_added`` lets another ``TailLines`` follow this one.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lines: deque[str] = deque()
        # The lines' characters plus one line break each.
        self._used = 0
        self.cut = 0
        # The newest this many lines were added since ``take_added``; a line
        # added since then was dropped again, or lines were left out (restart).
        self._added = 0
        self._restart = False

    def __len__(self) -> int:
        return len(self._lines)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self._lines)

    def newest(self, count: int) -> list[str]:
        """The newest *count* lines, oldest first."""
        newest = list(itertools.islice(reversed(self._lines), max(0, count)))
        newest.reverse()
        return newest

    def copy(self) -> TailLines:
        copy = TailLines(self._limit)
        copy._lines = self._lines.copy()
        copy._used = self._used
        copy.cut = self.cut
        return copy

    def add(self, line: str, chars: int | None = None) -> None:
        """Add a line; *chars* is its length when *line* holds only its end."""
        length = len(line) if chars is None else chars
        if length > self._limit:
            end = line[-self._limit :]
            self._lines.clear()
            self._lines.append(end)
            self._used = len(end) + 1
            self.cut = length - len(end)
            self._added = 1
            return
        self._drop_cut()
        self._lines.append(line)
        self._used += len(line) + 1
        self._added += 1
        self._trim()

    def extend(self, lines: Sequence[str]) -> None:
        """Add whole *lines*, keeping only the newest of them that can stay."""
        start = len(lines)
        used = 0
        while start:
            size = len(lines[start - 1]) + 1
            if used + size - 1 > self._limit:
                break
            used += size
            start -= 1
        if start == len(lines):
            if lines:
                self.add(lines[-1])
            return
        if start:
            # Lines between the older ones and these were left out.
            self._lines.clear()
            self._used = 0
            self.cut = 0
            self._added = 0
            self._restart = True
        self._drop_cut()
        self._lines.extend(itertools.islice(lines, start, None))
        self._used += used
        self._added += len(lines) - start
        self._trim()

    def take_added(self) -> tuple[tuple[str, ...], int, bool]:
        """What changed since the last call, for ``apply``: the lines added,
        the characters cut before the first of them, and whether the lines
        before them are gone."""
        count = min(self._added, len(self._lines))
        added = tuple(self.newest(count))
        cut = self.cut if count and count == len(self._lines) else 0
        restart = self._restart
        self._added = 0
        self._restart = False
        return added, cut, restart

    def apply(self, added: Sequence[str], cut: int, restart: bool) -> None:
        """Follow another ``TailLines`` of the same limit by its ``take_added``."""
        if restart or cut:
            self._lines.clear()
            self._used = 0
            self.cut = 0
        if cut:
            self._lines.append(added[0])
            self._used = len(added[0]) + 1
            self.cut = cut
        elif added:
            self._drop_cut()
            self._lines.extend(added)
            self._used += sum(len(line) + 1 for line in added)
            self._trim()
        self._added = 0
        self._restart = False

    def _drop_cut(self) -> None:
        """Drop a cut line before a later line is added.

        A cut line is the end of a line longer than the limit, so it never stays
        beside another line. Its kept end can be shorter than the limit (a
        backspace erased from it), so the trim alone would keep it.
        """
        if not self.cut:
            return
        # The cut line is the only line.
        if len(self._lines) == self._added:
            # A line added since take_added goes: a follower cannot drop it.
            self._added -= 1
            self._restart = True
        self._lines.clear()
        self._used = 0
        self.cut = 0

    def _trim(self) -> None:
        while self._used - 1 > self._limit:
            if len(self._lines) == self._added:
                # A line added since take_added goes: a follower cannot drop it.
                self._added -= 1
                self._restart = True
            self._used -= len(self._lines.popleft()) + 1
            self.cut = 0


class OutputLines:
    """A command's output lines, bounded: the first ones, the newest ones, the count.

    The head keeps the first whole lines while they fit in *head_chars*
    characters, lines joined by line breaks; a first line longer than that
    keeps its start instead. The tail keeps the newest whole lines after the
    head within *tail_chars* (``TailLines``). The line still being written
    keeps its start and its end.

    The cleaner's lines report each change (``take_update``); the lines on the
    Event Loop follow them (``apply``), and go on by themselves when cleaning
    fails.
    """

    def __init__(self, *, head_chars: int, tail_chars: int) -> None:
        self._head_chars = head_chars
        self._tail_chars = tail_chars
        self._head: list[str] = []
        # The head's characters plus one line break each.
        self._head_used = 0
        self._head_closed = False
        self._first: tuple[str, int] | None = None
        self._tail = TailLines(tail_chars)
        self._total = 0
        # The line still being written: its text in _parts, or for a long line
        # its end there and its start in _start.
        self._parts: list[str] = []
        self._parts_chars = 0
        self._chars = 0
        self._start: str | None = None
        # Changes since the last ``take_update``.
        self._head_added: list[str] = []
        self._first_added = False

    # The line still being written

    def append(self, text: str) -> None:
        """Add *text* to the current line."""
        self._parts.append(text)
        self._parts_chars += len(text)
        self._chars += len(text)
        if self._parts_chars > 2 * self._tail_chars:
            self._shorten()

    def erase(self) -> None:
        """Erase the last character of the current line."""
        if not self._chars:
            return
        while self._parts and not self._parts[-1]:
            self._parts.pop()
        if self._parts:
            self._parts[-1] = self._parts[-1][:-1]
            self._parts_chars -= 1
        self._chars -= 1

    def clear_line(self) -> None:
        """Empty the current line."""
        self._parts.clear()
        self._parts_chars = 0
        self._chars = 0
        self._start = None

    def complete(self) -> None:
        """Complete the current line, also an empty one."""
        text = "".join(self._parts)
        chars = self._chars
        start = self._start
        self.clear_line()
        self._add(text, chars, start)

    def end(self) -> None:
        """The output ended: complete the current line unless it is empty."""
        if self._chars:
            self.complete()

    def add_text(self, text: str) -> None:
        """Add text whose only control is the line feed."""
        lines = text.split("\n")
        if lines[0]:
            self.append(lines[0])
        if len(lines) == 1:
            return
        self.complete()
        if len(lines) > 2:
            self._add_lines(lines[1:-1])
        if lines[-1]:
            self.append(lines[-1])

    def _shorten(self) -> None:
        """Keep only the start and the end of a long current line."""
        text = "".join(self._parts)
        if self._start is None:
            self._start = text[: self._head_chars]
        end = text[-self._tail_chars :]
        self._parts = [end]
        self._parts_chars = len(end)

    def _current(self) -> tuple[str, str, int] | None:
        """The current line as ``OutputUpdate.current_line``."""
        if not self._chars:
            return None
        if len(self._parts) > 1:
            self._parts[:] = ["".join(self._parts)]
        text = self._parts[0] if self._parts else ""
        end = text[-self._tail_chars :]
        if self._chars == len(end):
            return end, "", self._chars
        start = self._start if self._start is not None else text[: self._head_chars]
        return end, start, self._chars

    # Completed lines

    def _add(self, text: str, chars: int, start: str | None) -> None:
        """Add a completed line: its text, or for a long line its end and start."""
        self._total += 1
        if not self._head_closed:
            if self._head_used + chars <= self._head_chars:
                self._head.append(text)
                self._head_used += chars + 1
                self._head_added.append(text)
                return
            self._head_closed = True
            if not self._head:
                self._first = (text[: self._head_chars] if start is None else start, chars)
                self._first_added = True
        self._tail.add(text, chars)

    def _add_lines(self, lines: list[str]) -> None:
        """Add whole completed lines at once."""
        index = 0
        while index < len(lines) and not self._head_closed:
            line = lines[index]
            self._add(line, len(line), None)
            index += 1
        if index < len(lines):
            self._total += len(lines) - index
            self._tail.extend(lines[index:] if index else lines)

    # Following

    def take_update(self) -> OutputUpdate:
        """The changes since the last call."""
        tail_added, tail_cut, tail_restart = self._tail.take_added()
        update = OutputUpdate(
            tuple(self._head_added),
            self._head_closed,
            self._first if self._first_added else None,
            tail_added,
            tail_cut,
            tail_restart,
            self._total,
            self._current(),
        )
        self._head_added.clear()
        self._first_added = False
        return update

    def apply(self, update: OutputUpdate) -> None:
        """Follow the lines that returned *update*."""
        for line in update.head_added:
            self._head.append(line)
            self._head_used += len(line) + 1
        self._head_closed = update.head_closed
        if update.first_line is not None:
            self._first = update.first_line
        self._tail.apply(update.tail_added, update.tail_cut, update.tail_restart)
        self._total = update.total_lines
        self.clear_line()
        if update.current_line is not None:
            end, start, chars = update.current_line
            self._parts = [end]
            self._parts_chars = len(end)
            self._chars = chars
            self._start = start if chars > len(end) else None

    # Reading

    def snapshot(self) -> tuple[tuple[str, ...], int, tuple[str, ...], int, int]:
        """``head``, ``head_cut``, ``tail``, ``tail_cut`` and ``total_lines`` of
        ``CommandTranscript``; the current line is the last line."""
        head = self._head
        first = self._first
        tail = self._tail
        total = self._total
        current = self._current()
        if current is not None:
            end, start, chars = current
            total += 1
            if not self._head_closed and self._head_used + chars <= self._head_chars:
                head = [*head, end]
            else:
                if not self._head_closed and not head:
                    first = (start or end[: self._head_chars], chars)
                tail = tail.copy()
                tail.add(end, chars)
        if not head and first is not None:
            start, chars = first
            return (start,), chars - len(start), tail.lines, tail.cut, total
        return tuple(head), 0, tail.lines, tail.cut, total

    def latest(self, count: int) -> str:
        """The newest *count* lines as text, the current line included."""
        if count <= 0:
            return ""
        lines = self._tail.newest(count)
        current = self._current()
        if current is not None:
            lines.append(current[0])
        missing = count - len(lines)
        if missing > 0 and self._total == len(self._head) + len(self._tail) and not self._tail.cut:
            lines = self._head[-missing:] + lines
        return "\n".join(lines[-count:])


class OutputCleaner:
    """Cleans one command's output into ``OutputLines``.

    Each call returns how the lines changed (``OutputUpdate``).
    """

    def __init__(self, *, head_chars: int, tail_chars: int) -> None:
        self._filter = _EscapeFilter()
        self._lines = OutputLines(head_chars=head_chars, tail_chars=tail_chars)
        # A lone CR was seen: the next text replaces the current line.
        self._restart = False
        self._ended = False

    def feed(self, data: bytes) -> OutputUpdate:
        """Add output bytes."""
        if data and not self._ended:
            self._apply(self._filter.decode(data))
        return self._lines.take_update()

    def end(self) -> OutputUpdate:
        """The output ended: decode what is left and complete the current line."""
        if not self._ended:
            self._ended = True
            self._apply(self._filter.decode(b"", final=True))
            self._lines.end()
        return self._lines.take_update()

    def _apply(self, text: str) -> None:
        if not text:
            return
        if "\r\n" in text:
            # A line ends the same either way: the CR only restarts the line.
            text = text.replace("\r\n", "\n")
        if _LINE_CONTROL_BUT_LF.search(text) is None:
            if self._restart:
                self._restart = False
                # A line break keeps the line's text; other text replaces it.
                if not text.startswith("\n"):
                    self._lines.clear_line()
            self._lines.add_text(text)
            return
        position = 0
        for match in _LINE_CONTROL.finditer(text):
            if match.start() > position:
                self._append(text[position : match.start()])
            control = match.group()
            if control == "\n":
                self._restart = False
                self._lines.complete()
            elif control == "\r":
                self._restart = True
            elif control == "\b" and not self._restart:
                # After a lone CR the cursor is at the line's start: nothing to erase.
                self._lines.erase()
            position = match.end()
        if position < len(text):
            self._append(text[position:])

    def _append(self, text: str) -> None:
        if self._restart:
            self._restart = False
            self._lines.clear_line()
        self._lines.append(text)


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
        # C1 code points are dropped.
        return True

    def _start(self, state: str, *, osc: bool = False) -> None:
        self._state = state
        self._osc = osc
        self._length = 0


__all__ = ["OutputCleaner", "OutputLines", "OutputUpdate", "TailLines"]
