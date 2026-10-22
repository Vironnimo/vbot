"""Lone UTF-16 surrogates: text that UTF-8, SQLite and JSON wires cannot carry.

A Python ``str`` holds an astral character as one code point, so every code
point in U+D800-U+DFFF is a lone surrogate: half of a character that a Model or
a JSON escape (``"\\ud83d"``) left unpaired, or a byte that was not UTF-8 read
with ``surrogateescape``. Encoding such text to UTF-8 raises, so a Session
write, a Provider request or an event that carries one fails. vBot replaces
each with U+FFFD where outside text enters a Run; the Session store does the
same as the last guard before it writes.
"""

from __future__ import annotations

import re
from typing import Any, cast

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def replace_lone_surrogates(text: str) -> str:
    """Return *text* with every lone surrogate replaced by U+FFFD."""
    if _LONE_SURROGATE.search(text) is None:
        return text
    return _LONE_SURROGATE.sub("\ufffd", text)


def without_lone_surrogates[T](value: T) -> T:
    """Return a JSON-like *value* with every lone surrogate in its text replaced.

    Object keys count as text too. A value without one is returned itself, not
    a copy, so callers that share it (a Tool Call's arguments, an event
    payload) keep sharing it.
    """
    return cast(T, _replaced(value))


def _replaced(value: Any) -> Any:
    if isinstance(value, str):
        return replace_lone_surrogates(value)
    if isinstance(value, dict):
        items = [(_replaced(key), _replaced(item)) for key, item in value.items()]
        if all(
            new_key is key and new_item is item
            for (new_key, new_item), (key, item) in zip(items, value.items(), strict=True)
        ):
            return value
        return dict(items)
    if isinstance(value, list | tuple):
        replaced = [_replaced(item) for item in value]
        if all(new is old for new, old in zip(replaced, value, strict=True)):
            return value
        return replaced if isinstance(value, list) else tuple(replaced)
    return value
