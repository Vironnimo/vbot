"""Incremental interpretation of raw command output, shared by read and the transcript.

Standard library only: the command cleaner also runs on a subinterpreter.
The raw log is never rewritten. This decoder returns UTF-8 for display and
records the lines where it interpreted UTF-16, omitted NULs or inferred a switch.
"""

from __future__ import annotations

import codecs
import re
from typing import Literal, NamedTuple

# UTF-16LE text without a byte order mark counts from this many characters
# U+0001-U+00FF in a row, each its byte and a NUL; a NUL beside UTF-8 is stray.
_UTF16_RUN_UNITS = 3
_UTF16_LATIN = re.compile(rb"(?:[\x01-\xff]\x00)+")
# Where UTF-8 stops: a NUL byte, or either UTF-16 byte order mark.
_UTF8_STOP = re.compile(rb"\x00|\xff\xfe|\xfe\xff")
# Aligned UTF-16 line ends. A BOM applies through a whole line, including CJK
# characters whose bytes also happen to be valid ASCII or UTF-8.
_UTF16_LINE_END = {
    "utf-16-le": re.compile(rb"(?:..)*?[\r\n]\x00", re.DOTALL),
    "utf-16-be": re.compile(rb"(?:..)*?\x00[\r\n]", re.DOTALL),
}
# A possible unmarked UTF-8 line after UTF-16 is checked within this bound.
_RESUME_WINDOW = 256
# What a decision reads past its position; a chunk holds it back for the next one.
_LOOKAHEAD = _RESUME_WINDOW + 8


class DecoderState(NamedTuple):
    """Compact interpretation state, transferable to the command worker's fallback.

    Diagnostic line ranges are not part of the state; only subsequent output and
    its line positions are restored. All members are immutable standard types.
    """

    buffer: bytes
    mode: Literal["utf-8", "latin", "bom"]
    after_utf16_line: bool
    encoding: str
    utf16: tuple[bytes, int]
    inferred_utf8: bool
    emitted_ascii: bytes
    line: int
    after_cr: bool


