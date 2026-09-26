"""Read the times a ``calendar`` call writes as server-local times, and render them back.

Events and windows live in the server time zone. A call may still name its own
zone, give an end instead of a duration, or read a window in another zone; these
helpers rewrite such times into server time when the reading is exact and refuse
the call, with the corrected call, when it is not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from core.calendar.when import parse_when
from core.tools._calendar_arguments import (
    END_FIELD,
    OMIT,
    STAND_INS,
    TIMEZONE_FIELD,
    CalendarCallRefusedError,
    choice,
    is_date,
    is_time_of_day,
    parse_local,
    refusal,
    render_call,
)
from core.tools._named_zones import (
    local_readings,
    named_zone,
    offset,
    offset_matches,
    same_zone,
    server_shifts,
    server_text,
    unclear_local_time,
)
from core.tools.tools import JsonObject

if TYPE_CHECKING:
    from core.calendar import CalendarService

_FREE_WINDOW_DAYS = 7


def read_window(
    arguments: JsonObject, server: ZoneInfo, default: str | None
) -> tuple[datetime, datetime, str | None]:
    """Resolve the list or find_free window, read in the call's time zone when it names one."""
    now = datetime.now(UTC)
    zone: tzinfo = server
    name = arguments.get(TIMEZONE_FIELD)
    if isinstance(name, str):
        found = named_zone(name)
        if found is None:
            raise CalendarCallRefusedError(unknown_zone(name, server, arguments))
        if not same_zone(found, server, now):
            zone = found
    # parse_when only attaches and converts the zone, which works for any tzinfo.
    reading_zone = cast("ZoneInfo", zone)
    when = arguments.get("when")
    if isinstance(when, str):
        window = parse_when(when, now_utc=now, tz=reading_zone)
    elif default is not None:
        window = parse_when(default, now_utc=now, tz=reading_zone)
    else:
        today, _ = parse_when("today", now_utc=now, tz=reading_zone)
        window = (today, today + timedelta(days=_FREE_WINDOW_DAYS))
    note = None
    if zone is not server:
        note = (
            f"Read the window in {name}: {window_text(*window, server)} in the server time "
            f"zone {server}."
        )
    return window[0], window[1], note


def apply_timezone(
    arguments: JsonObject, server: ZoneInfo, *, recurring: bool
) -> tuple[JsonObject, str | None]:
    """Rewrite start and end written in another zone as server time, when that is exact."""
    result = dict(arguments)
    name = result.pop(TIMEZONE_FIELD, None)
    if not isinstance(name, str):
        return result, None
    start, end = result.get("start"), result.get(END_FIELD)
    if isinstance(start, str) and isinstance(end, str) and is_time_of_day(end):
        begin = parse_local(start)
        if begin is not None:
            # "16:00" is on the start's day in the named zone, like the start.
            result[END_FIELD] = f"{begin.date().isoformat()}T{end.strip()}"
    fields = [
        field
        for field in ("start", END_FIELD)
        if isinstance(result.get(field), str) and parse_local(str(result[field])) is not None
    ]
    if not fields:
        # Dates of all-day events are the same in every zone.
        return result, None
    now = datetime.now(UTC)
    zone = named_zone(name)
    if zone is None:
        raise CalendarCallRefusedError(unknown_zone(name, server, result))
    if same_zone(zone, server, now):
        return result, None
    # Corrected calls keep the zone, so the other time still reads as the call meant it.
    call = {**result, TIMEZONE_FIELD: name}
    moments = {field: _zone_instant(call, field, name, zone, recurring) for field in fields}
    if recurring:
        converted = _repeating_in_server_time(call, moments, name, zone, server, now)
    else:
        converted = {field: server_text(moment, server) for field, moment in moments.items()}
    written = [f'"{result[field]}"' for field in fields]
    shown = [
        datetime.fromisoformat(converted[field]).replace(tzinfo=None).isoformat(timespec="minutes")
        for field in fields
    ]
    result.update(converted)
    return result, (
        f"Read {' and '.join(written)} as {name} time: {' and '.join(shown)} in the server "
        f"time zone {server}."
    )


