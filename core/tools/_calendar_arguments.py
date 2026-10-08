"""Read ``calendar`` calls written in vBot's schema or another calendar dialect.

Models describe events with the fields of the calendar APIs they know, mostly
Google Calendar v3 and the MCP servers built on it: ``summary``, ``start`` and
``end`` objects with ``dateTime``/``date``/``timeZone``, ``recurrence`` lists of
RRULE strings, ``eventId``, ``originalStartTime``, ``timeMin``/``timeMax`` and
``q``. This owner maps them onto the canonical fields so a call whose intent is
clear executes; readings with different effects fail before any side effect
with the corrected call.

Reminders and instructions to run at an event (``reminders``,
``minutes_before``, ``prompt``, ``add_action``) become the unadvertised
``reminder``, which the handler refuses with the ``cron`` call that does it:
only the handler knows whether that Tool is offered. A ``when`` phrase such as
``this week`` stays unadvertised for the handler, which resolves it against
the server clock and time zone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from core.calendar import occurrence_id, parse_occurrence_id
from core.tools._durations import duration_minutes
from core.tools._named_zones import named_zone
from core.tools.call_syntax import (
    SpellingAliases,
    is_placeholder,
    normalize_call_arguments,
    spelling,
)
from core.tools.contracts import ToolContract, ToolContractError

REFUSAL_PREFIX = "calendar was not run: "
WHEN_FIELD = "when"
REMINDER_FIELD = "reminder"
UNADVERTISED_PARAMETERS: dict[str, Any] = {
    WHEN_FIELD: {"type": "string", "minLength": 1},
    # The schedule and instruction of a requested reminder; the handler refuses it.
    REMINDER_FIELD: {"type": "object"},
}
OMIT = object()
"""``render_call`` override that removes a field from the rendered call."""

WINDOW_ACTIONS = frozenset({"list", "find_free_time"})
CHANGE_FIELDS = ("title", "start", "end", "description", "location", "rrule")
STAND_INS = {
    "id": "<event id from list>",
    "title": "<title>",
    "start": "<2030-01-10T15:00 or 2030-01-10>",
    "end": "<same form as start>",
    "rrule": "<rule such as FREQ=WEEKLY;BYDAY=MO>",
    "time_min": "<2030-01-10 or 2030-01-10T15:00>",
    "time_max": "<2030-01-10 or 2030-01-10T15:00>",
    "duration": "<minutes>",
    WHEN_FIELD: "this week",
}
"""Values only the Agent can supply, shown in a corrected call in their field's place."""
REMINDER_SCHEDULE_STAND_IN = "<start or end, e.g. start - 30m>"
REMINDER_PROMPT_STAND_IN = "<instruction>"


class CalendarCallRefusedError(ValueError):
    """A ``calendar`` call was refused before any side effect; the message names the fix."""


_CALL_ORDER = (
    "action",
    "id",
    "title",
    "start",
    "end",
    "rrule",
    "location",
    "description",
    "time_min",
    "time_max",
    "query",
    "duration",
    WHEN_FIELD,
)
_LONG_TEXT = 120
_REMINDER_ACTION = "add_reminder"
_TEMPLATE = re.compile(r"^\s*<[^<>]+>\s*$")

