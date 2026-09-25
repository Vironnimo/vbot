"""Read ``calendar`` calls written in vBot's schema or another calendar dialect.

Models describe events with the fields of calendar APIs they know: Google
Calendar (``summary``, ``description``, ``location``, ``start``/``end`` objects
with ``dateTime``/``date``/``timeZone``, ``recurrence`` lists of RRULE strings,
``eventId``, ``timeMin``/``timeMax``), iCalendar (``DTSTART``, RRULE strings),
and generic event Tools (``start_time``/``end_time``, ``date``, ``event_id``).
This owner maps them onto the canonical fields so a call whose intent is clear
executes. Readings with different effects, and effects the calendar cannot
provide (invitations, several calendars), fail before any side effect with the
corrected call.

``timezone``, ``end``, ``location`` and a list ``query`` stay unadvertised: they
reach the handler, which needs the server zone or the stored event to apply them
exactly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, is_placeholder, spelling
from core.tools._named_zones import named_zone
from core.tools.contracts import ToolContract, ToolContractError

TIMEZONE_FIELD = "timezone"
END_FIELD = "end"
LOCATION_FIELD = "location"
QUERY_FIELD = "query"
UNADVERTISED_PARAMETERS: dict[str, Any] = {
    TIMEZONE_FIELD: {"type": "string", "minLength": 1},
    END_FIELD: {"type": "string", "minLength": 1},
    LOCATION_FIELD: {"type": "string", "minLength": 1},
    QUERY_FIELD: {"type": "string", "minLength": 1},
}
OMIT = object()
"""``render_call`` override that removes a field from the rendered call."""

EVENT_FIELDS = ("title", "start", "duration", "rrule", "notes")
ACTION_FIELDS = ("when", "prompt", "target", "session")
_EXTRA_EVENT_FIELDS = (END_FIELD, LOCATION_FIELD, TIMEZONE_FIELD)
_WINDOW_ACTIONS = frozenset({"list", "find_free"})
_EVENT_ACTIONS = frozenset({"create", "update"})
_ACTION_ACTIONS = frozenset({"add_action", "update_action", "delete_action"})
_ACTION_ID_PREFIX = "act_"
WHEN_STAND_IN = "<e.g. start - 1h>"
_EVENT_ID_PREFIX = "evt_"


class CalendarCallRefusedError(ValueError):
    """A ``calendar`` call was refused before any side effect; the message names the fix."""


_REFUSAL_PREFIX = "calendar was not run: "
_CALL_ORDER = (
    "action",
    "id",
    "title",
    "start",
    "duration",
    "rrule",
    "notes",
    "when",
    "prompt",
    "target",
    "session",
)
_LONG_TEXT = 120
_LONG_TEXT_STAND_INS = {
    "prompt": "<the prompt from this call>",
    "notes": "<the notes from this call>",
}
_TEMPLATE = re.compile(r"^\s*<[^<>]+>\s*$")

_FIELD_ALIASES = SpellingAliases(
    {
        "id": (
            "event_id",
            "uid",
            "event_uid",
            "calendar_event_id",
            "action_id",
            "reminder_id",
        ),
        "title": ("summary", "name", "subject", "event_title", "event_name", "label"),
        "notes": ("description", "details", "note", "body", "event_description"),
        "start": (
            "start_time",
            "start_at",
            "starts_at",
            "start_datetime",
            "start_date",
            "datetime",
            "date",
            "begin",
            "begins_at",
            "dtstart",
            "occurrence_start",
            "original_start",
            "original_start_time",
            "instance_start",
            "recurrence_id",
        ),
        "duration": (
            "duration_minutes",
            "length",
            "minutes",
            "length_minutes",
            "duration_min",
            "slot_minutes",
            "slot_duration",
        ),
        "rrule": (
            "recurrence",
            "recurrence_rule",
            "repeat",
            "repeats",
            "rule",
            "rrule_string",
            "repetition",
        ),
        "prompt": ("instruction", "instructions", "message", "task", "action_prompt", "text"),
        "target": ("agent", "agent_id", "agent_name", "target_agent", "target_agent_id"),
        "session": ("session_id", "session_key"),
        "when": ("window", "range", "period", "time_range", "date_range", "timeframe", "offset"),
        END_FIELD: (
            "end_time",
            "end_at",
            "ends_at",
            "end_datetime",
            "end_date",
            "dtend",
            "until_time",
        ),
        LOCATION_FIELD: ("place", "venue", "where", "address"),
        TIMEZONE_FIELD: ("tz", "time_zone", "zone", "timezone_name", "iana_timezone"),
        QUERY_FIELD: ("q", "search", "search_text", "keyword", "keywords", "filter", "contains"),
    }
)
_ACTION_SYNONYMS = SpellingAliases(
    {
        "list": (
            "get",
            "show",
            "view",
            "ls",
            "read",
            "events",
            "list_events",
            "get_events",
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
            "new_event",
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
        "find_free": (
            "free",
            "free_time",
            "find_free_time",
            "availability",
            "available",
            "check_availability",
            "freebusy",
            "free_busy",
            "find_slot",
            "find_slots",
            "find_time",
            "suggest_time",
        ),
        "add_action": (
            "add_reminder",
            "set_reminder",
            "create_reminder",
            "create_action",
            "attach_action",
            "schedule_action",
            "new_action",
        ),
        "update_action": (
            "edit_action",
            "modify_action",
            "change_action",
            "update_reminder",
            "edit_reminder",
        ),
        "delete_action": (
            "remove_action",
            "cancel_action",
            "delete_reminder",
            "remove_reminder",
        ),
    }
)

_WRAPPER_KEYS = frozenset({"event", "resource", "requestbody", "body", "data", "eventdata"})
_TIME_OBJECT_KEYS = frozenset(
    {"start", "end", "originalstarttime", "starttime", "endtime", "dtstart", "dtend"}
)
_WINDOW_START_KEYS = frozenset({"timemin", "from", "since", "after", "windowstart", "rangestart"})
_WINDOW_END_KEYS = frozenset({"timemax", "to", "till", "before", "windowend", "rangeend"})
_RULE_PART_KEYS = {
    "freq": "freq",
    "frequency": "freq",
    "recurrencefrequency": "freq",
    "interval": "interval",
    "count": "count",
    "occurrences": "count",
    "until": "until",
    "untildate": "until",
    "byday": "by_weekday",
    "byweekday": "by_weekday",
    "weekdays": "by_weekday",
    "daysofweek": "by_weekday",
    "days": "by_weekday",
}
_NESTED_RULE_KEYS = {"enddate": "until", "endson": "until", "repeatuntil": "until", "ends": "until"}
_HOUR_KEYS = frozenset({"durationhours", "hours", "lengthhours"})
_BEFORE_KEYS = frozenset(
    {"minutesbefore", "beforeminutes", "reminderminutes", "remindminutesbefore", "leadminutes"}
)
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
        "showdeleted",
        "status",
        "kind",
    }
)
_CALENDAR_KEYS = frozenset({"calendarid", "calendar", "calendarname"})
_DEFAULT_CALENDARS = frozenset({"primary", "default", "main", "mine", "local"})
_ATTENDEE_KEYS = frozenset({"attendees", "guests", "invitees", "participants"})
_REMINDER_KEYS = frozenset({"reminders", "reminder", "alarms", "alarm", "notifications"})
_SESSION_FRESH_WORDS = frozenset({"new", "fresh", "isolated", "auto", "default", "none"})

_WEEKDAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_WEEKDAY_NAMES = {
    "monday": "mo",
    "mon": "mo",
    "tuesday": "tu",
    "tue": "tu",
    "tues": "tu",
    "wednesday": "we",
    "wed": "we",
    "thursday": "th",
    "thu": "th",
    "thur": "th",
    "thurs": "th",
    "friday": "fr",
    "fri": "fr",
    "saturday": "sa",
    "sat": "sa",
    "sunday": "su",
    "sun": "su",
}
_FREQ_WORDS = {
    "daily": "daily",
    "day": "daily",
    "days": "daily",
    "everyday": "daily",
    "weekly": "weekly",
    "week": "weekly",
    "weeks": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "months": "monthly",
    "yearly": "yearly",
    "year": "yearly",
    "years": "yearly",
    "annually": "yearly",
    "annual": "yearly",
}
_EVERY_PHRASE = re.compile(r"^every\s+(?:(\d+)\s+)?(day|week|month|year)s?$")
_WEEKDAY_PHRASES = {
    "weekdays": ["mo", "tu", "we", "th", "fr"],
    "everyweekday": ["mo", "tu", "we", "th", "fr"],
    "workdays": ["mo", "tu", "we", "th", "fr"],
    "weekends": ["sa", "su"],
}
_RRULE_SUPPORTED_PARTS = frozenset({"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"})
_DURATION_TEXT = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$")
_ISO_DURATION = re.compile(
    r"^p(?:(\d+)w)?(?:(\d+)d)?(?:t(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?)?$", re.IGNORECASE
)
_MINUTE_UNITS = {
    "m": 1,
    "min": 1,
    "mins": 1,
    "minute": 1,
    "minutes": 1,
    "h": 60,
    "hr": 60,
    "hrs": 60,
    "hour": 60,
    "hours": 60,
}
_DAY_UNITS = frozenset({"d", "day", "days"})
_ACTION_WHEN = re.compile(r"^(start|end)(?:\s*([+-])\s*(\d+)\s*([mhd]))?$")
_RELATIVE_PHRASE = re.compile(
    r"^(?:at\s+)?(?:(\d+(?:\.\d+)?)\s*([a-z]+)\s+)?(before|after|at)\s*(?:the\s+)?"
    r"(start|end|begin|beginning|event)?$"
)
_SIGNED_OFFSET = re.compile(r"^([+-])\s*(\d+(?:\.\d+)?)\s*([a-z]*)$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


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
            raise ToolContractError(_REFUSAL_PREFIX + " ".join([*self.texts, text, calls]))
        if self.texts:
            texts = " ".join(self.texts)
            raise ToolContractError(f"{_REFUSAL_PREFIX}{texts} Send: {render_call(arguments)}")


def normalize_calendar_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return the canonical ``calendar`` arguments for one Model call."""
    value = _json_object(arguments)
    if value is None:
        return normalize_call_arguments(contract, arguments)
    problems = _Problems()
    value = _lift_wrappers(value, problems)
    value = _read_time_objects(value, problems)
    value, rule_parts = _take_rule_parts(value)
    hours = _take_hours(value, problems)
    normalized = normalize_call_arguments(
        contract,
        value,
        enum_fields=("action",),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"action": _action_word},
        empty_as_omitted=(
            "id",
            "title",
            "start",
            "notes",
            "when",
            "prompt",
            "target",
            "session",
            END_FIELD,
            LOCATION_FIELD,
            TIMEZONE_FIELD,
        ),
    )
    if not isinstance(normalized, dict):
        return normalized
    if hours is not None:
        _merge(normalized, "duration", hours, problems)
    _read_extras(normalized, problems)
    _omit_placeholders(normalized)
    _read_action(normalized, problems)
    action = normalized.get("action")
    _read_recurrence(normalized, rule_parts, problems)
    if action in _WINDOW_ACTIONS:
        if _reads_only(normalized, problems):
            _read_window(normalized, problems)
    elif action in _EVENT_ACTIONS:
        _read_event_times(normalized, problems)
    elif action in _ACTION_ACTIONS:
        _read_action_when(normalized, problems)
    _read_duration(normalized, problems)
    _check_fields(normalized, problems)
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
    ordered: dict[str, Any] = {}
    for name in (*_CALL_ORDER, *_EXTRA_EVENT_FIELDS):
        if name in call and call[name] is not OMIT:
            ordered[name] = call[name]
    for name, stand_in in _LONG_TEXT_STAND_INS.items():
        text = ordered.get(name)
        if isinstance(text, str) and len(text) > _LONG_TEXT:
            ordered[name] = stand_in
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def refusal(text: str, arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Return a refusal message that ends with the corrected call."""
    return f"{_REFUSAL_PREFIX}{text} Send: {render_call(arguments, **overrides)}"


def choice(text: str, alternatives: list[str]) -> str:
    """Return a refusal message offering several complete calls."""
    return f"{_REFUSAL_PREFIX}{text} " + " or ".join(alternatives)


def is_date(text: str) -> bool:
    return bool(_DATE_ONLY.match(text.strip()))


def is_time_of_day(text: str) -> bool:
    return bool(_TIME_ONLY.match(text.strip()))


def parse_local(text: str) -> datetime | None:
    """Parse an ISO date-time (``Z`` allowed); None when it is not one."""
    value = text.strip()
    if is_date(value):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None


def minutes_text(minutes: int) -> str:
    """Render an action offset in the largest exact unit: 90 -> 90m, 120 -> 2h."""
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


# -- shapes ----------------------------------------------------------------------------


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
        if spelling(key) not in _WRAPPER_KEYS:
            continue
        inner = _json_object(result[key])
        if inner is None:
            continue
        del result[key]
        for name, item in inner.items():
            if name in result and result[name] != item:
                problems.add(f'"{name}" is given twice with different values; send one.')
                continue
            result[name] = item
    return result


def _read_time_objects(arguments: dict[str, Any], problems: _Problems) -> dict[str, Any]:
    """Unpack Google-style ``{"dateTime", "date", "timeZone"}`` objects."""
    result = dict(arguments)
    zones: set[str] = set()
    zoned: dict[str, str] = {}
    all_day_end: str | None = None
    for key in list(result):
        item = result[key]
        if spelling(key) not in _TIME_OBJECT_KEYS or not isinstance(item, dict):
            continue
        fields = {spelling(name): value for name, value in item.items()}
        moment = fields.get("datetime")
        day = fields.get("date")
        zone = fields.get("timezone")
        if isinstance(zone, str) and zone.strip():
            zones.add(zone.strip())
        if isinstance(moment, str) and moment.strip():
            result[key] = moment.strip()
            if isinstance(zone, str) and zone.strip():
                zoned[key] = zone.strip()
        elif isinstance(day, str) and day.strip():
            if spelling(key) in {"end", "endtime", "dtend"}:
                # Google and iCalendar all-day ends are exclusive dates.
                all_day_end = day.strip()
                del result[key]
            else:
                result[key] = day.strip()
        else:
            problems.add(f'"{key}" needs a date or dateTime.')
    if len(zones) > 1:
        # Each time names its own zone, so each is an exact moment: a flight, for example.
        for key, name in zoned.items():
            found = named_zone(name)
            moment = parse_local(str(result[key]))
            if found is None:
                problems.add(f'"timeZone" "{name}" is not a known time zone.')
            elif moment is not None and moment.tzinfo is None:
                result[key] = moment.replace(tzinfo=found).isoformat()
    elif zones:
        name = zones.pop()
        existing = next(
            (value for key, value in result.items() if spelling(key) in {"timezone", "tz"}),
            None,
        )
        if existing is not None and existing != name:
            problems.add(f'"timeZone" "{name}" differs from "timezone" "{existing}"; send one.')
        elif existing is None:
            result[TIMEZONE_FIELD] = name
    if all_day_end is not None:
        start = next(
            (value for key, value in result.items() if spelling(key) in {"start", "starttime"}),
            None,
        )
        if isinstance(start, str) and is_date(start) and is_date(all_day_end):
            days = (date.fromisoformat(all_day_end) - date.fromisoformat(start)).days
            if days >= 1:
                result["duration"] = days
            else:
                problems.add('the all-day "end" date must come after "start".')
        else:
            problems.add('an all-day "end" date needs an all-day "start" date.')
    return result


def _take_rule_parts(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Take top-level recurrence parts (``frequency``, ``byday``) out of the call."""
    rest: dict[str, Any] = {}
    parts: dict[str, Any] = {}
    has_freq = any(
        _RULE_PART_KEYS.get(spelling(key)) == "freq" and value not in (None, "")
        for key, value in arguments.items()
    )
    for key, value in arguments.items():
        part = _RULE_PART_KEYS.get(spelling(key))
        if part is not None and (has_freq or part == "freq"):
            parts[part] = value
        elif spelling(key) == "days" and _number(value) is not None and "duration" not in arguments:
            # "days": 3 next to a date start is the length of an all-day event.
            rest["duration"] = value
        else:
            rest[key] = value
    return rest, parts


def _take_hours(arguments: dict[str, Any], problems: _Problems) -> int | None:
    for key in list(arguments):
        if spelling(key) in _HOUR_KEYS:
            value = arguments.pop(key)
            number = _number(value)
            if number is None or number <= 0 or number * 60 != int(number * 60):
                problems.add(f'"{key}" must be a positive number of hours.')
                return None
            return int(number * 60)
    return None


def _merge(arguments: dict[str, Any], name: str, value: Any, problems: _Problems) -> None:
    if name in arguments and arguments[name] != value:
        problems.choose(
            f'"{name}" is given twice with different values:',
            [{name: arguments[name]}, {name: value}],
        )
        return
    arguments[name] = value


def _action_word(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _ACTION_SYNONYMS.get(value, value)


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
            item = arguments.pop(key)
            if item:
                names = _attendee_names(item)
                problems.add(
                    "the calendar cannot invite attendees. To record them, put them in notes."
                )
                if names:
                    line = "Attendees: " + ", ".join(names)
                    notes = arguments.get("notes")
                    arguments["notes"] = f"{notes}\n{line}" if isinstance(notes, str) else line
        elif word in _REMINDER_KEYS:
            item = arguments.pop(key)
            if _requests_reminder(item):
                problems.add(
                    "the calendar has no reminders. After creating the event, add_action with "
                    'its id, a "when" such as "start - 30m" and a prompt runs an Agent then.'
                )


def _attendee_names(item: Any) -> list[str]:
    names: list[str] = []
    for entry in item if isinstance(item, list) else [item]:
        if isinstance(entry, str) and entry.strip():
            names.append(entry.strip())
        elif isinstance(entry, dict):
            name = entry.get("displayName") or entry.get("name") or entry.get("email")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _requests_reminder(item: Any) -> bool:
    if item in (None, False, "", [], {}):
        return False
    if isinstance(item, dict):
        use_default = item.get("useDefault", item.get("use_default"))
        overrides = item.get("overrides")
        return not (use_default is True and not overrides) and bool(overrides or item)
    return True


def _omit_placeholders(arguments: dict[str, Any]) -> None:
    for name in ("id", "title", "notes", "start", "when", "target", "session", LOCATION_FIELD):
        item = arguments.get(name)
        if isinstance(item, str) and (is_placeholder(item) or _TEMPLATE.match(item)):
            del arguments[name]
    session = arguments.get("session")
    if isinstance(session, str) and spelling(session) in _SESSION_FRESH_WORDS:
        del arguments["session"]


def _read_action(arguments: dict[str, Any], problems: _Problems) -> None:
    action = arguments.get("action")
    item_id = arguments.get("id")
    action_id = isinstance(item_id, str) and item_id.startswith(_ACTION_ID_PREFIX)
    has_event_fields = any(name in arguments for name in (*EVENT_FIELDS, END_FIELD, LOCATION_FIELD))
    has_action_fields = "prompt" in arguments or "session" in arguments
    if action is None:
        arguments["action"] = _inferred_action(arguments, action_id, has_event_fields)
        if arguments["action"] is None:
            del arguments["action"]
            problems.choose(
                f'the call names "{item_id}" but no action:',
                (
                    [{"action": "delete_action"}, {"action": "list"}]
                    if action_id
                    else [{"action": "delete"}, {"action": "list"}]
                ),
            )
        return
    if action == "delete" and action_id:
        arguments["action"] = "delete_action"
    elif action == "update" and action_id and not has_event_fields:
        arguments["action"] = "update_action"
    elif action == "update" and (has_action_fields or "target" in arguments) and not action_id:
        # An event id with an action id's kind of action is resolved by the handler, which
        # knows which actions and events the ids name.
        fields: list[str] = [name for name in ("prompt", "target", "session") if name in arguments]
        when = arguments.get("when")
        if when is not None and not (
            isinstance(when, str) and (is_date(when) or parse_local(when) is not None)
        ):
            fields.insert(0, "when")
        verb = "belongs" if len(fields) == 1 else "belong"
        text = (
            f"{', '.join(fields)} {verb} to an action. add_action attaches one to this event "
            'with its id, "when" and prompt; update_action changes one by its act_ id.'
        )
        if has_event_fields:
            # Offer the event change alone; the action follows in its own call.
            for name in fields:
                del arguments[name]
            problems.choose(text + " To change the event, send update without them:", [{}])
        else:
            arguments["action"] = "add_action"
            problems.choose(
                text + " To attach one:", [{"when": arguments.get("when", WHEN_STAND_IN)}]
            )


def _inferred_action(
    arguments: Mapping[str, Any], action_id: bool, has_event_fields: bool
) -> str | None:
    if "id" not in arguments:
        if "title" in arguments:
            return "create"
        if "duration" in arguments and ("when" in arguments or "start" in arguments):
            return "find_free"
        return "list"
    if action_id:
        return "update_action" if any(name in arguments for name in ACTION_FIELDS) else None
    if "prompt" in arguments and "when" in arguments:
        return "add_action"
    if has_event_fields:
        return "update"
    return None


def _read_recurrence(arguments: dict[str, Any], parts: dict[str, Any], problems: _Problems) -> None:
    if parts:
        if "rrule" in arguments and arguments["rrule"] is not None:
            problems.add("repetition is given twice (rrule and top-level fields); send rrule.")
            return
        arguments["rrule"] = parts
    if "rrule" not in arguments or arguments["rrule"] is None:
        return
    start = arguments.get("start")
    rule = _rule(arguments["rrule"], problems, start if isinstance(start, str) else None)
    if rule is None:
        return
    arguments["rrule"] = rule


def _rule(value: Any, problems: _Problems, start: str | None) -> dict[str, Any] | None:
    """Return a canonical rrule object for an object, RRULE string, or phrase."""
    if isinstance(value, list):
        rules = [item for item in value if not (isinstance(item, str) and not item.strip())]
        if len(rules) != 1:
            problems.add("rrule takes one repetition rule; send one object.")
            return None
        value = rules[0]
    if isinstance(value, str):
        text = value.strip()
        if "=" in text:
            return _rrule_text(text, problems, start)
        return _rule_phrase(text, problems)
    if not isinstance(value, dict):
        problems.add('rrule must be an object such as {"freq":"weekly"}.')
        return None
    rule: dict[str, Any] = {}
    for key, item in value.items():
        part = _RULE_PART_KEYS.get(spelling(key)) or _NESTED_RULE_KEYS.get(spelling(key))
        if part is None:
            rule[key] = item
            continue
        rule[part] = item
    if isinstance(rule.get("freq"), str):
        freq = _FREQ_WORDS.get(spelling(rule["freq"]))
        if freq is not None:
            rule["freq"] = freq
    for name in ("interval", "count"):
        if isinstance(rule.get(name), str) and rule[name].strip().isdigit():
            rule[name] = int(rule[name].strip())
    if "until" in rule and isinstance(rule["until"], str):
        until = _until_date(rule["until"])
        if until is None:
            problems.add(f'rrule until "{rule["until"]}" must be a date such as 2030-06-30.')
        else:
            rule["until"] = until
    if "by_weekday" in rule:
        days = _weekdays(rule["by_weekday"])
        if days is None:
            problems.add("rrule by_weekday must list days such as mo, we, fr.")
        else:
            rule["by_weekday"] = days
    for name in ("interval", "count"):
        if rule.get(name) is None:
            rule.pop(name, None)
    if rule.get("until") is None:
        rule.pop("until", None)
    return rule


def _rrule_text(text: str, problems: _Problems, start: str | None) -> dict[str, Any] | None:
    body = text.split(":", 1)[1] if text.upper().startswith("RRULE:") else text
    fields: dict[str, str] = {}
    for part in body.split(";"):
        if not part.strip():
            continue
        name, _sep, value = part.partition("=")
        fields[name.strip().upper()] = value.strip()
    rule: dict[str, Any] = {}
    freq = _FREQ_WORDS.get(spelling(fields.get("FREQ", "")))
    if freq is None:
        problems.add(
            f'RRULE FREQ "{fields.get("FREQ", "")}" must be DAILY, WEEKLY, MONTHLY or YEARLY.'
        )
        return None
    rule["freq"] = freq
    unsupported = sorted(set(fields) - _RRULE_SUPPORTED_PARTS - _start_parts(fields, freq, start))
    if unsupported:
        problems.add(
            f"the calendar cannot repeat by {', '.join(unsupported)}; it repeats by freq, "
            "interval, count or until, and weekdays for weekly rules, always on the start's "
            "day and time. The call below repeats that way without "
            f"{', '.join(unsupported)}; send it only if that is meant."
        )
    if fields.get("INTERVAL", "").isdigit():
        rule["interval"] = int(fields["INTERVAL"])
    if fields.get("COUNT", "").isdigit():
        rule["count"] = int(fields["COUNT"])
    if "UNTIL" in fields:
        until = _until_date(fields["UNTIL"])
        if until is None:
            problems.add(f'RRULE UNTIL "{fields["UNTIL"]}" is not a date.')
            return None
        rule["until"] = until
    if "BYDAY" in fields:
        days = _weekdays(fields["BYDAY"].split(","))
        if days is None:
            problems.add(
                f'RRULE BYDAY "{fields["BYDAY"]}" must list plain weekdays such as MO,WE; '
                "positions such as 1MO cannot be kept."
            )
            return None
        rule["by_weekday"] = days
    return rule


def _start_parts(fields: Mapping[str, str], freq: str, start: str | None) -> set[str]:
    """RRULE parts that only restate the start's own day, which every rule repeats on."""
    day = date.fromisoformat(start[:10]) if start and is_date(start[:10]) else None
    if day is None:
        return set()
    same: set[str] = set()
    if freq in {"monthly", "yearly"} and fields.get("BYMONTHDAY") == str(day.day):
        same.add("BYMONTHDAY")
    if freq == "yearly" and fields.get("BYMONTH") == str(day.month):
        same.add("BYMONTH")
    return same


def _rule_phrase(text: str, problems: _Problems) -> dict[str, Any] | None:
    word = spelling(text)
    if word in _FREQ_WORDS:
        return {"freq": _FREQ_WORDS[word]}
    if word in _WEEKDAY_PHRASES:
        return {"freq": "weekly", "by_weekday": list(_WEEKDAY_PHRASES[word])}
    match = _EVERY_PHRASE.match(" ".join(text.lower().split()))
    if match is not None:
        count, unit = match.groups()
        rule: dict[str, Any] = {"freq": _FREQ_WORDS[unit]}
        if count and int(count) > 1:
            rule["interval"] = int(count)
        return rule
    days = _weekdays(re.split(r"[\s,]+", text.lower().removeprefix("every ").strip()))
    if days is not None:
        return {"freq": "weekly", "by_weekday": days}
    problems.add(
        f'rrule "{text}" is not a repetition rule. Send an object such as '
        '{"freq":"weekly","by_weekday":["mo"]}.'
    )
    return None


def _until_date(text: str) -> str | None:
    value = text.strip()
    if re.fullmatch(r"\d{8}(T\d{6}Z?)?", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    elif "T" in value:
        value = value.split("T", 1)[0]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _weekdays(value: Any) -> list[str] | None:
    items = value if isinstance(value, list) else re.split(r"[\s,]+", str(value))
    days: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        word = spelling(item)
        code = word if word in _WEEKDAYS else _WEEKDAY_NAMES.get(word)
        if code is None:
            return None
        if code not in days:
            days.append(code)
    return days or None


def _reads_only(arguments: dict[str, Any], problems: _Problems) -> bool:
    """list and find_free only read: refuse fields that ask for a change. False stops reading."""
    action = arguments["action"]
    changes = [
        name
        for name in ("rrule", "notes", "prompt", "target", "session", LOCATION_FIELD)
        if name in arguments
    ]
    if not changes:
        return True
    if "title" in arguments and "start" in arguments and "prompt" not in arguments:
        problems.choose(
            f"{action} only reads the calendar, but the call describes an event:",
            [{"action": "create"}],
        )
        return False
    for name in changes:
        del arguments[name]
    problems.add(
        f"{action} only reads the calendar; {', '.join(changes)} would change it. create makes "
        "an event and add_action attaches an instruction to one. To read the calendar:"
    )
    return True


def _read_window(arguments: dict[str, Any], problems: _Problems) -> None:
    """For list and find_free, read start/end or timeMin/timeMax as the window."""
    start = arguments.pop("start", None)
    end = arguments.pop(END_FIELD, None)
    for key in list(arguments):
        word = spelling(key)
        if word in _WINDOW_START_KEYS and start is None:
            start = arguments.pop(key)
        elif word in _WINDOW_END_KEYS and end is None:
            end = arguments.pop(key)
    if start is None and end is None:
        return
    if isinstance(start, str) and not isinstance(end, str) and parse_local(start) is not None:
        # A lone start time reads as its day.
        start = start.strip()[:10]
    window = (
        f"{start}..{end}"
        if isinstance(start, str) and isinstance(end, str)
        else start
        if isinstance(start, str)
        else None
    )
    if window is None:
        problems.add('a window needs a start; use "when" such as "this week" or "start..end".')
        return
    if "when" in arguments and arguments["when"] != window:
        problems.choose('the window is given twice ("when" and start/end):', [{}, {"when": window}])
        return
    arguments["when"] = window


def _read_event_times(arguments: dict[str, Any], problems: _Problems) -> None:
    """``when`` holding a date or time on create/update is the start."""
    when = arguments.get("when")
    if isinstance(when, str) and (is_date(when) or parse_local(when) is not None):
        del arguments["when"]
        _merge(arguments, "start", when, problems)
    elif when is not None and "prompt" not in arguments:
        # With a prompt, the field check explains the action as a whole.
        text = (
            '"when" sets an action time or a list window; an event takes "start" (a date or '
            "local time)."
        )
        relative = isinstance(when, str) and _ACTION_WHEN.match(" ".join(when.lower().split()))
        if relative and "id" in arguments:
            problems.choose(
                text + " To attach an action at that time:",
                [{"action": "add_action", "prompt": "<instruction>"}],
            )
        else:
            problems.choose(text, [{"when": OMIT, "start": "<2030-01-10 or 2030-01-10T15:00>"}])


def _read_action_when(arguments: dict[str, Any], problems: _Problems) -> None:
    """Read reminder-style offsets: '1h before', '-30m', minutes_before."""
    for key in list(arguments):
        if spelling(key) in _BEFORE_KEYS:
            minutes = _number(arguments.pop(key))
            if minutes is None or minutes < 0 or minutes != int(minutes):
                problems.add(f'"{key}" must be a whole number of minutes.')
            else:
                _merge(arguments, "when", _when_text("start", -int(minutes)), problems)
    when = arguments.get("when")
    if isinstance(when, str) and re.fullmatch(r"\s*-?\d+\s*", when):
        # A bare number of minutes, possibly already turned into text by the schema.
        when = int(when)
    if isinstance(when, (int, float)) and not isinstance(when, bool):
        if when == int(when) and when < 0:
            arguments["when"] = _when_text("start", int(when))
            return
        problems.choose(
            f"when {when} has no anchor or unit:",
            [
                {"when": _when_text("start", -abs(int(when)))},
                {"when": _when_text("end", abs(int(when)))},
            ],
        )
        return
    if not isinstance(when, str):
        return
    text = " ".join(when.lower().split())
    if _ACTION_WHEN.match(text):
        arguments["when"] = _canonical_when(text)
        return
    phrase = _RELATIVE_PHRASE.match(text)
    if phrase is not None:
        amount, unit, relation, anchor = phrase.groups()
        minutes = _offset_minutes(amount, unit) if amount else 0
        if minutes is None:
            problems.add(f'when "{when}" needs a duration in m, h or d, e.g. "start - 1h".')
            return
        base = {"begin": "start", "beginning": "start", "event": None}.get(anchor or "", anchor)
        if relation == "at":
            arguments["when"] = base or "start"
        elif relation == "before":
            arguments["when"] = _when_text(base or "start", -minutes)
        elif base is not None:
            arguments["when"] = _when_text(base, minutes)
        else:
            problems.choose(
                f'when "{when}" can mean after the start or after the end:',
                [
                    {"when": _when_text("start", minutes)},
                    {"when": _when_text("end", minutes)},
                ],
            )
        return
    signed = _SIGNED_OFFSET.match(text)
    if signed is not None:
        sign, amount, unit = signed.groups()
        minutes = _offset_minutes(amount, unit or "m")
        if minutes is None:
            problems.add(f'when "{when}" needs a duration in m, h or d, e.g. "start - 1h".')
        elif sign == "-":
            arguments["when"] = _when_text("start", -minutes)
        else:
            problems.choose(
                f'when "{when}" can count from the start or from the end:',
                [
                    {"when": _when_text("start", minutes)},
                    {"when": _when_text("end", minutes)},
                ],
            )


def _canonical_when(text: str) -> str:
    match = _ACTION_WHEN.match(text)
    assert match is not None
    anchor, sign, count, unit = match.groups()
    return f"{anchor} {sign} {count}{unit}" if sign else anchor


def _when_text(anchor: str, minutes: int) -> str:
    if minutes == 0:
        return anchor
    return f"{anchor} {'-' if minutes < 0 else '+'} {minutes_text(abs(minutes))}"


def _offset_minutes(amount: str | None, unit: str | None) -> int | None:
    if amount is None:
        return None
    number = float(amount)
    word = (unit or "").strip()
    factor = _MINUTE_UNITS.get(word) or (1440 if word in _DAY_UNITS else None)
    if factor is None:
        return None
    minutes = number * factor
    return int(minutes) if minutes == int(minutes) else None


def _read_duration(arguments: dict[str, Any], problems: _Problems) -> None:
    duration = arguments.get("duration")
    if not isinstance(duration, str) or duration.strip().isdigit():
        return
    start = arguments.get("start")
    all_day = isinstance(start, str) and is_date(start)
    kind_known = isinstance(start, str) or arguments.get("action") == "find_free"
    text = duration.strip().lower()
    iso = _ISO_DURATION.match(text)
    minutes: float | None = None
    days: int | None = None
    if iso is not None and any(iso.groups()):
        weeks, day_count, hours, mins, seconds = (int(part or 0) for part in iso.groups())
        if seconds or ((weeks or day_count) and (hours or mins)):
            minutes = None
        elif weeks or day_count:
            days = weeks * 7 + day_count
        else:
            minutes = hours * 60 + mins
    else:
        match = _DURATION_TEXT.match(text)
        if match is not None:
            number, unit = float(match.group(1)), match.group(2)
            if unit in _MINUTE_UNITS:
                minutes = number * _MINUTE_UNITS[unit]
            elif unit in _DAY_UNITS and number == int(number):
                days = int(number)
            elif unit in {"w", "week", "weeks"} and number == int(number):
                days = int(number) * 7
    if minutes is not None and minutes == int(minutes) and minutes > 0 and kind_known:
        if all_day:
            problems.choose(
                f'duration "{duration}" is a time span, but a date start makes an all-day '
                "event, which lasts whole days. For a timed event, give start a time:",
                [{"start": f"{str(start).strip()}T<HH:MM>", "duration": int(minutes)}],
            )
        else:
            arguments["duration"] = int(minutes)
        return
    if days is not None and days > 0 and kind_known:
        if all_day:
            arguments["duration"] = days
        elif arguments.get("action") == "find_free":
            problems.add(f'find_free looks for time spans in minutes, not "{duration}".')
        else:
            arguments["duration"] = days * 1440
        return
    problems.add(
        f'duration "{duration}" must be a whole number: minutes for a timed event, days for '
        "an all-day event."
    )


def _check_fields(arguments: dict[str, Any], problems: _Problems) -> None:
    """Refuse fields that point to another action than the one named; drop unused ones."""
    action = arguments.get("action")
    if action in _WINDOW_ACTIONS:
        if problems.choice is not None:
            return
        if action == "list" and "title" in arguments and QUERY_FIELD not in arguments:
            # A title on a read names what to look for.
            arguments[QUERY_FIELD] = arguments["title"]
        keep = {"action", "when", TIMEZONE_FIELD}
        keep |= {"duration"} if action == "find_free" else {QUERY_FIELD, "id"}
        for name in list(arguments):
            if name not in keep:
                del arguments[name]
        return
    arguments.pop(QUERY_FIELD, None)
    if action == "create":
        if "id" in arguments:
            problems.choose(
                f'create makes a new event and takes no id; to change "{arguments["id"]}" use '
                "update:",
                [{"action": "update"}, {"id": OMIT}],
            )
            return
        action_fields = [name for name in ACTION_FIELDS if name in arguments]
        if action_fields:
            verb = "belongs" if len(action_fields) == 1 else "belong"
            for name in action_fields:
                del arguments[name]
            problems.choose(
                f"{', '.join(action_fields)} {verb} to an action, which attaches to an existing "
                'event. Create the event first, then send add_action with its id, "when" and '
                "prompt. The create call:",
                [{}],
            )
        return
    if action == "delete":
        changes = [
            name
            for name in ("title", "duration", "rrule", "notes", END_FIELD, LOCATION_FIELD)
            if name in arguments
        ]
        for name in ACTION_FIELDS:
            arguments.pop(name, None)
        if "start" not in arguments:
            arguments.pop(TIMEZONE_FIELD, None)
        if changes and "id" in arguments:
            problems.choose(
                "delete removes the event, but the call also sends changes:",
                [
                    dict.fromkeys((*changes, TIMEZONE_FIELD), OMIT),
                    {"action": "update", "start": OMIT},
                ],
            )
        return
    if action in {"add_action", "update_action"} and "when" not in arguments:
        start = arguments.get("start")
        if isinstance(start, str):
            # A due time sent as start; the handler relates it to the event.
            arguments["when"] = start
    if action in _ACTION_ACTIONS:
        when = arguments.get("when")
        clock_time = isinstance(when, str) and (is_date(when) or parse_local(when) is not None)
        for name in (*EVENT_FIELDS, END_FIELD, LOCATION_FIELD):
            if name == "title" and "id" not in arguments and action == "add_action":
                continue  # The handler finds the event by its title and names the id.
            arguments.pop(name, None)
        if action == "delete_action" or not clock_time:
            # A zone only matters for a clock-time when, which the handler relates to the event.
            arguments.pop(TIMEZONE_FIELD, None)
        if action == "delete_action":
            for name in ACTION_FIELDS:
                arguments.pop(name, None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


__all__ = [
    "END_FIELD",
    "LOCATION_FIELD",
    "OMIT",
    "QUERY_FIELD",
    "TIMEZONE_FIELD",
    "UNADVERTISED_PARAMETERS",
    "WHEN_STAND_IN",
    "CalendarCallRefusedError",
    "choice",
    "is_date",
    "is_time_of_day",
    "minutes_text",
    "normalize_calendar_arguments",
    "parse_local",
    "refusal",
    "render_call",
]
