"""Parse agent-facing ``when`` expressions into concrete UTC query windows.

The grammar spares the model all date arithmetic: named spans (today, this
week, next month, ...) are resolved against the server timezone and the
current time, so a model never computes week or month boundaries itself.
Windows are half-open: ``[start, end)`` with an inclusive day bound expanding
to the whole local day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.calendar.errors import CalendarValidationError

WHEN_GRAMMAR = (
    "today, tomorrow, this week, next week, this month, next month, "
    "a date (YYYY-MM-DD), a year-month (YYYY-MM), or 'start..end'"
)
_DATE_LENGTH = 10
_MONTH_LENGTH = 7
_RANGE_SEPARATOR = ".."


def parse_when(value: str, *, now_utc: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Resolve one ``when`` expression to a half-open [start, end) UTC window."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if not text:
        raise CalendarValidationError(f"when must not be empty; use one of: {WHEN_GRAMMAR}")
    if _RANGE_SEPARATOR in text:
        return _parse_range(text, tz=tz)
    today = now_utc.astimezone(tz).date()
    if text == "today":
        return _day_window(today, tz)
    if text == "tomorrow":
        return _day_window(today + timedelta(days=1), tz)
    if text == "this week":
        return _week_window(today - timedelta(days=today.weekday()), tz)
    if text == "next week":
        return _week_window(today + timedelta(days=7 - today.weekday()), tz)
    if text == "this month":
        return _month_window(today.year, today.month, tz=tz)
    if text == "next month":
        return _month_window(*_next_month(today.year, today.month), tz=tz)
    if looks_like_date(text):
        return _day_window(_parse_date(text), tz)
    if _looks_like_month(text):
        return _month_window(*_parse_month(text), tz=tz)
    raise CalendarValidationError(f"cannot parse when {value!r}; use one of: {WHEN_GRAMMAR}")


def looks_like_date(text: str) -> bool:
    """Return whether ``text`` has the YYYY-MM-DD date shape."""
    return len(text) == _DATE_LENGTH and text[4] == "-" and text[7] == "-"


def _parse_range(text: str, *, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Resolve ``start..end`` where each side is a date or an ISO datetime."""
    start_text, end_text = (part.strip() for part in text.split(_RANGE_SEPARATOR, 1))
    window_start = _parse_range_bound(start_text, tz=tz, is_end=False)
    window_end = _parse_range_bound(end_text, tz=tz, is_end=True)
    if window_end <= window_start:
        raise CalendarValidationError(
            f"when range end must be after its start: {text!r}; use one of: {WHEN_GRAMMAR}"
        )
    return window_start.astimezone(UTC), window_end.astimezone(UTC)


def _parse_range_bound(text: str, *, tz: ZoneInfo, is_end: bool) -> datetime:
    if looks_like_date(text):
        local_midnight = datetime.combine(_parse_date(text), time.min).replace(tzinfo=tz)
        return local_midnight + timedelta(days=1) if is_end else local_midnight
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise CalendarValidationError(
            f"cannot parse when bound {text!r}; use one of: {WHEN_GRAMMAR}"
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min).replace(tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _week_window(monday: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(monday, time.min).replace(tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=7)).astimezone(UTC)


def _month_window(year: int, month: int, *, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(date(year, month, 1), time.min).replace(tzinfo=tz)
    next_year, next_month = _next_month(year, month)
    end = datetime.combine(date(next_year, next_month, 1), time.min).replace(tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _looks_like_month(text: str) -> bool:
    return len(text) == _MONTH_LENGTH and text[4] == "-"


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise CalendarValidationError(
            f"cannot parse when date {text!r}; use one of: {WHEN_GRAMMAR}"
        ) from error


def _parse_month(text: str) -> tuple[int, int]:
    year_text, month_text = text.split("-", 1)
    try:
        year, month = int(year_text), int(month_text)
    except ValueError as error:
        raise CalendarValidationError(
            f"cannot parse when month {text!r}; use one of: {WHEN_GRAMMAR}"
        ) from error
    if not 1 <= month <= 12:
        raise CalendarValidationError(
            f"cannot parse when month {text!r}; use one of: {WHEN_GRAMMAR}"
        )
    return year, month