_FIELD_ALIASES = SpellingAliases(
    {
        "id": (
            "event_id",
            "uid",
            "calendar_event_id",
            "occurrence_id",
            "instance_id",
            "recurring_event_id",
        ),
        "title": ("summary", "name", "subject", "event_title", "event_name"),
        "description": ("notes", "note", "details", "body", "event_description"),
        "start": (
            "start_time",
            "start_at",
            "starts_at",
            "start_datetime",
            "start_date",
            "datetime",
            "date",
            "begin",
            "dtstart",
        ),
        "end": ("end_time", "end_at", "ends_at", "end_datetime", "end_date", "dtend"),
        "location": ("place", "venue", "where", "address"),
        "rrule": ("recurrence", "recurrence_rule", "rule", "repeat", "repeats"),
        "time_min": ("from", "since", "window_start", "range_start"),
        "time_max": ("to", "till", "window_end", "range_end"),
        "query": ("q", "search", "search_text", "keyword", "keywords"),
        "duration": ("length", "minutes", "duration_minutes", "length_minutes", "slot_duration"),
        WHEN_FIELD: ("window", "range", "period", "time_range", "date_range", "timeframe"),
    }
)
_ACTION_SYNONYMS = SpellingAliases(
    {
        "list": (
            "get",
            "show",
            "view",
            "read",
            "events",
            "list_events",
            "get_events",
            "get_event",
            "search",
            "search_events",
            "query",
            "agenda",
            "upcoming",
        ),
        "create": (
            "add",
            "new",
            "insert",
            "schedule",
            "make",
            "book",
            "create_event",
            "add_event",
            "insert_event",
        ),
        "update": (
            "edit",
            "modify",
            "change",
            "patch",
            "move",
            "reschedule",
            "rename",
            "update_event",
            "edit_event",
            "move_event",
        ),
        "delete": (
            "remove",
            "rm",
            "del",
            "cancel",
            "drop",
            "delete_event",
            "remove_event",
            "cancel_event",
        ),
        "find_free_time": (
            "find_free",
            "free",
            "free_time",
            "availability",
            "check_availability",
            "freebusy",
            "free_busy",
            "get_freebusy",
            "find_slot",
            "find_slots",
            "find_time",
        ),
        _REMINDER_ACTION: (
            "add_action",
            "create_action",
            "attach_action",
            "schedule_action",
            "set_reminder",
            "create_reminder",
            "remind",
        ),
    }
)

