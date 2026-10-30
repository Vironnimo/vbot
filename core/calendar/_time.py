"""Calendar timezone, instant parsing and interval arithmetic."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone

from core.calendar.errors import (
    CalendarValidationError,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("calendar.service")


def _default_timezone() -> ZoneInfo:
    """Resolve the server's local zone, falling back to UTC when undetectable."""
    try:
        return get_localzone()
    except Exception as error:
        _LOGGER.warning("Could not resolve system timezone: %s", error)
        return ZoneInfo("UTC")


def _resolve_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise CalendarValidationError(
            f"timezone is not a known IANA timezone: {tz_name}"
        ) from error


def span_length(
    start: str, end: str, start_utc: str | None = None, end_utc: str | None = None
) -> timedelta:
    """Nominal wall-clock length, or elapsed length for an interval crossing back over itself."""
    length = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    if length <= timedelta(0) and start_utc is not None and end_utc is not None:
        return datetime.fromisoformat(end_utc) - datetime.fromisoformat(start_utc)
    return length


def input_span(
    start: datetime, end: datetime, zone: ZoneInfo, *, explicit_end: bool = False
) -> tuple[str, str]:
    """Resolve both endpoints, retaining explicit overlap instants and gap wall-clock lengths."""
    first = start.replace(tzinfo=zone).astimezone(UTC)
    shift = first.astimezone(zone).replace(tzinfo=None) - start
    last = end if explicit_end else (end + shift).replace(fold=end.fold)
    return first.isoformat(), last.replace(tzinfo=zone).astimezone(UTC).isoformat()


def _parse_iso_datetime(value: str, *, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise CalendarValidationError(f"{field_name} must be a valid ISO 8601 datetime") from error


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
