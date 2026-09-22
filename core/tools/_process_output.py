"""Incrementally decode one process pipe without exposing terminal controls."""

from __future__ import annotations

import codecs
import re

# Characters that may start or be part of a terminal control in plain text.
_TEXT_CONTROL = re.compile(r"[\x1b\x80-\x9f]")
# Legitimate control strings (titles, hyperlinks) and CSI parameters are short.
# Longer unterminated sequences are treated as garbage so they cannot hide output.
_MAX_STRING_CHARACTERS = 4096
_MAX_SEQUENCE_CHARACTERS = 64


class ProcessOutputDecoder:
    """Keep UTF-8 and bounded ANSI parser state separate for each pipe.

    Escape, CSI, and control-string state is bounded: a character that cannot
    continue the current sequence (for example a line break after an unterminated
    ``ESC [``) ends it and is then processed as ordinary output, and control
    strings end at a line break or after a bounded length. Dropped sequence
    characters never reach the output, so no terminal control is exposed.
    """

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "text"
        self._osc = False
        self._length = 0

    def decode(self, data: bytes, *, final: bool = False) -> bytes:
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
            if self._consume(text[index]):
                index += 1
        return "".join(output).encode("utf-8")

    def _consume(self, char: str) -> bool:
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
            elif "\x30" <= char <= "\x7e":
                self._state = "text"
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
