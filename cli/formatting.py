"""Shared value-rendering helpers for the CLI management commands.

The management modules render RPC result fields as deterministic, agent-facing
plain text. These small formatters were copy-pasted across ~14 of them; this is
their one home. Modules import them under their local ``_name`` convention.
``duration_text`` also serves the TUI and the tray's toasts, which write
durations as the WebUI does.
"""

from __future__ import annotations

import sys
import unicodedata
from collections.abc import Sequence
from contextvars import ContextVar

output_mode: ContextVar[str] = ContextVar("cli_output_mode", default="auto")
#: Characters that are invisible or steer the terminal: controls (escape sequences,
#: line breaks), format characters (bidirectional overrides, zero-width marks) and
#: line and paragraph separators.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def human_output() -> bool:
    mode = output_mode.get()
    return mode == "human" or (mode == "auto" and sys.stdout.isatty())


def record_fields(fields: Sequence[str], *, separator: str = " ") -> str:
    """Lay out already formatted fields without parsing or truncating their values."""
    if not human_output():
        return separator.join(fields)
    # Concatenated legacy fields carry one separator space. Values themselves
    # (including leading whitespace in Tool/Skill descriptions) stay untouched.
    readable = [
        field.removeprefix(" ") if index and separator == "" else field
        for index, field in enumerate(fields)
    ]
    return "\n  ".join(readable)


def bool_text(value: object) -> str:
    """Render a tri-state boolean as ``yes`` / ``no`` / ``unknown``."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def value_text(value: object) -> str:
    """Render any value as text, showing ``-`` for ``None``."""
    if value is None:
        return "-"
    return str(value)


def inline_text(value: object) -> str:
    """Render text another party chose on one line, its hidden characters escaped.

    Device names, client addresses and other server-supplied values may carry
    escape sequences or bidirectional overrides; each such character shows as
    a Python-style escape (``\\x1b``, ``\\u202e``) instead of reaching the
    terminal. ``None`` shows ``-``.
    """
    if value is None:
        return "-"
    return "".join(
        ascii(character)[1:-1]
        if unicodedata.category(character) in _HIDDEN_CATEGORIES
        else character
        for character in str(value)
    )


def string_or_default(value: object, default: str) -> str:
    """Return *value* when it is a non-empty string, else *default*."""
    if isinstance(value, str) and value:
        return value
    return default


def format_string_list(value: object) -> str:
    """Render a list as a comma-joined string (``-`` for non-list, ``[]`` for empty)."""
    if not isinstance(value, list):
        return "-"
    if not value:
        return "[]"
    return ",".join(str(item) for item in value)


def duration_text(milliseconds: float) -> str:
    """A duration as the WebUI writes it: 0.4s, 12s, 1m 5s, 1h 2m."""
    seconds = max(0.0, milliseconds / 1000)
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{round(seconds)}s"
    hours, rest = divmod(round(seconds), 3600)
    minutes, seconds_left = divmod(rest, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds_left}s"
