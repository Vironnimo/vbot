"""Incrementally decode one process pipe without exposing terminal controls."""

from __future__ import annotations

import codecs


class ProcessOutputDecoder:
    """Keep UTF-8 and bounded ANSI parser state separate for each pipe."""

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "text"
        self._osc = False

    def decode(self, data: bytes, *, final: bool = False) -> bytes:
        output: list[str] = []
        for char in self._utf8.decode(data, final=final):
            if self._state == "string":
                if char == "\x9c" or (self._osc and char == "\x07"):
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "string_escape"
            elif self._state == "string_escape":
                self._state = "text" if char == "\\" else "string"
            elif self._state == "csi":
                if "\x40" <= char <= "\x7e":
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "escape"
            elif self._state == "escape":
                if char == "[":
                    self._state = "csi"
                elif char in "]PX^_":
                    self._osc = char == "]"
                    self._state = "string"
                elif not "\x20" <= char <= "\x2f":
                    self._state = "text"
            elif char == "\x1b":
                self._state = "escape"
            elif char == "\x9b":
                self._state = "csi"
            elif char in "\x90\x98\x9d\x9e\x9f":
                self._osc = char == "\x9d"
                self._state = "string"
            elif not "\x80" <= char <= "\x9f":
                output.append(char)
        return "".join(output).encode("utf-8")
