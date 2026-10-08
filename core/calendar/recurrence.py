"""RFC 5545 repetition rules of calendar events.

An event repeats by one RRULE, stored without its ``RRULE:`` prefix. A timed
event repeats at a local wall-clock time in its IANA zone, so "every Monday
09:00" stays 09:00 across DST transitions: dateutil expands the rule on naive
local datetimes, and the zone is attached to each occurrence afterwards. An
all-day event repeats on dates, expanded from local midnight.

The calendar expands rules of daily or coarser frequency with every BY part
RFC 5545 defines except BYSECOND. A rule that never produces an occurrence is
refused instead of being stored.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr

from core.calendar.errors import CalendarValidationError

RRULE_FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
MAX_RRULE_INTERVAL = 1000
MAX_RRULE_COUNT = 10000
MAX_RRULE_LENGTH = 500
# BYHOUR x BYMINUTE: how many starts a rule may give one day.
MAX_DAILY_STARTS = 24
# Events start in these years: the probe below shifts a start by whole Gregorian
# cycles towards the last year dateutil expands.
FIRST_EVENT_YEAR = 1000
LAST_EVENT_YEAR = 9000

_PARTS = (
    "FREQ",
    "INTERVAL",
    "COUNT",
    "UNTIL",
    "BYMONTH",
    "BYWEEKNO",
    "BYYEARDAY",
    "BYMONTHDAY",
    "BYDAY",
    "BYHOUR",
    "BYMINUTE",
    "BYSETPOS",
    "WKST",
)
_UNTIL = re.compile(r"(\d{8})(?:T(\d{6})(Z?))?")
_PREFIX = "RRULE:"
_END_OF_DAY = time(23, 59, 59)
# The Gregorian calendar repeats every 400 years, weekdays included.
_CYCLE_YEARS = 400
_MAX_YEAR = 9999
_RULE_EXAMPLE = "such as FREQ=WEEKLY;BYDAY=MO,WE"


def parse_date_string(value: object, *, field_name: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` calendar date."""
    if not isinstance(value, str) or not value.strip():
        raise CalendarValidationError(f"{field_name} must be a date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CalendarValidationError(f"{field_name} must be a date in YYYY-MM-DD form") from error


def normalize_rrule(value: object, *, start: datetime, zone: ZoneInfo | None) -> str | None:
    """Validate one RRULE text for an event starting at ``start``; return its stored form.

    ``start`` is the event's naive local start, midnight for an all-day event;
    ``zone`` is a timed event's zone and None for an all-day event. An ``RRULE:``
    prefix and any letter case are accepted. None or an empty text means no
    repetition. The stored form names FREQ first and keeps the other parts in
    the order given.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise CalendarValidationError(f"rrule must be a text {_RULE_EXAMPLE}")
    text = "".join(value.split()).upper()
    if text.startswith(_PREFIX):
        text = text[len(_PREFIX) :]
    text = text.strip(";")
    if not text:
        return None
    if len(text) > MAX_RRULE_LENGTH:
        raise CalendarValidationError(f"rrule must not exceed {MAX_RRULE_LENGTH} characters")
    parts = _rule_parts(text)
    _check_parts(parts, all_day=zone is None)
    if not text.isascii():
        raise CalendarValidationError(f"rrule must be plain ASCII text {_RULE_EXAMPLE}")
    rule = _build(parts, start, zone)
    _check_first_occurrence(rule, parts, start, zone)
    ordered = {"FREQ": parts["FREQ"]} | parts
    return ";".join(f"{key}={item}" for key, item in ordered.items())


def build_rule(rule_text: str, start: datetime, zone: ZoneInfo | None) -> rrule:
    """The dateutil rule of a stored RRULE for an event starting at ``start``.

    ``start`` and ``zone`` mean what they mean for :func:`normalize_rrule`.
    """
    return _build(_rule_parts(rule_text), start, zone)


def resolve_local_span(
    naive_start: datetime, tz: ZoneInfo, duration: timedelta
) -> tuple[datetime, datetime]:
    """Return one wall-clock occurrence's UTC (start, end).

    A start in a DST gap is shifted forward by the gap (round-trip through the
    pre-transition offset, RFC 5545 section 3.3.5 / erratum 4271); ambiguous
    starts use the first occurrence (fold=0). The duration is wall-clock time.
    """
    local_start = naive_start.replace(tzinfo=tz).astimezone(UTC).astimezone(tz)
    return local_start.astimezone(UTC), (local_start + duration).astimezone(UTC)


def _rule_parts(text: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for item in text.split(";"):
        key, separator, part_value = item.partition("=")
        if not separator or not key or not part_value:
            raise CalendarValidationError(
                f'rrule part "{item}" must be NAME=VALUE, {_RULE_EXAMPLE}'
            )
        if key not in _PARTS:
            raise CalendarValidationError(f"rrule part {key} is not supported")
        if key in parts:
            raise CalendarValidationError(f"rrule gives {key} twice")
        parts[key] = part_value
    return parts


def _check_parts(parts: dict[str, str], *, all_day: bool) -> None:
    frequency = parts.get("FREQ")
    if frequency not in RRULE_FREQUENCIES:
        raise CalendarValidationError("rrule FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY")
    _check_whole_number(parts, "INTERVAL", MAX_RRULE_INTERVAL)
    _check_whole_number(parts, "COUNT", MAX_RRULE_COUNT)
    if "COUNT" in parts and "UNTIL" in parts:
        raise CalendarValidationError("rrule must give COUNT or UNTIL, not both")
    if "UNTIL" in parts and _UNTIL.fullmatch(parts["UNTIL"]) is None:
        raise CalendarValidationError(
            "rrule UNTIL must be a date such as 20301231, or a time such as 20301231T235959Z"
        )
    if all_day and ("BYHOUR" in parts or "BYMINUTE" in parts):
        raise CalendarValidationError(
            "an all-day event repeats on days; rrule must not give BYHOUR or BYMINUTE"
        )
    starts = len(parts.get("BYHOUR", "0").split(",")) * len(parts.get("BYMINUTE", "0").split(","))
    if starts > MAX_DAILY_STARTS:
        raise CalendarValidationError(
            f"rrule must not repeat more than {MAX_DAILY_STARTS} times a day"
        )


def _check_whole_number(parts: dict[str, str], key: str, maximum: int) -> None:
    text = parts.get(key)
    if text is not None and (
        not (text.isascii() and text.isdecimal()) or not 1 <= int(text) <= maximum
    ):
        raise CalendarValidationError(f"rrule {key} must be a whole number from 1 to {maximum}")


def _build(parts: dict[str, str], start: datetime, zone: ZoneInfo | None) -> rrule:
    expanded = ";".join(f"{key}={item}" for key, item in parts.items() if key != "UNTIL")
    try:
        rule = cast(rrule, rrulestr(f"{_PREFIX}{expanded}", dtstart=start))
    except (ValueError, TypeError) as error:
        raise CalendarValidationError(f"rrule is not valid: {error}") from error
    until = parts.get("UNTIL")
    if until is None:
        return rule
    return cast(rrule, rule.replace(until=_until(until, zone)))


def _until(text: str, zone: ZoneInfo | None) -> datetime:
    """The last start an UNTIL allows, as a naive local time of the event."""
    match = _UNTIL.fullmatch(text)
    assert match is not None
    try:
        day = datetime.strptime(match.group(1), "%Y%m%d")
        clock = (
            None if match.group(2) is None else datetime.strptime(match.group(2), "%H%M%S").time()
        )
    except ValueError as error:
        raise CalendarValidationError(f"rrule UNTIL {text} is not a real date or time") from error
    if clock is None:
        # A date allows every start of that local day.
        return day if zone is None else datetime.combine(day.date(), _END_OF_DAY)
    moment = datetime.combine(day.date(), clock)
    if match.group(3):
        moment = moment.replace(tzinfo=UTC)
        if zone is not None:
            moment = moment.astimezone(zone)
        moment = moment.replace(tzinfo=None)
    if zone is None:
        # All-day occurrences start at midnight: a time allows its own day.
        return datetime.combine(moment.date(), time.min)
    return moment


def _check_first_occurrence(
    rule: rrule, parts: dict[str, str], start: datetime, zone: ZoneInfo | None
) -> None:
    """Refuse a rule without any occurrence from the event's start on.

    dateutil looks for the next occurrence until the year 9999, which takes
    seconds for a rule that has none. The calendar repeats every 400 years, so
    the probe moves the start by whole cycles towards that year and looks at
    the rule there; its first occurrence, moved back, is the rule's first one.
    """
    if not FIRST_EVENT_YEAR <= start.year <= LAST_EVENT_YEAR:
        raise CalendarValidationError(
            f"a repeating event must start between the years {FIRST_EVENT_YEAR} and "
            f"{LAST_EVENT_YEAR}"
        )
    shift = (_MAX_YEAR - 2 * _CYCLE_YEARS - start.year) // _CYCLE_YEARS * _CYCLE_YEARS
    unbounded = {key: item for key, item in parts.items() if key not in {"COUNT", "UNTIL"}}
    probe = _build(unbounded, start.replace(year=start.year + shift), zone)
    first = next(iter(probe), None)
    if first is None:
        raise CalendarValidationError("rrule produces no occurrence")
    first = first.replace(year=first.year - shift)
    if "UNTIL" in parts and first > _until(parts["UNTIL"], zone):
        raise CalendarValidationError("rrule UNTIL ends the repetition before its first occurrence")


__all__ = [
    "MAX_RRULE_COUNT",
    "MAX_RRULE_INTERVAL",
    "RRULE_FREQUENCIES",
    "build_rule",
    "normalize_rrule",
    "parse_date_string",
    "resolve_local_span",
]