class CommandOutputDecoder:
    """A command's log as UTF-8, decoded run by run, with the lines that held UTF-16.

    On Windows some programs, such as wsl.exe, write UTF-16LE to a pipe, between
    the UTF-8 of the command's other output, at any byte offset, so no single
    encoding fits the whole log. Valid UTF-8 comes first outside a BOM. UTF-16LE is read in
    clear runs: at least ``_UTF16_RUN_UNITS`` characters U+0001-U+00FF in a row
    (each its byte and a NUL; text beyond U+00FF has no NUL to show it), or the
    text after a byte order mark, in that mark's byte order. After a UTF-16 line
    ends, a complete, NUL-free UTF-8 line can start the next run. This inference
    is named in the result; an unterminated or long ambiguous suffix keeps the
    BOM's encoding. Unframed encoding switches cannot always be distinguished
    from ordinary Unicode characters.
    Other NUL bytes are left out. ``utf16_lines`` and ``nul_lines`` record the
    lines, numbered as the renderer splits them, that held text decoded as UTF-16
    and NUL bytes left out, as ranges in ascending order.
    """

    def __init__(self, *, track_lines: bool = True) -> None:
        self._track_lines = track_lines
        self._buffer = b""
        # One ASCII byte already shown: it can also start a later UTF-16LE run,
        # with exactly the same displayed character. Keep that lookbehind without
        # delaying live output; _emit removes it from the next decoded piece.
        self._emitted_ascii = b""
        self._mode: Literal["utf-8", "latin", "bom"] = "utf-8"
        self._after_utf16_line = False
        self._utf16_encoding = "utf-16-le"
        self._utf16: codecs.IncrementalDecoder = codecs.getincrementaldecoder("utf-16-le")(
            errors="replace"
        )
        self._inferred_utf8 = False
        self.line = 1
        self._after_cr = False
        self.utf16_lines: list[list[int]] = []
        self.nul_lines: list[list[int]] = []
        self.inferred_utf8_lines: list[list[int]] = []

    def getstate(self) -> DecoderState:
        return DecoderState(
            self._buffer,
            self._mode,
            self._after_utf16_line,
            self._utf16_encoding,
            self._utf16.getstate(),
            self._inferred_utf8,
            self._emitted_ascii,
            self.line,
            self._after_cr,
        )

    def setstate(self, state: DecoderState) -> None:
        self._buffer = state.buffer
        self._mode = state.mode
        self._after_utf16_line = state.after_utf16_line
        self._utf16_encoding = state.encoding
        self._utf16 = codecs.getincrementaldecoder(state.encoding)(errors="replace")
        self._utf16.setstate(state.utf16)
        self._inferred_utf8 = state.inferred_utf8
        self._emitted_ascii = state.emitted_ascii
        self.line = state.line
        self._after_cr = state.after_cr

    def decode(self, data: bytes, *, final: bool = False) -> bytes:
        """Return the UTF-8 for *data*, holding back what the next bytes decide."""
        buffer = self._buffer + data
        out: list[bytes] = []
        position, more = 0, True
        while more:
            if self._mode == "utf-8":
                position, more = self._utf8(buffer, position, final, out)
            elif self._mode == "latin":
                position, more = self._latin(buffer, position, final, out)
            else:
                position, more = self._bom(buffer, position, final, out)
        self._buffer = buffer[position:]
        if final and self._mode == "bom":
            self._emit_utf16(b"", out, final=True)
        return b"".join(out)

    def _utf8(
        self, buffer: bytes, position: int, final: bool, out: list[bytes]
    ) -> tuple[int, bool]:
        end = len(buffer)
        stop = _UTF8_STOP.search(buffer, position)
        found = stop.start() if stop else end
        start = found - 1  # where UTF-16LE text before a NUL would start
        latin = (
            stop is not None
            and buffer[found] == 0
            and start >= position
            and buffer[start] != 0
            and not _ends_utf8_sequence(buffer, position, start)
        )
        if stop is None:
            cut = end if final else max(position, _utf8_boundary(buffer, position, end))
            if cut == end and end > position and not final:
                last = end - 1
                if buffer[last] < 0x80:
                    self._emit_utf8(buffer[position:end], out)
                    self._emitted_ascii = buffer[last:end]
                    return last, False
                if not _ends_utf8_sequence(buffer, position, last):
                    cut = last  # a non-ASCII byte can begin an unmarked UTF-16LE run
            self._emit_utf8(buffer[position:cut], out)
            return cut, False
        if buffer[found]:
            self._emit_utf8(buffer[position:found], out)
            self._start_bom(buffer[found : found + 2])
            return found + 2, True
        if latin:
            run = _UTF16_LATIN.match(buffer, start)
            if run and run.end() - start >= 2 * _UTF16_RUN_UNITS:
                self._emit_utf8(buffer[position:start], out)
                self._mode = "latin"
                self._inferred_utf8 = False
                return start, True
            if run and not final and run.end() >= end - 1:
                self._emit_utf8(buffer[position:start], out)
                return start, False
        self._emit_utf8(buffer[position:found], out)
        if self._track_lines:
            add_line_range(self.nul_lines, self.line, self.line)
        return found + 1, True

    def _latin(
        self, buffer: bytes, position: int, final: bool, out: list[bytes]
    ) -> tuple[int, bool]:
        run = _UTF16_LATIN.match(buffer, position)
        stop = run.end() if run else position
        self._emit(buffer[position:stop:2].decode("latin-1").encode(), out, utf16=True)
        if stop + 1 >= len(buffer) and not final:
            return stop, False
        self._mode = "utf-8"
        return stop, True

    def _bom(self, buffer: bytes, position: int, final: bool, out: list[bytes]) -> tuple[int, bool]:
        end = len(buffer)
        if end - position < 2:
            if not final:
                return position, False
            self._emit_utf16(buffer[position:], out, final=True)
            return end, False
        if self._after_utf16_line:
            mark = buffer[position : position + 2]
            if mark in (b"\xff\xfe", b"\xfe\xff"):
                self._emit_utf16(b"", out, final=True)
                self._start_bom(mark)
                return position + 2, True
            resumes = _resumes_utf8(buffer, position, self._utf16_encoding, final=final)
            if resumes is None:
                return position, False
            if resumes:
                self._emit_utf16(b"", out, final=True)
                self._mode = "utf-8"
                self._inferred_utf8 = True
                return position, True
        found = _UTF16_LINE_END[self._utf16_encoding].match(buffer, position)
        stop = found.end() if found else position + (end - position) // 2 * 2
        self._emit_utf16(buffer[position:stop], out)
        self._after_utf16_line = found is not None
        return stop, found is not None or (final and stop < end)

    def _start_bom(self, mark: bytes) -> None:
        self._utf16_encoding = "utf-16-le" if mark == b"\xff\xfe" else "utf-16-be"
        self._utf16 = codecs.getincrementaldecoder(self._utf16_encoding)(errors="replace")
        self._mode = "bom"
        self._after_utf16_line = False
        self._inferred_utf8 = False

    def _emit_utf8(self, data: bytes, out: list[bytes]) -> None:
        if not data.isascii():
            data = data.decode("utf-8", errors="replace").encode()
        self._emit(data, out, utf16=False)

    def _emit_utf16(self, data: bytes, out: list[bytes], *, final: bool = False) -> None:
        text = self._utf16.decode(data, final)
        if final:
            self._utf16.reset()
        first, *rest = text.split("\x00")
        self._emit(first.encode(), out, utf16=True)
        for part in rest:
            if self._track_lines:
                add_line_range(self.nul_lines, self.line, self.line)
            self._emit(part.encode(), out, utf16=True)

    def _emit(self, data: bytes, out: list[bytes], *, utf16: bool) -> None:
        """Append *data* and count its lines as the renderer splits them."""
        if not data:
            return
        if self._emitted_ascii:
            if utf16 and self._track_lines:
                number = self.line - (self._emitted_ascii in (b"\r", b"\n"))
                add_line_range(self.utf16_lines, number, number)
            data = data[1:]
            self._emitted_ascii = b""
            if not data:
                return
        if not self._track_lines:
            out.append(data)
            return
        # A line feed after a carriage return ends the line that return ended.
        first = self.line - 1 if self._after_cr and data.startswith(b"\n") else self.line
        breaks = data.count(b"\n") + data.count(b"\r") - data.count(b"\r\n")
        self.line += breaks - (first < self.line)
        if utf16:
            last = self.line - 1 if data.endswith((b"\n", b"\r")) else self.line
            add_line_range(self.utf16_lines, first, last)
        elif self._inferred_utf8:
            last = self.line - 1 if data.endswith((b"\n", b"\r")) else self.line
            add_line_range(self.inferred_utf8_lines, first, last)
        self._after_cr = data.endswith(b"\r")
        out.append(data)


