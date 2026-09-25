"""Read time zone names that automation Tool calls carry, and compare them with the server zone.

Scheduling Tools keep times in the server time zone. A call may still name its
own zone as an IANA name (any case), a UTC spelling, or a fixed offset such as
``+05:30``. These helpers resolve the name and tell whether a conversion is
exact over the coming year.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from core.tools._call_vocabulary import spelling

_UTC_NAMES = frozenset({"utc", "z", "gmt", "zulu", "etcutc", "etcgmt", "universal", "utc0", "gmt0"})
_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_LARGEST_OFFSET = timedelta(hours=14)
_SAMPLE_STEP = timedelta(days=7)
_SAMPLE_COUNT = 53


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
    """Return the UTC offset of ``zone`` at ``moment``."""
    return moment.astimezone(zone).utcoffset() or timedelta(0)


def year_samples(now: datetime) -> list[datetime]:
    """Weekly moments covering the coming year, where offsets can change."""
    return [now + _SAMPLE_STEP * index for index in range(_SAMPLE_COUNT)]


def server_shifts(zone: tzinfo, server: ZoneInfo, now: datetime) -> set[timedelta]:
    """Every difference between server time and ``zone`` time over the coming year."""
    return {offset(server, moment) - offset(zone, moment) for moment in year_samples(now)}


def same_zone(zone: tzinfo, server: ZoneInfo, now: datetime) -> bool:
    """Whether ``zone`` shows the same wall-clock time as the server all year."""
    if isinstance(zone, ZoneInfo) and zone.key == server.key:
        return True
    return server_shifts(zone, server, now) == {timedelta(0)}


def server_text(moment: datetime, server: ZoneInfo) -> str:
    """Render an aware moment as server-local ISO time with its offset."""
    return moment.astimezone(server).replace(microsecond=0).isoformat()


@cache
def _zone_keys() -> dict[str, str]:
    return {key.casefold(): key for key in available_timezones()}


__all__ = ["named_zone", "offset", "same_zone", "server_shifts", "server_text", "year_samples"]
