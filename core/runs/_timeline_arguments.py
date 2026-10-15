"""Bounded previews of completed string fields in streamed Tool arguments."""

from __future__ import annotations

import json
import re

_VALUE_LIMIT = 200
_TOKEN_LIMIT = _VALUE_LIMIT * 2


class ToolArgumentPreview:
    """Scan each fragment once without retaining a streamed file's contents.

    Only completed top-level strings are displayable. Nested values, incomplete
    strings and malformed tokens never become a claim about the final arguments.
    """

    def __init__(self) -> None:
        self._started = False
        self._finished = False
        self._depth = 0
        self._slot = "key"
        self._in_string = False
        self._escaped = False
        self._role = "skip"
        self._token = ""
        self._truncated = False
        self._key: str | None = None
        self._fields: dict[str, str] = {}

    def fields(self) -> dict[str, str] | None:
        return dict(self._fields) if self._fields else None

    def push(self, text: str) -> bool:
        changed = False
        for character in text:
            if self._finished:
                break
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif character == "\\":
                    self._escaped = True
                elif character == '"':
                    self._in_string = False
                    changed = self._end_string() or changed
                    continue
                if self._role != "skip":
                    if len(self._token) < _TOKEN_LIMIT:
                        self._token += character
                    else:
                        self._truncated = True
                continue
            if character in " \t\r\n":
                continue
            if not self._started:
                self._started = True
                self._finished = character != "{"
                self._depth = 1
                continue
            if character == '"':
                self._in_string = True
                self._escaped = False
                self._token = ""
                self._truncated = False
                self._role = self._slot if self._depth == 1 else "skip"
            elif character in "{[":
                if self._depth == 1 and self._slot == "value":
                    self._slot = "after"
                self._depth += 1
            elif character in "}]":
                self._depth -= 1
                self._finished = self._depth <= 0
            elif character == ":" and self._depth == 1 and self._slot == "colon":
                self._slot = "value"
            elif character == "," and self._depth == 1:
                self._slot = "key"
            elif self._depth == 1 and self._slot == "value":
                self._slot = "after"
        return changed

    def _end_string(self) -> bool:
        if self._role == "key":
            self._key = None if self._truncated else _decode(self._token)
            self._slot = "colon"
            return False
        if self._role != "value":
            return False
        self._slot = "after"
        key, self._key = self._key, None
        if key is None:
            return False
        token = self._token
        if self._truncated:
            # The capture bound may split an escape; discard only that unfinished
            # escape before decoding the completed prefix for display.
            match = re.search(r"(\\+)(?:u[0-9a-fA-F]{0,3})?$", token)
            if match and len(match[1]) % 2:
                token = token[: match.start() + len(match[1]) - 1]
        value = _decode(token)
        if value is None:
            return False
        if self._truncated or len(value) > _VALUE_LIMIT:
            value = value[:_VALUE_LIMIT] + "…"
        if self._fields.get(key) == value:
            return False
        self._fields[key] = value
        return True


def _decode(token: str) -> str | None:
    try:
        value = json.loads('"' + token + '"')
    except ValueError:
        return None
    return value if isinstance(value, str) else None
