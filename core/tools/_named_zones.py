"""Read time zone names that automation Tool calls carry, and compare them with the server zone.

Scheduling Tools keep times in the server time zone. A call may still name its
own zone as an IANA name (any case), a UTC spelling, or a fixed offset such as
``+05:30``. These helpers resolve the name, find the exact instants at which a
zone changes its UTC offset over the coming year, and tell which instants a
local time names in a zone: none when the clocks skip it, two when they repeat
it.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from functools import cache, lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from core.tools._call_vocabulary import spelling

_UTC_NAMES = frozenset({"utc", "z", "gmt", "zulu", "etcutc", "etcgmt", "universal", "utc0", "gmt0"})
_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_LARGEST_OFFSET = timedelta(hours=14)
# Offset changes are found by stepping through the year and bisecting each step whose
# offsets differ. No zone changes its offset twice within one step, so none is missed.
_SCAN_STEP = timedelta(hours=1)
_HORIZON = timedelta(days=367)


def named_zone(name: str) -> tzinfo | None:
    """Return the zone a call names, or None when the name is not a time zone."""
    text = name.strip()
    if spelling(text) in _UTC_NAMES:
        return UTC
    match = _OFFSET.match(text)
    if match is not None:
        sign, hours, minutes = match.groups()
        size = timedelta(hours=int(hours), minutes=int(minutes or 0))
        if size > _LARGEST_OFFSET:
            return None
        return timezone(size if sign == "+" else -size)
    key = _zone_keys().get(text.casefold(), text)
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def offset(zone: tzinfo, moment: datetime) -> timedelta:
    """Return the UTC offset of ``zone`` at the instant ``moment`` names."""
    # Through UTC, so a local time the clocks skip reads the offset in force at its instant.
    return moment.astimezone(UTC).astimezone(zone).utcoffset() or timedelta(0)


def offset_changes(zone: tzinfo, now: datetime) -> list[datetime]:
    """The instants within the coming year at which ``zone`` changes its UTC offset."""
    changes = _changes_from(zone, now.astimezone(UTC).date())
    return [moment for moment in changes if moment > now]


def server_shifts(zone: tzinfo, server: ZoneInfo, now: datetime) -> set[timedelta]:
    """Every difference between server time and ``zone`` time over the coming year.

    The difference changes only where one of the zones changes its offset, so it
    is read now and just after every such change.
    """
    moments = [now, *offset_changes(zone, now), *offset_changes(server, now)]
    return {offset(server, moment) - offset(zone, moment) for moment in moments}


def same_zone(zone: tzinfo, server: ZoneInfo, now: datetime) -> bool:
    """Whether ``zone`` shows the same wall-clock time as the server all year."""
    if isinstance(zone, ZoneInfo) and zone.key == server.key:
        return True
    return server_shifts(zone, server, now) == {timedelta(0)}


def local_readings(wall: datetime, zone: tzinfo) -> list[datetime]:
    """The UTC instants at which ``zone`` clocks show the naive time ``wall``.

    One instant for an ordinary time, none for a time the clocks skip, and two,
    earliest first, for a time the clocks repeat when they go back.
    """
    readings: set[datetime] = set()
    for fold in (0, 1):
        moment = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if moment.astimezone(zone).replace(tzinfo=None) == wall:
            readings.add(moment)
    return sorted(readings)


def offset_matches(moment: datetime, zone: tzinfo) -> bool:
    """Whether an aware time's offset is one ``zone`` has at that local time."""
    return moment.astimezone(zone).replace(tzinfo=None) == moment.replace(tzinfo=None)


def unclear_local_time(
    wall: datetime, zone: tzinfo, name: str
) -> tuple[str, list[tuple[datetime, str]]] | None:
    """Explain a local time that names no single instant in ``zone``, with the instants meant.

    Returns None when the time names exactly one instant. Otherwise returns the
    reason and each candidate instant with a label in ``name`` time: for a time
    the clocks skip, the instants its offsets before and after the change give;
    for a repeated time, both occurrences.
    """
    readings = local_readings(wall, zone)
    if len(readings) == 1:
        return None
    shown = wall.isoformat(timespec="minutes")
    if readings:
        reason = f'"{shown}" happens twice in {name}: the clocks go back over it that day.'
    else:
        readings = sorted({wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)})
        reason = f'"{shown}" does not exist in {name}: the clocks jump over it that day.'
    labeled = [
        (moment, f"{moment.astimezone(zone).isoformat(timespec='minutes')} in {name}")
        for moment in readings
    ]
    return reason, labeled


def server_text(moment: datetime, server: ZoneInfo) -> str:
    """Render an aware moment as server-local ISO time with its offset."""
    return moment.astimezone(server).replace(microsecond=0).isoformat()


@lru_cache(maxsize=64)
def _changes_from(zone: tzinfo, day: date) -> tuple[datetime, ...]:
    if not isinstance(zone, ZoneInfo):
        # UTC and fixed offsets never change.
        return ()
    begin = datetime.combine(day, time.min, UTC)
    changes: list[datetime] = []
    before = begin
    before_offset = offset(zone, before)
    while before < begin + _HORIZON:
        after = before + _SCAN_STEP
        after_offset = offset(zone, after)
        if after_offset != before_offset:
            changes.append(_first_instant_of(zone, before, after, before_offset))
        before, before_offset = after, after_offset
    return tuple(changes)


def _first_instant_of(
    zone: tzinfo, low: datetime, high: datetime, low_offset: timedelta
) -> datetime:
    """The first second after ``low`` at which ``zone`` no longer has ``low_offset``."""
    # Offsets change on whole seconds, so bisect over whole seconds.
    first, last = 0, int((high - low).total_seconds())
    while last - first > 1:
        middle = (first + last) // 2
        if offset(zone, low + timedelta(seconds=middle)) == low_offset:
            first = middle
        else:
            last = middle
    return low + timedelta(seconds=last)


@cache
def _zone_keys() -> dict[str, str]:
    return {key.casefold(): key for key in available_timezones()}


__all__ = [
    "local_readings",
    "named_zone",
    "offset",
    "offset_changes",
    "offset_matches",
    "same_zone",
    "server_shifts",
    "server_text",
    "unclear_local_time",
]
