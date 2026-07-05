"""Shared timestamp parsing for the statistics package.

A single pure helper that both the main :mod:`core.statistics.statistics`
aggregator and the :mod:`core.statistics.skills` section import. It lives in its
own leaf module because ``statistics`` imports ``skills`` (to wire the skills
section), so the helper cannot live in ``statistics`` without a circular import
back from ``skills``. No I/O, no state — just the ISO-8601 → UTC parse the
report's window filtering and min/max timestamp tracking share.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

UTC_Z_SUFFIX = "Z"
UTC_OFFSET_SUFFIX = "+00:00"


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a UTC ``datetime``, or ``None``.

    Accepts a trailing ``Z``, treats a naive timestamp as UTC, and normalizes
    any offset to UTC. A non-string, empty, or unparseable value yields ``None``
    so callers can apply their own lenient fallback.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = (
            value.removesuffix(UTC_Z_SUFFIX) + UTC_OFFSET_SUFFIX
            if value.endswith(UTC_Z_SUFFIX)
            else value
        )
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
