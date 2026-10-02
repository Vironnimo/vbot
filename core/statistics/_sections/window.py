"""Report windows: hour-aligned bounds, the previous window and series buckets.

A window is half-open: ``since`` (floored to the hour) is inside it and
``until`` (ceiled to the next hour) is not. Aggregates keyed by UTC hour and
facts keyed by instant select with the same bounds, so every section of one
report covers exactly the same time. Calendar days are local to an IANA zone;
hours fold into the local date of their start, so a zone with a half-hour
offset gets its day boundaries at the containing UTC hour. A window of at
most ``HOUR_BUCKET_SPAN`` has hour buckets in its series, a longer or
all-time window local days.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.statistics._projection import MICROSECONDS_PER_HOUR, datetime_instant
from core.utils.timestamps import format_canonical_timestamp

JsonObject = dict[str, object]

_HOUR = timedelta(hours=1)
# The longest window whose series have one point per hour instead of per day.
HOUR_BUCKET_SPAN = timedelta(hours=48)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Bounds of an open window side, beyond every stored instant and hour.
_MIN = -(2**62)
_MAX = 2**62


def floor_hour(value: datetime) -> datetime:
    """Return the start of the UTC hour containing an aware ``value``."""
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def ceil_hour(value: datetime) -> datetime:
    """Return the first UTC hour start at or after an aware ``value``."""
    floored = floor_hour(value)
    return floored if floored == value else floored + _HOUR


def zone_for(name: str) -> ZoneInfo:
    """Return the IANA zone ``name``; an unknown or malformed name raises ``ValueError``."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown timezone: {name!r}") from error


def hour_start(hour: int) -> datetime:
    """Return the UTC start of an hour number (``instant // MICROSECONDS_PER_HOUR``)."""
    return _EPOCH + timedelta(hours=hour)


def hour_timestamp(hour: int) -> str:
    return format_canonical_timestamp(hour_start(hour))


def instant_timestamp(instant: int) -> str:
    """Return the canonical timestamp of an instant in UTC microseconds."""
    return format_canonical_timestamp(_EPOCH + timedelta(microseconds=instant))


@dataclass(frozen=True)
class ReportWindow:
    """One report's hour-aligned window, its calendar zone and the report clock."""

    since: datetime | None
    until: datetime | None
    zone: ZoneInfo
    now: datetime

    @classmethod
    def create(
        cls,
        *,
        since: datetime | None,
        until: datetime | None,
        timezone: str,
        now: datetime,
    ) -> ReportWindow:
        """Align requested bounds to hours; ``since`` after ``until`` raises ``ValueError``."""
        if since is not None and until is not None and since > until:
            raise ValueError("since must not be after until")
        return cls(
            since=None if since is None else floor_hour(since),
            until=None if until is None else ceil_hour(until),
            zone=zone_for(timezone),
            now=now,
        )

    @property
    def instants(self) -> tuple[int, int]:
        """The half-open ``[low, high)`` instant bounds (UTC microseconds)."""
        return (
            _MIN if self.since is None else datetime_instant(self.since),
            _MAX if self.until is None else datetime_instant(self.until),
        )

    @property
    def hours(self) -> tuple[int, int]:
        """The half-open ``[low, high)`` hour-number bounds."""
        low, high = self.instants
        return (
            _MIN if self.since is None else low // MICROSECONDS_PER_HOUR,
            _MAX if self.until is None else high // MICROSECONDS_PER_HOUR,
        )

    @property
    def windowed(self) -> bool:
        return self.since is not None or self.until is not None

    @property
    def length(self) -> timedelta | None:
        """The window's length; ``None`` without ``since``.

        Without ``until`` the window runs to the current hour's end.
        """
        if self.since is None:
            return None
        end = self.until if self.until is not None else ceil_hour(self.now)
        return max(end - self.since, timedelta(0))

    @property
    def bucket(self) -> str:
        """``"hour"`` for a window of at most ``HOUR_BUCKET_SPAN``, else ``"day"``."""
        length = self.length
        return "hour" if length is not None and length <= HOUR_BUCKET_SPAN else "day"

    def previous(self) -> ReportWindow | None:
        """The window of equal length that ends at ``since``; ``None`` without ``since``."""
        length = self.length
        if self.since is None or length is None:
            return None
        return ReportWindow(self.since - length, self.since, self.zone, self.now)

    def echo(self) -> JsonObject:
        return {
            "since": None if self.since is None else format_canonical_timestamp(self.since),
            "until": None if self.until is None else format_canonical_timestamp(self.until),
            "timezone": self.zone.key,
            "bucket": self.bucket,
        }

    def series_hours(self, first_active: int | None, last_active: int | None) -> range | None:
        """The inclusive hour range a series covers; ``None`` for an empty series.

        An open start begins at the first active hour; an open end runs to the
        current hour, or a later active hour.
        """
        low, high = self.hours
        now_hour = datetime_instant(self.now) // MICROSECONDS_PER_HOUR
        first = low if self.since is not None else first_active
        if self.until is not None:
            last = high - 1
        else:
            last = max(now_hour, last_active) if last_active is not None else now_hour
        if first is None or last < first:
            return None
        return range(first, last + 1)


class LocalCalendar:
    """Map hour numbers to local dates and hours of one zone, with a per-hour cache."""

    def __init__(self, zone: ZoneInfo) -> None:
        self._zone = zone
        self._hours: dict[int, tuple[str, int]] = {}

    def local(self, hour: int) -> tuple[str, int]:
        """Return the local ISO date and local hour of day at the start of ``hour``."""
        cached = self._hours.get(hour)
        if cached is None:
            moment = hour_start(hour).astimezone(self._zone)
            cached = (moment.date().isoformat(), moment.hour)
            self._hours[hour] = cached
        return cached

    def date(self, hour: int) -> str:
        return self.local(hour)[0]

    def days(self, hours: range | None) -> list[str]:
        """Every local date from the first to the last hour of ``hours``, gaps included."""
        if hours is None or not hours:
            return []
        first = date.fromisoformat(self.date(hours[0]))
        last = date.fromisoformat(self.date(hours[-1]))
        return [
            (first + timedelta(days=offset)).isoformat()
            for offset in range((last - first).days + 1)
        ]


class SeriesBuckets:
    """The points of one report's time series: hours of a short window, else local days.

    A day point is keyed ``date`` (local ISO date), an hour point
    ``hour_start`` (canonical UTC timestamp of the hour). Every bucket from
    the series start to its end is present, gaps included.
    """

    def __init__(self, window: ReportWindow, calendar: LocalCalendar) -> None:
        self._window = window
        self._calendar = calendar
        self._hourly = window.bucket == "hour"
        self._hour_keys: dict[int, str] = {}
        self.field = "hour_start" if self._hourly else "date"

    def keys(self, active: Collection[int]) -> list[str]:
        """Every bucket key of the series around the ``active`` hour numbers, in order."""
        hours = self._window.series_hours(min(active, default=None), max(active, default=None))
        if not self._hourly:
            return self._calendar.days(hours)
        return [self.key(hour) for hour in hours or ()]

    def key(self, hour: int) -> str:
        """The key of the bucket holding hour number ``hour``."""
        if not self._hourly:
            return self._calendar.date(hour)
        key = self._hour_keys.get(hour)
        if key is None:
            key = self._hour_keys[hour] = hour_timestamp(hour)
        return key
