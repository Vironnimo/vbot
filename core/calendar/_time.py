"""Calendar timezone, instant parsing and interval arithmetic."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone

from core.calendar.errors import (
    CalendarValidationError,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("calendar.service")


_DATE_ONLY_PATTERN_LENGTH = 10


def _default_timezone() -> ZoneInfo:
    """Resolve the server's local zone, falling back to UTC when undetectable."""
    try:
        zone = get_localzone()
        return zone if isinstance(zone, ZoneInfo) else ZoneInfo(str(zone))
    except Exception as error:
        _LOGGER.warning("Could not resolve system timezone: %s", error)
        return ZoneInfo("UTC")


def _resolve_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise CalendarValidationError(f"tz is not a known IANA timezone: {tz_name}") from error


def _local_naive_iso(value: datetime, zone: ZoneInfo) -> str:
    """Render one UTC instant as a naive local datetime string in ``zone``."""
    return value.astimezone(zone).replace(tzinfo=None, microsecond=0).isoformat()


def _looks_like_date(value: str) -> bool:
    return len(value) == _DATE_ONLY_PATTERN_LENGTH and value[4] == "-" and value[7] == "-"


def _parse_iso_datetime(value: str, *, field_name: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as error:
        raise CalendarValidationError(f"{field_name} must be a valid ISO 8601 datetime") from error


def _parse_utc_instant(value: str, *, field_name: str) -> datetime:
    parsed = _parse_iso_datetime(value, field_name=field_name)
    if parsed.tzinfo is None:
        raise CalendarValidationError(f"{field_name} must include timezone information")
    return parsed


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _round_up_to_minutes(value: datetime, minutes: int) -> datetime:
    step_seconds = minutes * 60
    # Align against UTC explicitly; timestamp() on a naive datetime would
    # silently use the host's local zone, drifting an ostensibly-UTC cursor.
    epoch_seconds = _as_utc(value).timestamp()
    rounded = math.ceil(epoch_seconds / step_seconds) * step_seconds
    return datetime.fromtimestamp(rounded, tz=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