def _zone_instant(
    call: JsonObject, field: str, name: str, zone: tzinfo, recurring: bool
) -> datetime:
    """The instant a time field names in ``zone``; refuses a time that names no single one."""
    text = str(call[field]).strip()
    parsed = parse_local(text)
    assert parsed is not None
    wall = parsed.replace(tzinfo=None)
    if parsed.tzinfo is None:
        if recurring:
            # Every occurrence keeps this wall-clock time; the first one only measures the offset.
            return wall.replace(tzinfo=zone)
        return named_instant(call, field, wall, name, zone)
    if offset_matches(parsed, zone):
        return parsed
    reason = f'"{text}" carries an offset that is not {name} time.'
    unclear = unclear_local_time(wall, zone, name)
    if unclear is None:
        meant = [(local_readings(wall, zone)[0], f"{wall.isoformat(timespec='minutes')} in {name}")]
    else:
        reason = f"{reason[:-1]}, and {unclear[0]}"
        meant = unclear[1]
    meant.insert(0, (parsed, f"{text} as written"))
    raise CalendarCallRefusedError(_readings_choice(call, field, zone, reason, meant))


def named_instant(
    call: JsonObject, field: str, wall: datetime, name: str, zone: tzinfo
) -> datetime:
    """The one instant a naive time names in ``zone``; refuses a time the clocks skip or repeat.

    The refusal offers each instant meant as a time with ``zone``'s offset, so the
    corrected call keeps the zone for its other times.
    """
    unclear = unclear_local_time(wall, zone, name)
    if unclear is None:
        return local_readings(wall, zone)[0]
    reason, meant = unclear
    raise CalendarCallRefusedError(_readings_choice(call, field, zone, reason, meant))


def _readings_choice(
    call: JsonObject, field: str, zone: tzinfo, reason: str, meant: list[tuple[datetime, str]]
) -> str:
    calls = [
        f"{render_call(call, **{field: moment.astimezone(zone).isoformat(timespec='minutes')})} "
        f"({label})"
        for moment, label in meant
    ]
    return choice(f"{reason} Send the one that is meant:", calls)


def _repeating_in_server_time(
    call: JsonObject,
    moments: dict[str, datetime],
    name: str,
    zone: tzinfo,
    server: ZoneInfo,
    now: datetime,
) -> dict[str, str]:
    """Server wall-clock times for a repeating event, when they match every occurrence.

    Every occurrence keeps the wall-clock time as written, even when the first one
    falls where the clocks skip it, so the written time is shifted, not the instant.
    """
    written: dict[str, datetime] = {}
    walls: dict[str, datetime] = {}
    for field, moment in moments.items():
        parsed = parse_local(str(call[field]))
        assert parsed is not None
        written[field] = parsed.replace(tzinfo=None)
        walls[field] = written[field] + offset(server, moment) - offset(zone, moment)
    moved = [field for field in moments if walls[field].date() != written[field].date()]
    converted = {field: wall.isoformat(timespec="minutes") for field, wall in walls.items()}
    if not moved and len(server_shifts(zone, server, now)) == 1:
        return converted
    reason = (
        "falls on another day there"
        if moved
        else f"differs from {name} by an offset that changes during the year"
    )
    texts = " and ".join(str(call[field]).strip() for field in moments)
    raise CalendarCallRefusedError(
        refusal(
            f"a repeating event keeps the same wall-clock time in the server time zone {server}, "
            f"which {reason}, so no server time matches {name} time on every occurrence. This "
            f"call uses the server time of {texts} {name} time at the first occurrence.",
            call,
            timezone=OMIT,
            **converted,
        )
    )


def apply_end(
    arguments: JsonObject, server: ZoneInfo, *, stored_start: str | None, recurring: bool
) -> JsonObject:
    """Turn an end into a duration: minutes for timed events, days for all-day ones."""
    result = dict(arguments)
    end = result.pop(END_FIELD, None)
    if not isinstance(end, str):
        return result
    start = result.get("start")
    if not isinstance(start, str):
        start = stored_start
    if not isinstance(start, str):
        raise CalendarCallRefusedError(
            refusal('"end" needs a start to measure from.', result, start=STAND_INS["start"])
        )
    duration = _duration_between(server, start, end.strip(), recurring, result)
    if "duration" in result and result["duration"] != duration:
        raise CalendarCallRefusedError(
            choice(
                f'"end" gives a duration of {duration}, but "duration" is {result["duration"]}. '
                "Send the one that is meant:",
                [
                    render_call(result, duration=duration),
                    render_call(result, duration=result["duration"]),
                ],
            )
        )
    result["duration"] = duration
    return result