def _ends_utf8_sequence(buffer: bytes, low: int, index: int) -> bool:
    """Whether ``buffer[index]`` ends a valid multi-byte UTF-8 character from *low* on."""
    if not 0x80 <= buffer[index] <= 0xBF:
        return False
    for start in range(index - 1, max(low, index - 3) - 1, -1):
        if not 0x80 <= buffer[start] <= 0xBF:
            try:
                buffer[start : index + 1].decode("utf-8")
            except UnicodeDecodeError:
                return False
            return True
    return False


def _utf8_boundary(buffer: bytes, low: int, index: int) -> int:
    """Move *index* back before a UTF-8 character that starts before it and ends after it."""
    for start in range(index - 1, max(low, index - 3) - 1, -1):
        byte = buffer[start]
        if byte < 0x80:
            return index
        if byte >= 0xC0:
            length = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return start if start + length > index else index
    return index


def _resumes_utf8(buffer: bytes, index: int, encoding: str, *, final: bool) -> bool | None:
    """A bounded, complete UTF-8 line after a UTF-16 line: a possible new run.

    The zero high byte after a UTF-16LE CR or LF belongs to that line end, so
    include it when checking for NULs. None waits for more evidence; clear UTF-16
    is available immediately, without waiting for the whole inference window.
    """
    window = buffer[index : index + _RESUME_WINDOW]
    # An actual UTF-16 line end is stronger evidence than a CR/LF byte inside
    # another code unit (such as U+4E0A or U+0A4E). Keep the BOM in that case.
    if _UTF16_LINE_END[encoding].match(window) is not None:
        return False
    line_end = re.search(rb"[\r\n]", window[1:])
    if line_end is None:
        return False if b"\x00" in window or final or len(buffer) - index >= _LOOKAHEAD else None
    stop = line_end.end() + 1
    segment = window[:stop]
    if b"\x00" in segment or buffer[index + stop : index + stop + 1] == b"\x00":
        return False
    try:
        segment.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True if final or len(buffer) - index >= _LOOKAHEAD else None


def add_line_range(ranges: list[list[int]], first: int, last: int) -> None:
    """Add lines *first* to *last* to ascending *ranges*, joining a neighboring range."""
    if ranges and first <= ranges[-1][1] + 1:
        ranges[-1][1] = max(ranges[-1][1], last)
    else:
        ranges.append([first, last])
