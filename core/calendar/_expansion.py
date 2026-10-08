"""Expand calendar events into occurrences: in a window, or lazily from an instant on.

Pure functions of an event and the server zone (which places all-day days).
A repeating event's occurrences come from its rule, minus removed occurrences,
with each occurrence's changes applied; a changed occurrence may also start
far from its original start.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule

from core.calendar._events import CalendarEvent, EventOccurrence, occurrence_id
from core.calendar._time import _resolve_zone
from core.calendar.recurrence import build_rule, resolve_local_span

# Rule starts this much before a window still reach into it through their length.
_MARGIN = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class _Series:
    """An event read for expansion: anchor, length, zone and rule."""

    event: CalendarEvent
    zone: ZoneInfo | None
    system_tz: ZoneInfo
    start: datetime
    length: timedelta
    rule: rrule | None


def event_series(event: CalendarEvent, system_tz: ZoneInfo) -> _Series:
    """Read ``event`` for expansion; raises CalendarValidationError for an unusable record."""
    zone = None if event.tz_name is None else _resolve_zone(event.tz_name)
    start = local_time(event.start)
    length = local_time(event.end) - start
    rule = None if event.rrule is None else build_rule(event.rrule, start, zone)
    return _Series(event, zone, system_tz, start, length, rule)


def local_time(text: str) -> datetime:
    """A stored start, end or key as a naive datetime: midnight for a date."""
    return datetime.fromisoformat(text)


def occurrence_key(moment: datetime, *, all_day: bool) -> str:
    """The stored form of a naive local start: a date, or a time to the second."""
    if all_day:
        return moment.date().isoformat()
    return moment.replace(microsecond=0).isoformat()


def window_occurrences(
    event: CalendarEvent,
    window_start: datetime,
    window_end: datetime,
    system_tz: ZoneInfo,
    limit: int,
) -> list[EventOccurrence]:
    """The occurrences of ``event`` overlapping the half-open UTC window, in start order."""
    series = event_series(event, system_tz)
    if series.rule is None:
        single = _occurrence(series, series.start, None)
        return [single] if _overlaps(single, window_start, window_end) else []
    low = _local(series, window_start) - series.length - _MARGIN
    high = _local(series, window_end) + _MARGIN
    found: dict[str, EventOccurrence] = {}
    for moment in series.rule.between(low, high, inc=True):
        key = occurrence_key(moment, all_day=series.zone is None)
        if key in event.exdates:
            continue
        item = _occurrence(series, moment, event.overrides.get(key))
        if _overlaps(item, window_start, window_end):
            found[key] = item
    for item in _moved_occurrences(series):
        if item.original_start not in found and _overlaps(item, window_start, window_end):
            found[str(item.original_start)] = item
    ordered = sorted(found.values(), key=lambda item: (item.start_utc, item.id))
    return ordered[:limit]


def iter_occurrences(
    event: CalendarEvent, after: datetime, system_tz: ZoneInfo
) -> Iterator[EventOccurrence]:
    """Yield the occurrences of ``event`` that end after ``after``, in start order, lazily."""
    series = event_series(event, system_tz)
    if series.rule is None:
        single = _occurrence(series, series.start, None)
        if single.end_utc > after:
            yield single
        return
    moved = [item for item in _moved_occurrences(series) if item.end_utc > after]
    yield from heapq.merge(
        _rule_occurrences(series, after),
        sorted(moved, key=lambda item: item.start_utc),
        key=lambda item: item.start_utc,
    )


def single_occurrence(event: CalendarEvent, system_tz: ZoneInfo) -> EventOccurrence:
    """The one occurrence of a single event."""
    series = event_series(event, system_tz)
    return _occurrence(series, series.start, None)


def occurrence_keys(event: CalendarEvent, keys: Iterable[str]) -> frozenset[str]:
    """Those of ``keys`` that are original starts of occurrences of a repeating ``event``.

    Walks the rule once up to the last key, so checking many keys costs one expansion.
    """
    series = event_series(event, ZoneInfo("UTC"))
    candidates = set(keys)
    if series.rule is None or not candidates:
        return frozenset()
    moments = []
    for key in candidates:
        try:
            moments.append(local_time(key))
        except ValueError:
            continue
    if not moments:
        return frozenset()
    produced = {
        occurrence_key(moment, all_day=series.zone is None)
        for moment in series.rule.between(min(moments), max(moments), inc=True)
    }
    return frozenset(candidates & produced)


def occurrence_at(event: CalendarEvent, key: str, system_tz: ZoneInfo) -> EventOccurrence | None:
    """The occurrence of a repeating ``event`` that originally starts at ``key``, if any."""
    series = event_series(event, system_tz)
    if series.rule is None or key in event.exdates or not has_key(series.rule, key, series.zone):
        return None
    return _occurrence(series, local_time(key), event.overrides.get(key))


def has_key(rule: rrule, key: str, zone: ZoneInfo | None) -> bool:
    """Whether ``rule`` produces an occurrence that originally starts at ``key``."""
    try:
        moment = local_time(key)
    except ValueError:
        return False
    if occurrence_key(moment, all_day=zone is None) != key:
        return False
    return moment in rule


def _rule_occurrences(series: _Series, after: datetime) -> Iterator[EventOccurrence]:
    """The occurrences at their original times that end after ``after``, in order."""
    assert series.rule is not None
    event = series.event
    low = _local(series, after) - series.length - _MARGIN
    for moment in series.rule.xafter(low, inc=True):
        key = occurrence_key(moment, all_day=series.zone is None)
        override = event.overrides.get(key)
        if key in event.exdates or (override is not None and "start" in override):
            continue
        item = _occurrence(series, moment, override)
        if item.end_utc > after:
            yield item


def _moved_occurrences(series: _Series) -> list[EventOccurrence]:
    """The occurrences whose override gives them another start."""
    event = series.event
    return [
        _occurrence(series, local_time(key), override)
        for key, override in event.overrides.items()
        if "start" in override and key not in event.exdates
    ]


def _occurrence(
    series: _Series, original: datetime, override: dict[str, Any] | None
) -> EventOccurrence:
    event = series.event
    changes = override or {}
    start, length = original, series.length
    if "start" in changes:
        start = local_time(changes["start"])
        length = local_time(changes["end"]) - start
    all_day = series.zone is None
    if series.zone is None:
        start_utc = _midnight(start.date(), series.system_tz)
        end_utc = _midnight((start + length).date(), series.system_tz)
    else:
        start_utc, end_utc = resolve_local_span(start, series.zone, length)
    recurring = series.rule is not None
    key = occurrence_key(original, all_day=all_day) if recurring else None
    return EventOccurrence(
        id=event.id if key is None else occurrence_id(event.id, key),
        event_id=event.id,
        title=changes.get("title", event.title),
        description=changes.get("description", event.description),
        location=changes.get("location", event.location),
        all_day=all_day,
        recurring=recurring,
        start=occurrence_key(start, all_day=all_day),
        end=occurrence_key(start + length, all_day=all_day),
        start_utc=start_utc,
        end_utc=end_utc,
        original_start=key,
        overridden=bool(changes),
    )


def _local(series: _Series, instant: datetime) -> datetime:
    """A UTC instant as a naive time of the event: its zone, or the server's day for all-day."""
    if series.zone is None:
        return datetime.combine(instant.astimezone(series.system_tz).date(), time.min)
    return instant.astimezone(series.zone).replace(tzinfo=None)


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def _overlaps(item: EventOccurrence, window_start: datetime, window_end: datetime) -> bool:
    return item.end_utc > window_start and item.start_utc < window_end
