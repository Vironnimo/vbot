"""Read cron schedule dialects and calendar-event offsets without changing intent."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.tools._cron_call import TIMEZONE_FIELD, _Problems, _set_field
from core.tools._durations import (
    duration_from_seconds,
    duration_minutes,
    duration_text,
)
from core.tools.call_syntax import (
    is_placeholder,
    spelling,
)

_AGENT_TURN_KINDS = frozenset({"agentturn", "agent", "prompt", "message"})

# Schedule spellings by what their value means.
_CRON_KEYS = frozenset({"cron", "cronexpression", "expression", "expr", "crontab"})
_AT_KEYS = frozenset(
    {
        "runat",
        "at",
        "atms",
        "fireat",
        "datetime",
        "when",
        "startat",
        "scheduledat",
        "scheduledtime",
        "triggerat",
        "timestamp",
    }
)
_EVERY_KEYS = frozenset(
    {
        "every",
        "everyms",
        "everyseconds",
        "everyminutes",
        "interval",
        "intervalms",
        "intervalseconds",
        "intervalminutes",
        "intervalhours",
    }
)
_IN_KEYS = frozenset(
    {"in", "delay", "delayms", "delayseconds", "delayminutes", "inseconds", "inminutes", "after"}
)
_TYPE_KEYS = frozenset({"scheduletype", "schedulekind", "kind", "type"})
WEBHOOK_SCHEDULE = "webhook"
"""The ``schedule`` of a job that runs when a request reaches its webhook URL."""
_WEBHOOK_WORDS = frozenset({"webhook", "webhooks", "hook", "hooks", "onwebhook"})
_TYPE_WORDS = {
    "cron": frozenset({"cron", "crontab", "cronexpression", "expression", "expr"}),
    "once": frozenset(
        {"once", "onetime", "oneshot", "single", "at", "date", "datetime", "runat", "fireat"}
    ),
    "interval": frozenset({"interval", "every", "recurring", "repeating", "periodic"}),
    WEBHOOK_SCHEDULE: _WEBHOOK_WORDS,
}
_UNIT_BY_KEY_SUFFIX = (("ms", 0.001), ("seconds", 1), ("minutes", 60), ("hours", 3600))


_CRON_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLOCK_SCHEDULE = re.compile(r"^at ([01]\d|2[0-3]):([0-5]\d)$")
"""A clock time without a date; the handler reads it as its next occurrence."""
_CLOCK_TEXT = re.compile(r"^(?:at\s*)?(?:(\d{1,2}):(\d{2})|(\d{2})(\d{2}))$")
# Epochs this far from 1970 (2001 to 2286) are the only numbers read as instants.
_EPOCH_SECONDS = (1_000_000_000, 10_000_000_000)
_EPOCH_MILLISECONDS = (1_000_000_000_000, 10_000_000_000_000)
_MOMENT_STAND_IN = "<local time such as 2030-01-01T09:00>"
EVENT_TIME_STAND_IN = "<start or end, e.g. start - 30m>"
EVENT_REPEAT_REFUSAL = (
    "a job at a calendar event runs at every occurrence of the event, so it takes no repeat. "
    "Nothing was changed."
)
# Offsets from an event edge under their own names: the edge and the direction they count.
_EVENT_OFFSET_KEYS = {
    "minutesbefore": ("start", "-"),
    "beforeminutes": ("start", "-"),
    "reminderminutes": ("start", "-"),
    "remindminutesbefore": ("start", "-"),
    "leadminutes": ("start", "-"),
    "beforestart": ("start", "-"),
    "minutesbeforestart": ("start", "-"),
    "afterstart": ("start", "+"),
    "minutesafterstart": ("start", "+"),
    "beforeend": ("end", "-"),
    "minutesbeforeend": ("end", "-"),
    "afterend": ("end", "+"),
    "minutesafterend": ("end", "+"),
}
_EVENT_TIME_KEYS = frozenset({"schedule", "offset", "eventtime", "trigger", "edge", "time"})
_EDGE_KEYS = frozenset({"edge", "eventedge"})
_SIGNED_OFFSET_KEYS = frozenset({"offset", "eventoffsetminutes", "offsetminutes"})
_EVENT_EDGE = re.compile(r"^(start|end)\s*(?:([+-])\s*(.+))?$")
_EVENT_PHRASE = re.compile(
    r"^(?:(.+?)\s+)?(before|after|at)(?:\s+(?:the\s+)?(start|end|beginning|event))?$"
)


def schedule_kind(schedule: str) -> str:
    """Classify a canonical schedule string: ``once``, ``interval``, ``cron`` or ``webhook``."""
    text = schedule.strip()
    if text == WEBHOOK_SCHEDULE:
        return WEBHOOK_SCHEDULE
    if text.startswith("every "):
        return "interval"
    if text.startswith("in "):
        return "once"
    if len(text.split()) == 5:
        return "cron"
    return "once"


def _read_schedule(arguments: dict[str, Any], problems: _Problems) -> None:
    """Resolve every schedule spelling into one canonical ``schedule`` string."""
    type_hint = _schedule_type(arguments, problems)
    candidates: list[tuple[str, str]] = []
    # Epochs name instants in every zone, so a time zone next to one changes nothing.
    absolute: set[str] = set()
    schedule = arguments.pop("schedule", None)
    if isinstance(schedule, dict):
        type_hint = _schedule_object(arguments, schedule, type_hint, candidates, problems, absolute)
    elif schedule is not None and not is_placeholder(schedule):
        candidates.append(("schedule", _schedule_text(schedule, type_hint, "schedule", problems)))
    for key in list(arguments):
        word = spelling(key)
        if word in _CRON_KEYS | _AT_KEYS | _EVERY_KEYS | _IN_KEYS:
            item = arguments.pop(key)
            if is_placeholder(item):
                continue
            text = _keyed_schedule(word, key, item, problems, absolute)
            if text is not None:
                candidates.append((key, text))
    texts = list(dict.fromkeys(text for _key, text in candidates if text))
    if not texts:
        if type_hint == WEBHOOK_SCHEDULE:
            # A webhook job keeps no time: its type alone is its schedule.
            arguments["schedule"] = WEBHOOK_SCHEDULE
        return
    if len(texts) > 1 and type_hint is not None:
        typed = list(dict.fromkeys(text for text in texts if schedule_kind(text) == type_hint))
        if len(typed) == 1:
            texts = typed
    if len(texts) > 1:
        problems.choose(
            "it names more than one schedule ("
            + ", ".join(f'{key} "{text}"' for key, text in candidates)
            + "); send only the one that is meant:",
            [{"schedule": text} for text in texts],
        )
        arguments["schedule"] = texts[0]
        return
    text = texts[0]
    if type_hint is not None and schedule_kind(text) != type_hint:
        _schedule_type_mismatch(text, type_hint, problems)
    arguments["schedule"] = text
    if text in absolute:
        arguments.pop(TIMEZONE_FIELD, None)


def _schedule_type(arguments: dict[str, Any], problems: _Problems) -> str | None:
    hints: list[str] = []
    for key in [key for key in arguments if spelling(key) in _TYPE_KEYS]:
        item = arguments.pop(key)
        if is_placeholder(item):
            continue
        word = spelling(str(item))
        kind = next((kind for kind, words in _TYPE_WORDS.items() if word in words), None)
        if kind is None and word in _AGENT_TURN_KINDS:
            # OpenClaw spells the payload kind at the top level too.
            continue
        if kind is None:
            problems.add(
                f'"{key}" "{item}" is not a schedule type; the schedule string alone decides it.'
            )
            continue
        hints.append(kind)
    if len(set(hints)) > 1:
        problems.add("it names conflicting schedule types; the schedule string alone decides it.")
        return None
    return hints[0] if hints else None


def _schedule_object(
    arguments: dict[str, Any],
    schedule: dict[str, Any],
    type_hint: str | None,
    candidates: list[tuple[str, str]],
    problems: _Problems,
    absolute: set[str],
) -> str | None:
    """Read an OpenClaw-style schedule object such as ``{"kind":"cron","expr":"0 8 * * *"}``."""
    inner = dict(schedule)
    object_hint = _schedule_type(inner, problems)
    if object_hint is not None:
        if type_hint is not None and type_hint != object_hint:
            problems.add(
                "it names conflicting schedule types; the schedule string alone decides it."
            )
        type_hint = object_hint
    for key in list(inner):
        word = spelling(key)
        item = inner.pop(key)
        if is_placeholder(item):
            continue
        if word in {"tz", "timezone", "timezonename"}:
            _set_field(arguments, TIMEZONE_FIELD, item, problems)
        elif word in {"stagger", "staggerms", "exact"}:
            continue
        elif word in _CRON_KEYS | _AT_KEYS | _EVERY_KEYS | _IN_KEYS:
            text = _keyed_schedule(word, key, item, problems, absolute)
            if text is not None:
                candidates.append((key, text))
        elif word == "schedule" or word == "value":
            candidates.append((key, _schedule_text(item, type_hint, key, problems)))
        else:
            problems.add(f'schedule field "{key}" is not available; send schedule as one string.')
    return type_hint


def _keyed_schedule(
    word: str, key: str, item: Any, problems: _Problems, absolute: set[str]
) -> str | None:
    if word in _CRON_KEYS:
        return _schedule_text(item, "cron", key, problems)
    if word in _AT_KEYS:
        return _moment_text(key, item, problems, absolute, milliseconds=word.endswith("ms"))
    prefix = "every" if word in _EVERY_KEYS else "in"
    if isinstance(item, int | float) and not isinstance(item, bool):
        unit = next((size for suffix, size in _UNIT_BY_KEY_SUFFIX if word.endswith(suffix)), None)
        if unit is None:
            problems.add(
                f'"{key}" {item} has no unit; write it like "{prefix} 30m" or "{prefix} 2h".'
            )
            return None
        duration = duration_from_seconds(item * unit)
        if duration is None:
            problems.add(f'"{key}" {item} is not a whole number of minutes.')
            return None
        return f"{prefix} {duration}"
    return _schedule_text(item, "interval" if prefix == "every" else "once", key, problems)


def _schedule_text(value: Any, hint: str | None, key: str, problems: _Problems) -> str:
    """Return the canonical schedule string for one supplied schedule value."""
    text = " ".join(str(value).split())
    lowered = text.casefold()
    if spelling(text) in _WEBHOOK_WORDS:
        return WEBHOOK_SCHEDULE
    if lowered in _CRON_MACROS:
        return _CRON_MACROS[lowered]
    if lowered.startswith("at "):
        clock = _clock_text(lowered)
        if clock is not None:
            return clock
    prefix, _space, rest = lowered.partition(" ")
    if prefix in {"in", "every"} and rest:
        duration = duration_text(rest)
        return f"{prefix} {duration}" if duration else text
    duration = duration_text(lowered)
    if duration is not None:
        if hint == "once":
            return f"in {duration}"
        if hint == "interval":
            return f"every {duration}"
        problems.choose(
            f'"{key}" "{text}" can mean one fire after that time or a repeat at that interval:',
            [{"schedule": f"in {duration}"}, {"schedule": f"every {duration}"}],
        )
        return f"in {duration}"
    if _DATE_ONLY.match(text):
        problems.add(
            f'"{key}" "{text}" has no time of day; add one, as in "{text}T09:00".',
            schedule=f"<{text} with a time of day, as {text}THH:MM>",
        )
        return text
    fields = text.split()
    if len(fields) in {6, 7} and "T" not in text:
        problems.add(
            f'"{key}" "{text}" has {len(fields)} fields; cron here takes exactly five '
            "(minute hour day-of-month month day-of-week), without seconds or years.",
            schedule="<five cron fields: minute hour day-of-month month day-of-week>",
        )
    return text


def _schedule_type_mismatch(text: str, type_hint: str, problems: _Problems) -> None:
    kind = schedule_kind(text)
    if kind == "cron" and type_hint == "once":
        problems.choose(
            f'the schedule "{text}" repeats, but the call asks for a one-time job:',
            [{"schedule": text}, {"schedule": text, "repeat": 1}],
        )
    else:
        problems.add(
            f'the schedule "{text}" is not a {type_hint} schedule; the schedule string alone '
            "decides the type."
        )


def _moment_text(
    key: str, item: Any, problems: _Problems, absolute: set[str], *, milliseconds: bool
) -> str | None:
    """Read a one-time value: a local time, a clock time, digits, or an epoch number."""
    if isinstance(item, int | float) and not isinstance(item, bool):
        return _epoch_text(key, item, problems, absolute, milliseconds=milliseconds)
    if not isinstance(item, str):
        return _schedule_text(item, "once", key, problems)
    text = item.strip()
    clock = _clock_text(text)
    if clock is not None:
        return clock
    if not text.isdecimal():
        return _schedule_text(item, "once", key, problems)
    if len(text) in {10, 13}:
        return _epoch_text(key, int(text), problems, absolute, milliseconds=milliseconds)
    stamp = None
    if len(text) in {8, 12, 14}:
        # A compact local date, or date and time: 20300101, 203001010900, 20300101090000.
        shape = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}[len(text)]
        try:
            stamp = datetime.strptime(text, shape)
        except ValueError:
            stamp = None
    if stamp is not None and len(text) == 8:
        day = stamp.date().isoformat()
        problems.add(
            f'"{key}" "{text}" is a date without a time of day; add one, as in "{day}T09:00".',
            schedule=f"<{day} with a time of day, as {day}THH:MM>",
        )
        return None
    if stamp is not None:
        return stamp.isoformat(timespec="minutes" if len(text) == 12 else "seconds")
    problems.add(
        f'"{key}" "{text}" does not read as one time. Send a local time such as '
        '"2030-01-01T09:00", a clock time such as "09:00", or a delay such as "in 30m".',
        schedule=_MOMENT_STAND_IN,
    )
    return None


def _clock_text(text: str) -> str | None:
    """``at HH:MM`` for a clock time written "09:00", "9:00", "0900" or "at 9:00"."""
    match = _CLOCK_TEXT.match(text.strip().casefold())
    if match is None:
        return None
    hour_text, minute_text, *compact = match.groups()
    if hour_text is None:
        hour_text, minute_text = compact
    hour, minute = int(hour_text), int(minute_text)
    if hour > 23 or minute > 59:
        return None
    return f"at {hour:02d}:{minute:02d}"


def _epoch_text(
    key: str, value: float, problems: _Problems, absolute: set[str], *, milliseconds: bool
) -> str | None:
    """An epoch as a UTC instant, when its size leaves no doubt about the unit."""
    low, high = _EPOCH_MILLISECONDS
    if low <= value < high:
        seconds = value / 1000
    elif not milliseconds and _EPOCH_SECONDS[0] <= value < _EPOCH_SECONDS[1]:
        seconds = value
    else:
        unit = "milliseconds" if milliseconds else "seconds or milliseconds"
        problems.add(
            f'"{key}" {_json_value(value)} is not an epoch time in {unit} after 2001. Send a '
            'local time such as "2030-01-01T09:00", or a delay such as "in 30m".',
            schedule=_MOMENT_STAND_IN,
        )
        return None
    text = datetime.fromtimestamp(seconds, UTC).isoformat()
    absolute.add(text)
    return text


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _names_event_offset(arguments: Mapping[str, Any]) -> bool:
    words = {spelling(key) for key in arguments}
    return bool(words & _EVENT_OFFSET_KEYS.keys()) or bool(
        words & _EDGE_KEYS and words & _SIGNED_OFFSET_KEYS
    )


def _read_event_schedule(
    arguments: dict[str, Any], problems: _Problems, *, bound: bool = True
) -> None:
    """Resolve the event time of a job bound to a calendar event into ``schedule``."""
    candidates: list[tuple[str, Any, list[str]]] = []
    edges = [key for key in arguments if spelling(key) in _EDGE_KEYS]
    offsets = [key for key in arguments if spelling(key) in _SIGNED_OFFSET_KEYS]
    if len(edges) == 1 and len(offsets) == 1:
        # vBot's stored form: an edge and a signed offset in minutes.
        edge, offset = arguments.pop(edges[0]), arguments.pop(offsets[0])
        minutes = duration_minutes(abs(offset)) if isinstance(offset, int) else None
        if edge in {"start", "end"} and (minutes is not None or offset == 0):
            sign = "-" if offset < 0 else "+"
            candidates.append((edges[0], edge, [_event_time(edge, sign, minutes or 0)]))
        else:
            problems.add(
                f"{edges[0]} {_json_value(edge)} with {offsets[0]} {_json_value(offset)} is not "
                "an event time.",
                schedule=EVENT_TIME_STAND_IN,
            )
    for key in list(arguments):
        word = spelling(key)
        if word in _EVENT_OFFSET_KEYS:
            edge, sign = _EVENT_OFFSET_KEYS[word]
            item = arguments.pop(key)
            minutes = duration_minutes(item)
            if minutes is None:
                problems.add(
                    f'"{key}" {_json_value(item)} is not a duration such as 30 (minutes) or "1h".',
                    schedule=EVENT_TIME_STAND_IN,
                )
                continue
            candidates.append((key, item, [_event_time(edge, sign, minutes)]))
        elif word in _EVENT_TIME_KEYS | _AT_KEYS | _IN_KEYS and not is_placeholder(arguments[key]):
            item = arguments.pop(key)
            candidates.append((key, item, event_time_readings(item)))
    readings = list(dict.fromkeys(text for _key, _item, found in candidates for text in found))
    offsets = [reading for reading in readings if reading not in {"start", "end"}]
    if len(offsets) == 1 and all(offsets[0].startswith(reading) for reading in readings):
        # A bare edge beside an offset from it, such as schedule "start" with minutes_before 15.
        readings = offsets
    if len(readings) > 1:
        if len(candidates) > 1:
            names = ", ".join(f"{key} {_json_value(item)}" for key, item, _found in candidates)
            text = f"it names more than one event time ({names}); send only the one that is meant:"
        else:
            key, item, _found = candidates[0]
            text = ambiguous_event_time(key, item, readings)
        problems.choose(text, [{"schedule": reading} for reading in readings])
    if readings:
        arguments["schedule"] = readings[0]
    elif candidates:
        # The handler explains an event time it cannot read.
        arguments["schedule"] = " ".join(str(candidates[0][1]).split())
    if not bound:
        return
    if arguments.pop("repeat", None) is not None:
        problems.add(EVENT_REPEAT_REFUSAL)
    arguments.pop(TIMEZONE_FIELD, None)


def ambiguous_event_time(key: str, value: Any, readings: list[str]) -> str:
    """Say what an event time with several ``readings`` leaves open, before the choices."""
    edges = {reading.split(" ", 1)[0] for reading in readings}
    if len(edges) > 1:
        return (
            f"{key} {_json_value(value)} does not say whether it counts from the event's start "
            "or end:"
        )
    return (
        f"{key} {_json_value(value)} does not say whether the job runs before or after the "
        f"event's {edges.pop()}:"
    )


def event_time_readings(value: Any) -> list[str]:
    """The canonical event times a value can mean, such as ``start - 30m``; [] when none.

    A value that does not say whether it counts before or after the event has two.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        if value == 0:
            return ["start"]
        minutes = duration_minutes(abs(value))
        if minutes is None:
            return []
        if value < 0:
            return [_event_time("start", "-", minutes)]
        return [_event_time("start", "-", minutes), _event_time("start", "+", minutes)]
    if not isinstance(value, str):
        return []
    text = " ".join(value.casefold().replace("_", " ").split())
    edge = _EVENT_EDGE.match(text)
    if edge is not None:
        anchor, sign, amount = edge.groups()
        minutes = duration_minutes(amount) if amount else 0
        return [] if minutes is None else [_event_time(anchor, sign or "+", minutes)]
    phrase = _EVENT_PHRASE.match(text)
    if phrase is not None:
        amount, relation, anchor = phrase.groups()
        anchor = {"beginning": "start", "event": None}.get(anchor or "", anchor)
        minutes = duration_minutes(amount) if amount else 0
        if minutes is None or (relation == "at") == bool(amount):
            # "at" takes no amount; before and after need one.
            return []
        if relation == "at":
            return [anchor or "start"]
        if anchor is not None or relation == "before":
            return [_event_time(anchor or "start", "-" if relation == "before" else "+", minutes)]
        return [_event_time("start", "+", minutes), _event_time("end", "+", minutes)]
    sign, rest = (text[0], text[1:].strip()) if text[:1] in {"+", "-"} else ("", text)
    minutes = duration_minutes(rest)
    if minutes is None:
        return []
    if sign == "-":
        return [_event_time("start", "-", minutes)]
    if sign == "+":
        return [_event_time("start", "+", minutes), _event_time("end", "+", minutes)]
    return [_event_time("start", "-", minutes), _event_time("start", "+", minutes)]


def _event_time(edge: str, sign: str, minutes: int) -> str:
    """The canonical event time ``minutes`` before or after an edge: ``start - 30m``."""
    return f"{edge} {sign} {duration_from_seconds(minutes * 60)}" if minutes else edge


# -- state, recurrence, Session, delivery --------------------------------------------------
