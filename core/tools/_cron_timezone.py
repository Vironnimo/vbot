"""Read the time zone a ``cron`` call names against the server time zone jobs run in.

Jobs store no time zone of their own: cron fields and local timestamps are
server time. A call that names its own zone still executes when the reading is
exact: the zone is the server's, the schedule is relative, a local timestamp
converts to one instant, or cron fields shift by an offset that stays the same
all year. Otherwise the call is refused before any side effect, naming the
server zone and the corrected call.
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
    named_zone,
    offset,
    same_zone,
    server_shifts,
    server_text,
)

_CRON_STAND_IN = "<five cron fields in server time>"


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
    if parsed.tzinfo is None:
        converted = server_text(parsed.replace(tzinfo=zone), server)
        arguments["schedule"] = converted
        return (
            arguments,
            f'Read "{text}" as {name} time: {converted} in the server time zone {server}.',
        )
    as_zone = parsed.replace(tzinfo=zone)
    if as_zone.utcoffset() == parsed.utcoffset():
        return arguments, None
    wall = parsed.replace(tzinfo=None).isoformat()
    as_written = render_call(arguments, schedule=text)
    zone_reading = render_call(arguments, schedule=server_text(as_zone, server))
    raise CronCallRefusedError(
        f'cron was not run: "{text}" carries an offset that is not {name} time. Send the one '
        f"that is meant: {as_written} (the time as written) or {zone_reading} ({wall} in {name})."
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
    # Exact when every offset the year brings yields the same fields, e.g. "*/15 * * * *".
    conversions = {_shift_cron(schedule.split(), _minutes(shift)) for shift in shifts}
    if converted is not None and conversions == {converted}:
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
    if converted is not None:
        raise CronCallRefusedError(
            refusal(
                f"{base} The offset between the two zones changes during the year, so no cron "
                f'expression matches "{schedule}" there exactly; "{converted}" matches it today '
                "and differs for part of the year.",
                arguments,
                schedule=converted,
                timezone=OMIT,
            )
        )
    raise CronCallRefusedError(
        refusal(
            f"{base} Convert the fields to server time and omit timezone.",
            arguments,
            schedule=_CRON_STAND_IN,
            timezone=OMIT,
        )
    )


def _shift_cron(fields: list[str], shift: int) -> str | None:
    """Shift plain minute and hour fields by ``shift`` minutes, or return None."""
    minute, hour, day, month, weekday = fields
    shift_hours, shift_minutes = divmod(shift, 60)
    carry = 0
    if minute.isdigit():
        carry, new_minute = divmod(int(minute) + shift_minutes, 60)
        minute = str(new_minute)
    elif shift_minutes:
        return None
    if hour == "*":
        return " ".join((minute, hour, day, month, weekday))
    parts: list[str] = []
    moved_day = False
    for part in hour.split(","):
        start_text, _dash, end_text = part.partition("-")
        if not start_text.isdigit() or (end_text and not end_text.isdigit()):
            return None
        start = int(start_text) + shift_hours + carry
        end = int(end_text) + shift_hours + carry if end_text else start
        moved_day = moved_day or start // 24 != 0 or end // 24 != 0
        if start // 24 == end // 24:
            parts.append(f"{start % 24}" if start == end else f"{start % 24}-{end % 24}")
        else:
            parts.extend((f"{start % 24}-23", f"0-{end % 24}"))
    if moved_day and (day, month, weekday) != ("*", "*", "*"):
        return None
    return " ".join((minute, ",".join(parts), day, month, weekday))


def _minutes(shift: timedelta) -> int:
    return int(shift.total_seconds() // 60)


__all__ = ["zoned_schedule"]
