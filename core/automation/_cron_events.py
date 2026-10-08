"""Cron jobs bound to a calendar event: event times, owed occurrences and the Run's event note.

An event job runs once at every occurrence of its calendar event, at the
occurrence's start or end plus a signed offset. An occurrence whose due time
passed without a Run may still start late while its event needs it: until the
occurrence starts for a job due before it, until it ends for one due during
it, and for one due at or after its end until the event's next occurrence
starts; after the last occurrence, however late.

The calendar is read through :class:`EventCalendar`: an event by id and its
occurrences with each occurrence's changes applied.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast

from core.automation._cron_jobs import (
    MAX_EVENT_OFFSET_MINUTES,
    CronJob,
    CronJobValidationError,
    EventEdge,
    _parse_utc_timestamp,
)

if TYPE_CHECKING:
    from core.calendar import CalendarEvent, EventOccurrence

_EVENT_TIME = re.compile(r"^(start|end)(?:\s*([+-])\s*([1-9][0-9]*)\s*([mhd]))?$")
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 24 * 60}
_ONE_DAY = timedelta(days=1)
EVENT_TIME_GRAMMAR = "start or end, optionally + or - a duration such as 'start - 30m'"


class EventCalendar(Protocol):
    """What event jobs read from the calendar.

    ``get_event`` raises ``CalendarEventNotFoundError`` for an unknown id and
    ``CalendarStorageError`` while the calendar cannot be read.
    """

    def get_event(self, event_id: str) -> CalendarEvent:
        """The event ``event_id``."""
        ...

    def iter_occurrences(self, event: CalendarEvent, after: datetime) -> Iterator[EventOccurrence]:
        """The occurrences of ``event`` that end after ``after``, in start order, lazily."""
        ...

    def system_timezone_name(self) -> str:
        """The zone whose local days all-day events span."""
        ...

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` after every calendar change; return an unsubscribe function."""
        ...


@dataclass(frozen=True, slots=True)
class EventDue:
    """One occurrence of an event job's event and when the job is due for it."""

    occurrence: EventOccurrence
    due_at: datetime
    """The UTC instant the job is due for the occurrence."""
    zone_name: str
    """The zone the occurrence's local times are in."""


def parse_event_time(value: object) -> tuple[EventEdge, int]:
    """Read an event time such as ``start - 30m``: the edge and the signed offset in minutes."""
    text = " ".join(value.split()).lower() if isinstance(value, str) else ""
    match = _EVENT_TIME.fullmatch(text)
    if match is None:
        raise CronJobValidationError(f"event time must be {EVENT_TIME_GRAMMAR}")
    edge, sign, count, unit = match.groups()
    minutes = int(count) * _UNIT_MINUTES[unit] if count else 0
    if minutes > MAX_EVENT_OFFSET_MINUTES:
        raise CronJobValidationError("event time offset must not exceed 31 days")
    return cast(EventEdge, edge), -minutes if sign == "-" else minutes


def format_event_time(edge: str | None, offset_minutes: int | None) -> str:
    """The canonical event time of an edge and offset, such as ``start - 30m`` or ``end + 1d``."""
    offset = offset_minutes or 0
    if not offset:
        return edge or ""
    minutes = abs(offset)
    amount = f"{minutes}m"
    for unit in ("d", "h"):
        if minutes % _UNIT_MINUTES[unit] == 0:
            amount = f"{minutes // _UNIT_MINUTES[unit]}{unit}"
            break
    return f"{edge} {'-' if offset < 0 else '+'} {amount}"


def coverage(job: CronJob) -> datetime:
    """The instant through which an event job owes no occurrence.

    Its creation, activation or schedule change, or the due time of the last
    occurrence it started a Run for.
    """
    instants = [_parse_utc_timestamp(job.created_at, field_name="created_at")]
    if job.covered_until is not None:
        instants.append(_parse_utc_timestamp(job.covered_until, field_name="covered_until"))
    return max(instants)


def owed_occurrence(
    calendar: EventCalendar,
    event: CalendarEvent,
    job: CronJob,
    now: datetime,
    *,
    after: datetime | None = None,
) -> EventDue | None:
    """The earliest occurrence due at or before ``now`` that the job owes a Run.

    It came due after :func:`coverage` (and after ``after``, an instant through
    which nothing is owed) and can still start late.
    """
    floor = coverage(job) if after is None else max(coverage(job), after)
    for occurrence, due_at in _dues_between(calendar, event, job, floor, now):
        closes_at = _closes_at(calendar, event, occurrence, due_at)
        if closes_at is None or closes_at > now:
            return _event_due(calendar, event, occurrence, due_at)
    return None