_WRAPPER_KEYS = frozenset({"event", "resource", "requestbody", "body", "data", "eventdata"})
_TIME_OBJECT_KEYS = frozenset(
    {"start", "end", "starttime", "endtime", "originalstarttime", "timemin", "timemax"}
)
_ORIGINAL_START_KEYS = frozenset(
    {"originalstart", "originalstarttime", "occurrencestart", "instancestart", "recurrenceid"}
)
_ZONE_KEYS = frozenset({"timezone", "tz", "zone", "timezonename"})
_RULE_KEYS = frozenset({"rrule", "recurrence", "recurrencerule", "rule", "repeat", "repeats"})
_REMINDER_KEYS = frozenset({"reminders", "reminder", "alarms", "alarm", "notifications"})
_BEFORE_KEYS = frozenset(
    {"minutesbefore", "beforeminutes", "reminderminutes", "remindminutesbefore", "leadminutes"}
)
_PROMPT_KEYS = frozenset({"prompt", "instruction", "instructions", "task", "actionprompt"})
_INERT_KEYS = frozenset(
    {
        "singleevents",
        "orderby",
        "maxresults",
        "limit",
        "sendupdates",
        "sendnotifications",
        "colorid",
        "visibility",
        "transparency",
        "guestscanmodify",
        "guestscaninviteothers",
        "guestscanseeotherguests",
        "showdeleted",
        "status",
        "kind",
    }
)
_CALENDAR_KEYS = frozenset({"calendarid", "calendar", "calendarname"})
_DEFAULT_CALENDARS = frozenset({"primary", "default", "main", "mine", "local"})
_ATTENDEE_KEYS = frozenset({"attendees", "guests", "invitees", "participants"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Problems:
    """Refusals collected while a call is read, reported with one corrected call."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.choice: tuple[str, list[dict[str, Any]]] | None = None

    def add(self, text: str) -> None:
        self.texts.append(text)

    def choose(self, text: str, alternatives: list[dict[str, Any]]) -> None:
        if self.choice is None:
            self.choice = (text, alternatives)
        else:
            self.texts.append(text)

    def raise_if_any(self, arguments: Mapping[str, Any]) -> None:
        if self.choice is not None:
            text, alternatives = self.choice
            calls = " or ".join(render_call(arguments, **overrides) for overrides in alternatives)
            raise ToolContractError(REFUSAL_PREFIX + " ".join([*self.texts, text, calls]))
        if self.texts:
            raise ToolContractError(refusal(" ".join(self.texts), arguments))


def normalize_calendar_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return the canonical ``calendar`` arguments for one Model call."""
    value = _json_object(arguments)
    if value is None:
        return normalize_call_arguments(contract, arguments)
    problems = _Problems()
    value = _lift_wrappers(value, problems)
    value = _read_time_objects(value, problems)
    zone_name, original_start = _take_zone(value), _take_original_start(value)
    reminder = _take_reminder(value)
    for key in [key for key in value if spelling(key) in _RULE_KEYS]:
        value[key] = _rule_text(value[key], problems)
    normalized = normalize_call_arguments(
        contract,
        value,
        enum_fields=("action",),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"action": _action_word},
        empty_as_omitted=("id", "title", "start", "end", "time_min", "time_max", "query"),
    )
    if not isinstance(normalized, dict):
        return normalized
    _read_extras(normalized, problems)
    _omit_placeholders(normalized)
    action = _read_action(normalized, reminder)
    if zone_name is not None:
        _apply_zone(normalized, zone_name, problems)
    if action in WINDOW_ACTIONS:
        _read_window(normalized, problems)
    else:
        _read_event(normalized, original_start, problems)
    if problems.texts or problems.choice:
        known = set(contract.input_schema["properties"])
        for key in normalized:
            if key not in known:
                problems.add(f'"{key}" is not a parameter.')
    problems.raise_if_any(normalized)
    return normalized


def render_call(arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Render a canonical ``calendar`` call as compact JSON; ``OMIT`` removes a field."""
    call = {**arguments, **overrides}
    ordered = {name: call[name] for name in _CALL_ORDER if name in call and call[name] is not OMIT}
    text = ordered.get("description")
    if isinstance(text, str) and len(text) > _LONG_TEXT:
        ordered["description"] = "<the description from this call>"
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def refusal(text: str, arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Return a refusal message that ends with the corrected call."""
    return f"{REFUSAL_PREFIX}{text} Send: {render_call(arguments, **overrides)}"


def length_text(minutes: int) -> str:
    """Render minutes in the largest exact unit: 90 -> 90m, 120 -> 2h, 1440 -> 1d."""
    for size, unit in ((1440, "d"), (60, "h")):
        if minutes % size == 0:
            return f"{minutes // size}{unit}"
    return f"{minutes}m"


# -- shapes ------------------------------------------------------------------------------


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return dict(value) if isinstance(value, dict) else None


def _lift_wrappers(arguments: dict[str, Any], problems: _Problems) -> dict[str, Any]:
    """Lift an ``event``/``resource``/``requestBody`` object into the call."""
    result = dict(arguments)
    for key in list(result):
        inner = _json_object(result[key]) if spelling(key) in _WRAPPER_KEYS else None
        if inner is None:
            continue
        del result[key]
        for name, item in inner.items():
            if name in result and result[name] != item:
                problems.add(f'"{name}" is given twice with different values; send one.')
            result[name] = item
    return result


def _read_time_objects(arguments: dict[str, Any], problems: _Problems) -> dict[str, Any]:
    """Unpack Google ``{"dateTime" | "date", "timeZone"}`` objects into their time text."""
    result = dict(arguments)
    for key, item in arguments.items():
        if spelling(key) not in _TIME_OBJECT_KEYS or not isinstance(item, dict):
            continue
        fields = {spelling(name): value for name, value in item.items()}
        moment, day, zone = fields.get("datetime"), fields.get("date"), fields.get("timezone")
        if isinstance(moment, str) and moment.strip():
            result[key] = _in_zone(moment.strip(), zone, problems)
        elif isinstance(day, str) and day.strip():
            result[key] = day.strip()
        else:
            problems.add(f'"{key}" needs a date or dateTime.')
    return result


def _in_zone(text: str, zone_name: Any, problems: _Problems) -> str:
    """A local date-time read in the named zone, as a time with that zone's offset."""
    if not isinstance(zone_name, str) or not zone_name.strip() or _DATE.match(text):
        return text
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    if moment.tzinfo is not None:
        return text
    zone = named_zone(zone_name)
    if zone is None:
        problems.add(f'"timeZone" "{zone_name}" is not a known time zone.')
        return text
    return moment.replace(tzinfo=zone).isoformat(timespec="minutes")


def _take_zone(arguments: dict[str, Any]) -> str | None:
    zones = [arguments.pop(key) for key in list(arguments) if spelling(key) in _ZONE_KEYS]
    return next((zone for zone in zones if isinstance(zone, str) and zone.strip()), None)


def _apply_zone(arguments: dict[str, Any], zone_name: str, problems: _Problems) -> None:
    """Read local date-times in a call's own time zone as exact times."""
    for name in ("start", "end", "time_min", "time_max"):
        item = arguments.get(name)
        if isinstance(item, str):
            arguments[name] = _in_zone(item.strip(), zone_name, problems)


def _take_original_start(arguments: dict[str, Any]) -> str | None:
    """The original start that names one occurrence of a series, as local wall-clock text."""
    found = [arguments.pop(key) for key in list(arguments) if spelling(key) in _ORIGINAL_START_KEYS]
    for item in found:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if _DATE.match(text):
            return text
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None).isoformat(timespec="minutes")
        except ValueError:
            continue
    return None


def _take_reminder(arguments: dict[str, Any]) -> dict[str, str] | None:
    """Take reminder and instruction fields out of the call; None when none asks for one."""
    asked = False
    schedule: str | None = None
    prompt: str | None = None
    for key in list(arguments):
        word = spelling(key)
        if word in _REMINDER_KEYS:
            item = arguments.pop(key)
            if _requests_reminder(item):
                asked = True
                schedule = schedule or _before_start(_reminder_minutes(item))
        elif word in _BEFORE_KEYS:
            asked = True
            schedule = schedule or _before_start(duration_minutes(arguments.pop(key)))
        elif word in _PROMPT_KEYS:
            item = arguments.pop(key)
            if isinstance(item, str) and item.strip() and not _TEMPLATE.match(item):
                asked, prompt = True, item.strip()
    if not asked:
        return None
    result = {"prompt": prompt or REMINDER_PROMPT_STAND_IN}
    if schedule is not None:
        result["schedule"] = schedule
    return result


def _requests_reminder(item: Any) -> bool:
    if item in (None, False, "", [], {}):
        return False
    if isinstance(item, dict):
        # Google's default reminders ask for nothing beyond the user's own calendar settings.
        uses_default = item.get("useDefault", item.get("use_default")) is True
        return not (uses_default and not item.get("overrides"))
    return True


def _reminder_minutes(item: Any) -> int | None:
    if isinstance(item, dict):
        overrides = item.get("overrides")
        if overrides is None:
            return duration_minutes(item.get("minutes"))
        item = overrides
    if isinstance(item, list) and item:
        return _reminder_minutes(item[0])
    return duration_minutes(item)


def _before_start(minutes: int | None) -> str | None:
    return None if minutes is None else f"start - {length_text(minutes)}"


def _rule_text(value: Any, problems: _Problems) -> Any:
    """Read ``RRULE:`` text, a one-rule list or a rule object as RRULE text; null is ""."""
    if value is None:
        return ""
    if isinstance(value, list):
        rules = [item for item in value if isinstance(item, str | dict)]
        if len(rules) > 1 or len(rules) != len(value):
            problems.add("rrule takes one rule such as FREQ=WEEKLY;BYDAY=MO.")
            return value
        value = rules[0] if rules else ""
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            name = {"byweekday": "BYDAY", "frequency": "FREQ"}.get(spelling(key), key.upper())
            text = ",".join(map(str, item)) if isinstance(item, list) else str(item)
            parts.append(f"{name}={text.replace('-', '') if name == 'UNTIL' else text}".upper())
        value = ";".join(sorted(parts, key=lambda part: not part.startswith("FREQ=")))
    if isinstance(value, str) and value.strip().upper().startswith("RRULE:"):
        return value.strip()[6:].strip()
    return value


def _action_word(value: Any) -> Any:
    return _ACTION_SYNONYMS.get(value, value) if isinstance(value, str) else value


# -- fields ------------------------------------------------------------------------------


def _read_extras(arguments: dict[str, Any], problems: _Problems) -> None:
    for key in list(arguments):
        word = spelling(key)
        if word in _INERT_KEYS:
            arguments.pop(key)
        elif word in _CALENDAR_KEYS:
            item = arguments.pop(key)
            if isinstance(item, str) and spelling(item) not in _DEFAULT_CALENDARS:
                problems.add(
                    f'there is one local calendar; "{key}" "{item}" cannot select another.'
                )
        elif word in _ATTENDEE_KEYS:
            names = _attendee_names(arguments.pop(key))
            if names:
                problems.add(
                    "the calendar cannot invite attendees. To record them, put them in description."
                )
                line = "Attendees: " + ", ".join(names)
                text = arguments.get("description")
                arguments["description"] = f"{text}\n{line}" if isinstance(text, str) else line


def _attendee_names(item: Any) -> list[str]:
    names: list[str] = []
    for entry in item if isinstance(item, list) else [item]:
        if isinstance(entry, dict):
            entry = entry.get("displayName") or entry.get("name") or entry.get("email")
        if isinstance(entry, str) and entry.strip():
            names.append(entry.strip())
    return names


def _omit_placeholders(arguments: dict[str, Any]) -> None:
    """Drop optional values that stand for nothing, such as "none" or "<title>"."""
    for name in ("id", "title", "description", "location", "start", "end", "query", WHEN_FIELD):
        item = arguments.get(name)
        if isinstance(item, str) and (is_placeholder(item) or _TEMPLATE.match(item)):
            del arguments[name]


def _read_action(arguments: dict[str, Any], reminder: dict[str, str] | None) -> str | None:
    """Settle the action, inferring a missing one, and attach a requested reminder."""
    action = arguments.get("action")
    if action == _REMINDER_ACTION:
        reminder = reminder or {"prompt": REMINDER_PROMPT_STAND_IN}
        action = None
    if action is None:
        if "id" in arguments and (reminder or any(name in arguments for name in CHANGE_FIELDS)):
            action = "update"
        elif "title" in arguments or reminder:
            action = "create"
        elif "duration" in arguments:
            action = "find_free_time"
        else:
            action = "list"
        arguments["action"] = action
    if reminder is not None and action in {"create", "update"}:
        when = arguments.pop(WHEN_FIELD, None)
        if isinstance(when, str) and "schedule" not in reminder:
            reminder["schedule"] = when
        arguments[REMINDER_FIELD] = reminder
    return action if isinstance(action, str) else None


def _read_window(arguments: dict[str, Any], problems: _Problems) -> None:
    """list and find_free_time read a window: start and end name its bounds."""
    for edge, bound in (("start", "time_min"), ("end", "time_max")):
        if edge in arguments:
            _merge(arguments, bound, arguments.pop(edge), problems)
    if arguments["action"] == "list":
        if "title" in arguments and "query" not in arguments:
            arguments["query"] = arguments["title"]
        keep = {"action", "id", "time_min", "time_max", "query", WHEN_FIELD}
    else:
        keep = {"action", "time_min", "time_max", "duration", WHEN_FIELD}
        if "duration" in arguments:
            minutes = duration_minutes(arguments["duration"])
            if minutes is None:
                problems.add(f'duration "{arguments["duration"]}" is not a number of minutes.')
                arguments["duration"] = STAND_INS["duration"]
            else:
                arguments["duration"] = minutes
    for name in list(arguments):
        if name not in keep:
            del arguments[name]


def _read_event(arguments: dict[str, Any], original_start: str | None, problems: _Problems) -> None:
    """create, update and delete: an event's times, rule and the occurrence they name."""
    action = arguments["action"]
    when = arguments.pop(WHEN_FIELD, None)
    if isinstance(when, str) and "start" not in arguments and _is_time(when):
        arguments["start"] = when.strip()
    for name in ("time_min", "time_max", "query"):
        arguments.pop(name, None)
    item_id = arguments.get("id")
    if isinstance(item_id, str) and original_start and parse_occurrence_id(item_id) is None:
        arguments["id"] = occurrence_id(item_id, original_start)
    if action == "create":
        if arguments.get("rrule") == "":
            del arguments["rrule"]
        if "id" in arguments:
            problems.choose(
                f'create makes a new event and takes no id; to change "{arguments["id"]}" use '
                "update:",
                [{"action": "update"}, {"id": OMIT}],
            )
    if "duration" in arguments:
        _read_length(arguments, problems)
    if action == "delete":
        changes = [name for name in CHANGE_FIELDS if name in arguments]
        if changes and "id" in arguments:
            problems.choose(
                "delete removes the event, but the call also sends changes:",
                [dict.fromkeys(changes, OMIT), {"action": "update"}],
            )


def _read_length(arguments: dict[str, Any], problems: _Problems) -> None:
    """Turn a create or update length into the end it gives, counted from the start sent."""
    length = arguments.pop("duration")
    minutes = duration_minutes(length)
    start = arguments.get("start")
    start = start.strip() if isinstance(start, str) else None
    end = None if minutes is None or start is None else _end_after(start, minutes)
    if end is None:
        if minutes is None:
            reason = f'duration "{length}" is not a length'
        elif start is None:
            reason = "duration counts from start, which this call does not send"
        elif _DATE.match(start):
            reason = f"an all-day event lasts whole days, but duration {length} is minutes"
        else:
            return  # The calendar explains a malformed start.
        problems.add(f"{reason}; send end instead.")
        arguments.setdefault("end", STAND_INS["end"])
        return
    if "end" in arguments and arguments["end"] != end:
        problems.choose('"end" and "duration" give different ends:', [{}, {"end": end}])
        return
    arguments["end"] = end


def _end_after(start: str, minutes: int) -> str | None:
    """The end ``minutes`` after a start, on the wall clock; None when the kinds do not fit."""
    if _DATE.match(start):
        if minutes % 1440:
            return None
        try:
            return (date.fromisoformat(start) + timedelta(days=minutes // 1440)).isoformat()
        except ValueError:
            return None
    try:
        begin = datetime.fromisoformat(start)
    except ValueError:
        return None
    return (begin + timedelta(minutes=minutes)).isoformat(timespec="minutes")


def _is_time(text: str) -> bool:
    try:
        datetime.fromisoformat(text.strip())
    except ValueError:
        return False
    return True


def _merge(arguments: dict[str, Any], name: str, item: Any, problems: _Problems) -> None:
    if name in arguments and arguments[name] != item:
        problems.choose(
            f'"{name}" is given twice with different values:',
            [{name: arguments[name]}, {name: item}],
        )
        return
    arguments[name] = item


__all__ = [
    "CHANGE_FIELDS",
    "OMIT",
    "REFUSAL_PREFIX",
    "REMINDER_FIELD",
    "REMINDER_PROMPT_STAND_IN",
    "REMINDER_SCHEDULE_STAND_IN",
    "STAND_INS",
    "UNADVERTISED_PARAMETERS",
    "WHEN_FIELD",
    "WINDOW_ACTIONS",
    "CalendarCallRefusedError",
    "length_text",
    "normalize_calendar_arguments",
    "refusal",
    "render_call",
]