def _duration_between(
    server: ZoneInfo, start: str, end: str, recurring: bool, arguments: JsonObject
) -> int:
    if is_date(start):
        if not is_date(end):
            raise CalendarCallRefusedError(
                refusal(
                    "an all-day event ends on a date; send its length in days as duration.",
                    arguments,
                    duration=STAND_INS["duration"],
                )
            )
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if days < 0:
            raise CalendarCallRefusedError(
                refusal('"end" is before "start".', arguments, duration=STAND_INS["duration"])
            )
        if days == 0:
            return 1
        raise CalendarCallRefusedError(
            choice(
                f'"end" {end} can be the last day or the day after the event. Send the one that '
                "is meant:",
                [
                    render_call(arguments, duration=days + 1) + f" (through {end})",
                    render_call(arguments, duration=days) + f" (ending before {end})",
                ],
            )
        )
    begin = parse_local(start)
    if begin is None:
        # The service explains a malformed start.
        return 60
    if is_time_of_day(end):
        finish = datetime.combine(begin.date(), time.fromisoformat(end), begin.tzinfo)
        if finish <= begin:
            # An end time before the start time can only be on the next day.
            finish += timedelta(days=1)
    else:
        parsed_end = parse_local(end)
        if parsed_end is None:
            raise CalendarCallRefusedError(
                refusal(
                    f'"end" {end} must be a local time such as 2030-01-10T16:00 for a timed event.',
                    arguments,
                    duration=STAND_INS["duration"],
                )
            )
        finish = parsed_end
    if recurring:
        # Repeating events keep wall-clock lengths.
        first = begin.astimezone(server).replace(tzinfo=None) if begin.tzinfo else begin
        last = finish.astimezone(server).replace(tzinfo=None) if finish.tzinfo else finish
        seconds = (last - first).total_seconds()
    else:
        first_moment = begin if begin.tzinfo else begin.replace(tzinfo=server)
        last_moment = finish if finish.tzinfo else finish.replace(tzinfo=server)
        seconds = (last_moment - first_moment).total_seconds()
    if seconds <= 0 or seconds % 60:
        raise CalendarCallRefusedError(
            refusal(
                f'"end" {end} must come after "start" by whole minutes.',
                arguments,
                duration=STAND_INS["duration"],
            )
        )
    return int(seconds // 60)


def server_zone(calendar_service: CalendarService) -> ZoneInfo:
    """The server time zone every event and window is kept in."""
    return ZoneInfo(calendar_service.system_timezone_name())


def unknown_zone(name: str, server: ZoneInfo, arguments: JsonObject) -> str:
    """The refusal for a ``timezone`` that names no time zone."""
    return refusal(
        f'"timezone" "{name}" is not a known time zone. Use an IANA name such as '
        f'"Europe/Berlin", or omit it for the server time zone {server}.',
        arguments,
        timezone=OMIT,
    )


def window_text(start: datetime, end: datetime, zone: ZoneInfo) -> str:
    """Render a window as dates when it spans whole days, else as local times."""
    first, last = start.astimezone(zone), end.astimezone(zone)
    if first.time() == time.min and last.time() == time.min:
        final = (last - timedelta(days=1)).date()
        if final == first.date():
            return first.date().isoformat()
        return f"{first.date().isoformat()} to {final.isoformat()}"
    return f"{local_text(start, zone)} to {local_text(end, zone)}"


def local_text(value: datetime, zone: ZoneInfo) -> str:
    """Render an aware moment as a naive local time to the minute."""
    return value.astimezone(zone).replace(tzinfo=None).isoformat(timespec="minutes")


def minute_text(value: str) -> str:
    """Drop zero seconds from a naive ISO time: 2030-01-10T15:00:00 -> 2030-01-10T15:00."""
    return value[:-3] if len(value) == 19 and value.endswith(":00") else value


def length_text(minutes: int) -> str:
    """Render minutes as days, hours and minutes: 1500 -> 1d 1h."""
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins else ""]
    return " ".join(part for part in parts if part) or "0m"


__all__ = [
    "apply_end",
    "apply_timezone",
    "length_text",
    "local_text",
    "minute_text",
    "named_instant",
    "read_window",
    "server_zone",
    "unknown_zone",
    "window_text",
]