def next_due(
    calendar: EventCalendar, event: CalendarEvent, job: CronJob, after: datetime
) -> EventDue | None:
    """The occurrence the job is next due for strictly after ``after``; None after the last."""
    offset = _offset(job)
    best: tuple[EventOccurrence, datetime] | None = None
    for occurrence in calendar.iter_occurrences(event, after - offset):
        # A later occurrence starts later, so it is due no earlier than this bound.
        if best is not None and occurrence.start_utc + offset >= best[1]:
            break
        due_at = _due_at(job, occurrence)
        if due_at > after and (best is None or due_at < best[1]):
            best = (occurrence, due_at)
    return None if best is None else _event_due(calendar, event, *best)


def dues_in_window(
    calendar: EventCalendar,
    event: CalendarEvent,
    job: CronJob,
    window_start: datetime,
    window_end: datetime,
    limit: int,
) -> list[EventDue]:
    """The occurrences the job is due for in the half-open UTC window, by due time."""
    tick = timedelta(microseconds=1)
    found = _dues_between(calendar, event, job, window_start - tick, window_end - tick)
    return [_event_due(calendar, event, occurrence, due_at) for occurrence, due_at in found[:limit]]


def can_fire(calendar: EventCalendar, event: CalendarEvent, job: CronJob, now: datetime) -> bool:
    """Whether the job can still start a Run for ``event``: an owed or a later occurrence.

    An inactive job owes nothing: enabling it covers what came due before.
    """
    if job.status == "active" and owed_occurrence(calendar, event, job, now) is not None:
        return True
    return next_due(calendar, event, job, now) is not None


def context_note(job: CronJob, due: EventDue) -> str:
    """The note before the Run's input that names the event the job is due for."""
    occurrence = due.occurrence
    names = f"event {occurrence.event_id}"
    if occurrence.recurring:
        names += f", occurrence {occurrence.id}"
    lines = [
        f"Cron job {job.id} is due ({format_event_time(job.event_edge, job.event_offset_minutes)})"
        f' for the calendar event "{occurrence.title}" ({names}).',
        f"Event time: {_event_time_text(occurrence)} ({due.zone_name})",
    ]
    if occurrence.location:
        lines.append(f"Location: {occurrence.location}")
    if occurrence.description:
        lines.append(f"Description: {occurrence.description}")
    return "\n".join(lines)


def _dues_between(
    calendar: EventCalendar,
    event: CalendarEvent,
    job: CronJob,
    after: datetime,
    until: datetime,
) -> list[tuple[EventOccurrence, datetime]]:
    """The occurrences the job is due for in ``(after, until]``, by due time."""
    offset = _offset(job)
    found: list[tuple[EventOccurrence, datetime]] = []
    # An occurrence due after ``after`` ends after ``after - offset``.
    for occurrence in calendar.iter_occurrences(event, after - offset):
        # Its due time and every later occurrence's lie beyond ``until``.
        if occurrence.start_utc + offset > until:
            break
        due_at = _due_at(job, occurrence)
        if after < due_at <= until:
            found.append((occurrence, due_at))
    found.sort(key=lambda item: (item[1], item[0].start_utc))
    return found


def _closes_at(
    calendar: EventCalendar, event: CalendarEvent, occurrence: EventOccurrence, due_at: datetime
) -> datetime | None:
    """Until when an occurrence due at ``due_at`` can still start; None for no limit."""
    if due_at < occurrence.start_utc:
        return occurrence.start_utc
    if due_at < occurrence.end_utc:
        return occurrence.end_utc
    # The next occurrence replaces it; one starting exactly at the due time does not.
    for following in calendar.iter_occurrences(event, due_at):
        if following.start_utc > due_at:
            return following.start_utc
    return None


def _due_at(job: CronJob, occurrence: EventOccurrence) -> datetime:
    edge = occurrence.start_utc if job.event_edge == "start" else occurrence.end_utc
    return edge + _offset(job)


def _offset(job: CronJob) -> timedelta:
    return timedelta(minutes=job.event_offset_minutes or 0)


def _event_due(
    calendar: EventCalendar, event: CalendarEvent, occurrence: EventOccurrence, due_at: datetime
) -> EventDue:
    zone_name = event.tz_name or calendar.system_timezone_name()
    return EventDue(occurrence=occurrence, due_at=due_at, zone_name=zone_name)


def _event_time_text(occurrence: EventOccurrence) -> str:
    if occurrence.all_day:
        last_day = date.fromisoformat(occurrence.end) - _ONE_DAY
        if last_day.isoformat() == occurrence.start:
            return f"{occurrence.start}, all day"
        return f"{occurrence.start} to {last_day.isoformat()}, all day"
    return f"{_minutes(occurrence.start)} to {_minutes(occurrence.end)}"


def _minutes(value: str) -> str:
    """A local time to the minute when its seconds are zero: 2030-01-10T15:00."""
    return value[:-3] if len(value) == 19 and value.endswith(":00") else value
