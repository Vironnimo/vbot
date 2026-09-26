"""Read the time zone a ``cron`` call names against the server time zone jobs run in.

Jobs store no time zone of their own: cron fields and local timestamps are
server time, and the scheduler steps server wall-clock time (a fire the clocks
skip moves past the gap, a repeated fire happens only the first time). A call
that names its own zone still executes when the reading is exact: the zone is
the server's, the schedule is relative or absolute, a local timestamp names one
instant there, or cron fields shift by an offset that stays the same all year,
so every fire lands on the instant the named zone means. Otherwise the call is
refused before any side effect, naming the server zone and the corrected call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from core.tools._cron_arguments import (
    OMIT,
    TIMEZONE_FIELD,
    CronCallRefusedError,
    refusal,
    render_call,
    schedule_kind,
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

_CRON_STAND_IN = "<five cron fields in server time>"
_WEEKDAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def zoned_schedule(
    arguments: dict[str, Any], server: ZoneInfo, now: datetime
) -> tuple[dict[str, Any], str | None]:
    """Return the arguments without ``timezone``, with the schedule in server time, and a note."""
    result = dict(arguments)
    name = result.pop(TIMEZONE_FIELD, None)
    if not isinstance(name, str):
        return result, None
    zone = named_zone(name)
    schedule = result.get("schedule")
    if not isinstance(schedule, str) or schedule.startswith(("in ", "every ")):
        if (
            not isinstance(schedule, str)
            and result.get("action") == "update"
            and zone is not None
            and not same_zone(zone, server, now)
        ):
            raise CronCallRefusedError(
                refusal(
                    f"jobs use the server time zone {server} and cannot keep {name}. To change "
                    "when the job fires, send schedule in server time.",
                    result,
                    schedule=_CRON_STAND_IN,
                )
            )
        return result, None
    if zone is None:
        raise CronCallRefusedError(
            refusal(
                f'"timezone" "{name}" is not a known time zone. Use an IANA name such as '
                f'"Europe/Berlin", or omit it for the server time zone {server}.',
                result,
            )
        )
    if same_zone(zone, server, now):
        return result, None
    if schedule_kind(schedule) == "cron":
        return _cron_in_zone(result, schedule, name, zone, server, now)
    return _moment_in_zone(result, schedule, name, zone, server)


def _moment_in_zone(
    arguments: dict[str, Any], schedule: str, name: str, zone: tzinfo, server: ZoneInfo
) -> tuple[dict[str, Any], str | None]:
    text = schedule.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        # The schedule parser explains the form; a time zone cannot fix it.
        return arguments, None
    if parsed.tzinfo is not None and offset_matches(parsed, zone):
        # An offset the zone has at that time names the instant exactly.
        return arguments, None
    wall = parsed.replace(tzinfo=None)
    unclear = unclear_local_time(wall, zone, name)
    if parsed.tzinfo is None and unclear is None:
        converted = server_text(local_readings(wall, zone)[0], server)
        arguments["schedule"] = converted
        return (
            arguments,
            f'Read "{text}" as {name} time: {converted} in the server time zone {server}.',
        )
    if unclear is None:
        readings = [(local_readings(wall, zone)[0], f"{wall.isoformat()} in {name}")]
        reason = f'"{text}" carries an offset that is not {name} time.'
    else:
        reason, readings = unclear
    calls = [
        f"{render_call(arguments, schedule=server_text(moment, server))} ({label})"
        for moment, label in readings
    ]
    if parsed.tzinfo is not None:
        calls.insert(0, f"{render_call(arguments, schedule=text)} (the time as written)")
        if unclear is not None:
            reason = f'"{text}" carries an offset that is not {name} time, and {reason}'
    raise CronCallRefusedError(
        f"cron was not run: {reason} Send the one that is meant: {' or '.join(calls)}."
    )


def _cron_in_zone(
    arguments: dict[str, Any],
    schedule: str,
    name: str,
    zone: tzinfo,
    server: ZoneInfo,
    now: datetime,
) -> tuple[dict[str, Any], str | None]:
    shifts = server_shifts(zone, server, now)
    current = offset(server, now) - offset(zone, now)
    converted = _shift_cron(schedule.split(), _minutes(current))
    if converted is not None and len(shifts) == 1:
        # One offset all year: every fire moves by it, gaps and repeats included.
        if converted == schedule:
            return arguments, None
        arguments["schedule"] = converted
        return arguments, (
            f'Read "{schedule}" as {name} time: "{converted}" in the server time zone {server}.'
        )
    base = (
        f"jobs use the server time zone {server}, so cron fields are read as server time, not "
        f"{name}."
    )
    if converted is None:
        raise CronCallRefusedError(
            refusal(
                f"{base} Convert the fields to server time and omit timezone.",
                arguments,
                schedule=_CRON_STAND_IN,
                timezone=OMIT,
            )
        )
    if converted == schedule:
        text = (
            f"{base} The two zones change their clocks on different dates, so the same fields "
            f"fire at the moments {name} time means except around those changes."
        )
    else:
        text = (
            f"{base} The offset between the two zones changes during the year, so no cron "
            f'expression matches "{schedule}" there exactly; "{converted}" matches it today '
            "and differs for part of the year."
        )
    raise CronCallRefusedError(refusal(text, arguments, schedule=converted, timezone=OMIT))


def _shift_cron(fields: list[str], shift: int) -> str | None:
    """Shift cron fields by ``shift`` minutes, or return None when no expression matches."""
    minute, hour, day, month, weekday = fields
    shift_hours, shift_minutes = divmod(shift, 60)
    carry = 0
    if minute.isdigit():
        carry, new_minute = divmod(int(minute) + shift_minutes, 60)
        minute = str(new_minute)
    elif shift_minutes:
        return None
    hours = shift_hours + carry
    restricted = (day, month, weekday) != ("*", "*", "*")
    if hour == "*":
        # Some fire crosses midnight whenever the hours move; restricted days cannot follow.
        return None if restricted and hours else " ".join((minute, hour, day, month, weekday))
    parts: list[str] = []
    day_moves: set[int] = set()
    for part in hour.split(","):
        start_text, _dash, end_text = part.partition("-")
        if not start_text.isdigit() or (end_text and not end_text.isdigit()):
            return None
        start = int(start_text) + hours
        end = int(end_text or start_text) + hours
        if start // 24 == end // 24:
            day_moves.add(start // 24)
            parts.append(_hour_range(start % 24, end % 24))
        else:
            day_moves.update((start // 24, end // 24))
            parts.extend((_hour_range(start % 24, 23), _hour_range(0, end % 24)))
    if day_moves != {0} and restricted:
        # Every fire moves to the same other day: only a weekday can follow it exactly.
        if len(day_moves) != 1 or (day, month) != ("*", "*"):
            return None
        shifted = _shift_weekdays(weekday, day_moves.pop())
        if shifted is None:
            return None
        weekday = shifted
    return " ".join((minute, ",".join(parts), day, month, weekday))


def _hour_range(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}-{last}"


def _shift_weekdays(field: str, days: int) -> str | None:
    """Move a weekday field by ``days``; None when the field is not plain weekdays."""
    weekdays: set[int] = set()
    for part in field.casefold().split(","):
        body, slash, step_text = part.partition("/")
        step = int(step_text) if step_text.isdigit() and int(step_text) > 0 else 0
        if slash and not step:
            return None
        if body == "*":
            first, last = 0, 6
        else:
            start_text, dash, end_text = body.partition("-")
            first_day = _weekday_number(start_text)
            last_day = _weekday_number(end_text) if dash else first_day
            if first_day is None or last_day is None or (slash and not dash):
                return None
            first, last = first_day, last_day
            if last == 0 and first > 0:
                last = 7  # "fri-sun": Sunday closes the range.
            if last < first:
                return None
        weekdays.update(day % 7 for day in range(first, last + 1, step or 1))
    moved = sorted((day + days) % 7 for day in weekdays)
    return _weekday_text(moved)


def _weekday_number(text: str) -> int | None:
    """0-7 as written (0 and 7 are Sunday) or a three-letter day name."""
    if text.isdigit():
        return int(text) if int(text) <= 7 else None
    return _WEEKDAY_NAMES.get(text)


def _weekday_text(days: list[int]) -> str:
    if len(days) == 7:
        return "*"
    runs: list[list[int]] = []
    for day in days:
        if runs and day == runs[-1][-1] + 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    return ",".join(
        f"{run[0]}-{run[-1]}" if len(run) > 2 else ",".join(map(str, run)) for run in runs
    )


def _minutes(shift: timedelta) -> int:
    return int(shift.total_seconds() // 60)


__all__ = ["zoned_schedule"]
