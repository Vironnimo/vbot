"""Canonical UTC timestamps for durable storage.

Every stored timestamp uses the fixed-width form ``YYYY-MM-DDTHH:MM:SS.ffffffZ``:
ISO 8601 with an explicit UTC designator, so plain text order equals time order.
Readers still accept other ISO 8601 forms with an explicit offset, such as
``+00:00`` or a missing fraction, and normalize them on write.
"""

from __future__ import annotations

from datetime import UTC, datetime

_CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_canonical_timestamp(value: datetime) -> str:
    """Format an aware *value* as a canonical UTC timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone information")
    return value.astimezone(UTC).strftime(_CANONICAL_FORMAT)


def canonical_timestamp(value: str | datetime) -> str:
    """Return *value* as a canonical UTC timestamp.

    Strings must be ISO 8601 with an explicit offset (``Z`` or ``±HH:MM``);
    a naive value raises ``ValueError``.
    """
    if isinstance(value, datetime):
        return format_canonical_timestamp(value)
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty ISO 8601 string")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO 8601 timestamp: {value!r}") from exc
    return format_canonical_timestamp(parsed)


def utc_now_timestamp() -> str:
    """Return the current time as a canonical UTC timestamp."""
    return datetime.now(UTC).strftime(_CANONICAL_FORMAT)
