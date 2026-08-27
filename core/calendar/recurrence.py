"""RFC 5545 recurrence semantics for the calendar domain (v1 subset).

Recurrence follows RFC 5545 wall-clock anchoring: a recurring event starts at a
local wall time in its IANA zone, so "every Monday 09:00" stays 09:00 across DST
transitions. Expansion runs on naive local datetimes and attaches the zone per
occurrence afterwards. The v1 subset covers daily/weekly/monthly/yearly
frequencies, an interval, a count or inclusive until date, and weekday
restrictions for weekly rules; anything richer is rejected instead of being
expanded incorrectly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from dateutil.rrule import (
    DAILY,
    FR,
    MO,
    MONTHLY,
    SA,
    SU,
    TH,
    TU,
    WE,
    WEEKLY,
    YEARLY,
    rrule,
)

from core.calendar.errors import CalendarValidationError

ALLOWED_RRULE_FREQS = frozenset(("daily", "weekly", "monthly", "yearly"))
WEEKDAY_CODES = ("mo", "tu", "we", "th", "fr", "sa", "su")
MAX_RRULE_INTERVAL = 1000
MAX_RRULE_COUNT = 10000

_RRULE_FIELDS = frozenset(("freq", "interval", "count", "until", "by_weekday"))
_DATEUTIL_FREQS = {
    "daily": DAILY,
    "weekly": WEEKLY,
    "monthly": MONTHLY,
    "yearly": YEARLY,
}
_DATEUTIL_WEEKDAYS = {
    "mo": MO,
    "tu": TU,
    "we": WE,
    "th": TH,
    "fr": FR,
    "sa": SA,
    "su": SU,
}
_END_OF_DAY = time(23, 59, 59)


def parse_date_string(value: object, *, field_name: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` calendar date."""
    if not isinstance(value, str) or not value.strip():
        raise CalendarValidationError(f"{field_name} must be a date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CalendarValidationError(f"{field_name} must be a date in YYYY-MM-DD form") from error


def normalize_rrule(payload: object) -> dict[str, Any] | None:
    """Validate and normalize one rrule payload into its persisted form."""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise CalendarValidationError("rrule must be an object")
    unknown_fields = sorted(set(payload) - _RRULE_FIELDS)
    if unknown_fields:
        raise CalendarValidationError(f"Unsupported rrule fields: {', '.join(unknown_fields)}")

    freq = payload.get("freq")
    if not isinstance(freq, str) or freq not in _DATEUTIL_FREQS:
        options = ", ".join(sorted(_DATEUTIL_FREQS))
        raise CalendarValidationError(f"rrule.freq must be one of: {options}")

    interval = payload.get("interval", 1)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 1 <= interval <= MAX_RRULE_INTERVAL
    ):
        raise CalendarValidationError(
            f"rrule.interval must be an integer between 1 and {MAX_RRULE_INTERVAL}"
        )

    count = payload.get("count")
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_RRULE_COUNT
    ):
        raise CalendarValidationError(
            f"rrule.count must be an integer between 1 and {MAX_RRULE_COUNT}"
        )

    until = payload.get("until")
    until_date = None if until is None else parse_date_string(until, field_name="rrule.until")

    by_weekday = payload.get("by_weekday")
    if by_weekday is not None:
        if freq != "weekly":
            raise CalendarValidationError("rrule.by_weekday is only valid for weekly recurrence")
        if (
            not isinstance(by_weekday, list)
            or not by_weekday
            or not all(isinstance(code, str) and code in _DATEUTIL_WEEKDAYS for code in by_weekday)
        ):
            raise CalendarValidationError(
                "rrule.by_weekday must be a non-empty list drawn from: " + ", ".join(WEEKDAY_CODES)
            )
        by_weekday = sorted(set(by_weekday))

    return {
        "freq": freq,
        "interval": interval,
        "count": count,
        "until": until_date.isoformat() if until_date is not None else None,
        "by_weekday": by_weekday,
    }


def expand_recurring_timed(
    *,
    start_local: datetime,
    tz: ZoneInfo,
    rrule_spec: dict[str, Any],
    duration_minutes: int,
    exdates: frozenset[str],
    window_start_utc: datetime,
    window_end_utc: datetime,
    max_occurrences: int,
) -> list[tuple[datetime, datetime]]:
    """Expand one recurring timed event into UTC (start, end) pairs.

    Returns occurrences overlapping the half-open window. Occurrence ends use
    wall-clock arithmetic in the event zone, so a meeting keeps its local
    09:00-10:00 shape even when a DST transition falls inside it.
    """
    duration = timedelta(minutes=duration_minutes)
    window_start_local = window_start_utc.astimezone(tz).replace(tzinfo=None)
    window_end_local = window_end_utc.astimezone(tz).replace(tzinfo=None)
    rule = _build_rrule(start_local, rrule_spec)
    candidates = rule.between(
        window_start_local - duration,
        window_end_local,
        inc=True,
    )

    occurrences: list[tuple[datetime, datetime]] = []
    for naive_start in candidates:
        if naive_start.isoformat() in exdates:
            continue
        if naive_start < start_local:
            continue
        local_start = naive_start.replace(tzinfo=tz)
        local_end = local_start + duration
        start_utc = local_start.astimezone(UTC)
        end_utc = local_end.astimezone(UTC)
        if end_utc <= window_start_utc or start_utc >= window_end_utc:
            continue
        occurrences.append((start_utc, end_utc))
        if len(occurrences) >= max_occurrences:
            break
    return occurrences


def expand_recurring_allday(
    *,
    start_date: date,
    duration_days: int,
    rrule_spec: dict[str, Any],
    exdates: frozenset[str],
    window_start_utc: datetime,
    window_end_utc: datetime,
    system_tz: ZoneInfo,
    max_occurrences: int,
) -> list[tuple[date, date]]:
    """Expand one recurring all-day event into inclusive (start, exclusive end) dates."""
    window_start_date = window_start_utc.astimezone(system_tz).date()
    window_end_date = window_end_utc.astimezone(system_tz).date()
    rule = _build_rrule(datetime.combine(start_date, time.min), rrule_spec)
    lookback_start = datetime.combine(window_start_date - timedelta(days=duration_days), time.min)
    window_end_bound = datetime.combine(window_end_date, time.min)
    candidates = rule.between(lookback_start, window_end_bound, inc=True)

    occurrences: list[tuple[date, date]] = []
    for naive_start in candidates:
        occurrence_date = naive_start.date()
        if occurrence_date.isoformat() in exdates:
            continue
        occurrence_end = occurrence_date + timedelta(days=duration_days)
        if occurrence_end <= window_start_date or occurrence_date >= window_end_date:
            continue
        occurrences.append((occurrence_date, occurrence_end))
        if len(occurrences) >= max_occurrences:
            break
    return occurrences


def _build_rrule(dtstart: datetime, spec: dict[str, Any]) -> rrule:
    kwargs: dict[str, Any] = {"dtstart": dtstart, "interval": spec["interval"]}
    if spec.get("count") is not None:
        kwargs["count"] = spec["count"]
    if spec.get("until") is not None:
        kwargs["until"] = datetime.combine(
            parse_date_string(spec["until"], field_name="rrule.until"), _END_OF_DAY
        )
    if spec.get("by_weekday"):
        kwargs["byweekday"] = tuple(_DATEUTIL_WEEKDAYS[code] for code in spec["by_weekday"])
    frequency = cast(Any, _DATEUTIL_FREQS[spec["freq"]])
    return cast(rrule, rrule(frequency, **kwargs))
