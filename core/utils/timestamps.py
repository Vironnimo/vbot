"""Canonical UTC timestamps for durable storage.

Every stored timestamp uses the fixed-width form ``YYYY-MM-DDTHH:MM:SS.ffffffZ``:
ISO 8601 with an explicit UTC designator, so plain text order equals time order.
This module is the one owner of that form: formatting, parsing and
normalization. Readers still accept other ISO 8601 forms with an explicit
offset, such as ``+00:00`` or a missing fraction, and normalize them on write.
A value without an offset is never guessed and raises ``ValueError``.
"""

from __future__ import annotations

from datetime import UTC, datetime

_CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_canonical_timestamp(value: datetime) -> str:
    """Format an aware *value* as a canonical UTC timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone information")
    return value.astimezone(UTC).strftime(_CANONICAL_FORMAT)


def parse_timestamp(value: str) -> datetime:
    """Parse ISO 8601 with an explicit offset (``Z`` or ``±HH:MM``) as aware UTC.

    A malformed value or one without an offset raises ``ValueError``.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty ISO 8601 string")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp must include timezone information: {value!r}")
    return parsed.astimezone(UTC)


def canonical_timestamp(value: str | datetime) -> str:
    """Return *value*, an aware datetime or ISO 8601 text, as a canonical UTC timestamp."""
    if isinstance(value, datetime):
        return format_canonical_timestamp(value)
    return format_canonical_timestamp(parse_timestamp(value))


def utc_now_timestamp() -> str:
    """Return the current time as a canonical UTC timestamp."""
    return format_canonical_timestamp(datetime.now(UTC))
