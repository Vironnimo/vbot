"""Built-in calendar tool for managing the user's local calendar."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import cache
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarServiceError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar.service import FIND_FREE_MAX_RESULTS
from core.calendar.when import parse_when
from core.projects.address import format_agent_address
from core.tools._calendar_arguments import (
    END_FIELD,
    LOCATION_FIELD,
    OMIT,
    QUERY_FIELD,
    TIMEZONE_FIELD,
    UNADVERTISED_PARAMETERS,
    WHEN_STAND_IN,
    CalendarCallRefusedError,
    choice,
    is_date,
    is_time_of_day,
    minutes_text,
    normalize_calendar_arguments,
    parse_local,
    refusal,
    render_call,
)
from core.tools._named_zones import named_zone, offset, same_zone, server_shifts, server_text
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.calendar import CalendarEvent, CalendarService, EventOccurrence

CALENDAR_TOOL_NAME = "calendar"
CALENDAR_TOOL_DESCRIPTION = (
    "The user's calendar: list events, find free time, create, change and delete events, and "
    "attach actions, instructions an Agent carries out before, at or after an event. Times are "
    "local to the Time zone shown in Runtime Environment. An action starts a Run of its target "
    "Agent with prompt plus the event's title, time and notes; it moves with its event and is "
    "deleted with it. For schedules that should not appear in the calendar, use cron if "
    "available."
)

CALENDAR_ACTIONS = frozenset(
    (
        "list",
        "create",
        "update",
        "delete",
        "find_free",
        "add_action",
        "update_action",
        "delete_action",
    )
)

CALENDAR_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list",
                "create",
                "update",
                "delete",
                "find_free",
                "add_action",
                "update_action",
                "delete_action",
            ],
            "description": (
                "list shows events with their ids and actions; find_free shows free time. "
                "update and update_action change only the fields you send."
            ),
        },
        "when": {
            "type": "string",
            "minLength": 1,
            "description": (
                "For list and find_free: today, tomorrow, this week, next week, this month, "
                "next month, a date, a year-month (2030-01) or 'start..end'; defaults are this "
                "month and the next 7 days. For actions: start or end, optionally +/- a "
                "duration in m, h or d, e.g. 'start - 1h'."
            ),
        },
        "id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Event id, or for update_action and delete_action the action id (act_...), "
                "from list or an earlier result."
            ),
        },
        "title": {
            "type": "string",
            "minLength": 1,
            "description": "Event title. Required for create.",
        },
        "start": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Local date or time: 2030-01-10 makes an all-day event, 2030-01-10T15:00 a "
                "timed one. Required for create. On delete, an occurrence start from list "
                "removes just that occurrence of a repeating event."
            ),
        },
        "duration": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Minutes for timed events and find_free, days for all-day events. Defaults: "
                "60 minutes, 1 day."
            ),
        },
        "rrule": {
            "type": ["object", "null"],
            "description": (
                'Repetition, e.g. {"freq":"weekly","by_weekday":["mo","we"]}: freq daily, '
                "weekly, monthly or yearly; optional interval, count or until (inclusive "
                "date), by_weekday for weekly. null on update stops repeating."
            ),
        },
        "notes": {"type": "string", "description": "Free text kept with the event."},
        "prompt": {
            "type": "string",
            "description": "Instruction the action's Run carries out. Required for add_action.",
        },
        "target": {
            "type": "string",
            "description": (
                "Agent that runs the action: agent or agent@project. Defaults to the current Agent."
            ),
        },
        "session": {
            "type": "string",
            "description": (
                "Session of the target Agent to run the action in. Omit for a fresh Session "
                "each time."
            ),
        },
    },
    "required": ["action"],
}

_EVENT_ID_ACTIONS = frozenset({"update", "delete", "add_action"})
_ACTION_ID_ACTIONS = frozenset({"update_action", "delete_action"})
_CURRENT_SESSION_WORDS = frozenset({"current", "this", "here", "same", "thissession"})
_FREE_WINDOW_DAYS = 7
_DEFAULT_FREE_MINUTES = 60
_UPCOMING_DAYS = 400
_NEARBY = timedelta(days=7)
_ACTION_REACH = timedelta(days=32)
_LISTED_OCCURRENCES = 8
_LISTED_RUNS = 3
_TITLE_MATCHES = 5
_WEEKDAY_ORDER = ("mo", "tu", "we", "th", "fr", "sa", "su")
_STAND_INS = {
    "title": "<title>",
    "start": "<2030-01-10 or 2030-01-10T15:00>",
    "duration": "<minutes, or days for all-day>",
    "rrule": '<rule such as {"freq":"weekly"}>',
    "notes": "<notes>",
    "when": WHEN_STAND_IN,
    "prompt": "<instruction>",
    "target": "<agent or agent@project>",
}
_FIELD_WORDS = {
    "title": "title",
    "start": "start",
    "duration": "duration",
    "duration_minutes": "duration",
    "duration_days": "duration",
    "rrule": "rrule",
    "notes": "notes",
    "when": "when",
    "window": "when",
    "prompt": "prompt",
}
_TARGET_GUIDANCE = 'Set "target" to an existing Agent id, or to agent@project for a Project member.'

_LOGGER = get_logger("tools.calendar")


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name=CALENDAR_TOOL_NAME,
        input_schema={
            **CALENDAR_TOOL_PARAMETERS,
            "properties": {**CALENDAR_TOOL_PARAMETERS["properties"], **UNADVERTISED_PARAMETERS},
        },
        require_closed_input=False,
    )


def _normalize_calendar_arguments(arguments: Any) -> Any:
    return normalize_calendar_arguments(_repair_contract(), arguments)


def register_calendar_tool(registry: ToolRegistry, calendar_service: CalendarService) -> None:
    """Register the calendar tool with a vBot tool registry."""

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _handle_calendar_tool(calendar_service, arguments, context)

    registry.register(
        CALENDAR_TOOL_NAME,
        CALENDAR_TOOL_DESCRIPTION,
        CALENDAR_TOOL_PARAMETERS,
        handler,
        open_input_schema=True,
        argument_normalizer=_normalize_calendar_arguments,
        unadvertised_parameters=UNADVERTISED_PARAMETERS,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_calendar_display_parts,
            # Only list results carry an occurrence count.
            fact_builder=result_count_fact_builder("occurrences"),
        ),
    )


def _handle_calendar_tool(
    calendar_service: CalendarService, arguments: JsonObject, context: ToolContext | None = None
) -> JsonObject:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in CALENDAR_ACTIONS:
        options = ", ".join(sorted(CALENDAR_ACTIONS))
        return tool_failure("invalid_arguments", f"action must be one of: {options}.")
    try:
        if action in _EVENT_ID_ACTIONS | _ACTION_ID_ACTIONS and "id" not in arguments:
            raise CalendarCallRefusedError(_missing_id(calendar_service, action, arguments))
        if action in _EVENT_ID_ACTIONS | _ACTION_ID_ACTIONS:
            _check_id_kind(calendar_service, action, arguments)
        if action == "list":
            return _handle_list(calendar_service, arguments)
        if action == "find_free":
            return _handle_find_free(calendar_service, arguments)
        if action == "create":
            return _handle_create(calendar_service, arguments)
        if action == "update":
            return _handle_update(calendar_service, arguments)
        if action == "delete":
            return _handle_delete(calendar_service, arguments)
        if action == "add_action":
            return _handle_add_action(calendar_service, arguments, context)
        if action == "update_action":
            return _handle_update_action(calendar_service, arguments, context)
        return _handle_delete_action(calendar_service, arguments)
    except CalendarCallRefusedError as error:
        return tool_failure("invalid_arguments", str(error))
    except CalendarEventNotFoundError as error:
        kind = "action" if "action not found" in str(error) else "event"
        return tool_failure(
            f"{kind}_not_found",
            f'No {kind} has id "{arguments.get("id")}". {{"action":"list"}} shows events, their '
            'actions and ids; add a when such as "next month" to look further ahead.',
        )
    except CalendarValidationError as error:
        return tool_failure("invalid_arguments", _validation_message(arguments, error))
    except CalendarStorageError as error:
        _LOGGER.warning("Calendar storage error for action=%s: %s", action, error)
        return tool_failure(
            "calendar_storage_error", f"{error}. Do not repeat the same call unchanged."
        )
    except CalendarServiceError as error:
        _LOGGER.warning("Calendar service error for action=%s: %s", action, error)
        return tool_failure(
            "calendar_service_error", f"{error}. Do not repeat the same call unchanged."
        )


def _missing_id(calendar_service: CalendarService, action: str, arguments: JsonObject) -> str:
    """Name the call with the id: a title that matches one event supplies it."""
    kind = "action" if action in _ACTION_ID_ACTIONS else "event"
    title = arguments.get("title")
    if kind == "event" and isinstance(title, str):
        wanted = title.strip().casefold()
        events = calendar_service.list_events()
        matches = [event for event in events if event.title.strip().casefold() == wanted]
        matches = matches or [event for event in events if wanted in event.title.casefold()]
        # update may be renaming; delete and add_action only used the title to find the event.
        kept: dict[str, Any] = {} if action == "update" else {"title": OMIT}
        if len(matches) == 1:
            event = matches[0]
            start = _event_fields(calendar_service, event)["start"]
            return refusal(
                f'{action} needs the event "id"; "{event.title}" at {start} has id {event.id}.',
                arguments,
                id=event.id,
                **kept,
            )
        if matches:
            calls = [
                render_call(arguments, id=event.id, **kept)
                + f' ("{event.title}" at {_event_fields(calendar_service, event)["start"]})'
                for event in matches[:_TITLE_MATCHES]
            ]
            return choice(f'{action} needs the event "id"; several events match "{title}":', calls)
    return refusal(
        f'{action} needs the {kind} "id"; {{"action":"list"}} shows events, their actions and ids.',
        arguments,
        id=f"<{kind} id from list>",
        **({} if action == "update" else {"title": OMIT}),
    )


def _check_id_kind(calendar_service: CalendarService, action: str, arguments: JsonObject) -> None:
    """Refuse an event id where an action id belongs, or the reverse, naming the right call."""
    item_id = str(arguments["id"])
    if action in _ACTION_ID_ACTIONS and item_id.startswith("evt_"):
        event = calendar_service.get_event(item_id)
        actions = calendar_service.actions.list_actions(event.id)
        text = f'{action} takes an action id (act_...); "{item_id}" is the event "{event.title}"'
        if not actions:
            if action == "delete_action":
                raise CalendarCallRefusedError(
                    f"calendar was not run: {text}, which has no actions to delete."
                )
            raise CalendarCallRefusedError(
                refusal(
                    f"{text}, which has no actions; add_action attaches one.",
                    arguments,
                    action="add_action",
                    when=arguments.get("when", WHEN_STAND_IN),
                )
            )
        if len(actions) == 1:
            raise CalendarCallRefusedError(
                refusal(
                    f"{text}, whose one action is {actions[0]['id']}.",
                    arguments,
                    id=actions[0]["id"],
                )
            )
        calls = [
            render_call(arguments, id=item["id"]) + f" ({item['when']}: {_brief(item['prompt'])})"
            for item in actions
        ]
        raise CalendarCallRefusedError(choice(f"{text}, which has several actions:", calls))
    if action in _EVENT_ID_ACTIONS and item_id.startswith("act_"):
        # delete and update of an action id were already read as delete_action/update_action.
        current = _action(calendar_service, item_id)
        event = calendar_service.get_event(current["event_id"])
        raise CalendarCallRefusedError(
            refusal(
                f'{action} takes an event id; "{item_id}" is an action of "{event.title}" '
                f"({event.id}).",
                arguments,
                id=event.id,
            )
        )


def _brief(text: str) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= 40 else line[:37] + "..."


# -- reading the calendar ---------------------------------------------------------------


def _handle_list(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    zone = _server_zone(calendar_service)
    window_start, window_end, note = _window(calendar_service, arguments, "this month")
    events = {event.id: event for event in calendar_service.list_events()}
    by_event: dict[str, list[EventOccurrence]] = {}
    item_id = arguments.get("id")
    if isinstance(item_id, str):
        # One event, shown even when none of its occurrences falls in the window.
        chosen = _event_for_id(calendar_service, item_id)
        events = {chosen.id: chosen}
        by_event[chosen.id] = []
    for occurrence in calendar_service.occurrences_in_window(window_start, window_end):
        if occurrence.event_id in events:
            by_event.setdefault(occurrence.event_id, []).append(occurrence)
    query = arguments.get(QUERY_FIELD)
    if isinstance(query, str):
        text = query.casefold()
        by_event = {
            event_id: items
            for event_id, items in by_event.items()
            if text in events[event_id].title.casefold()
            or text in (events[event_id].notes or "").casefold()
        }
    actions: dict[str, list[dict[str, Any]]] = {}
    for item in calendar_service.actions.list_actions():
        actions.setdefault(item["event_id"], []).append(item)
    listed = [item for items in by_event.values() for item in items]
    runs: dict[str, list[dict[str, Any]]] = {}
    for row in calendar_service.actions.project(listed):
        runs.setdefault(row["action_id"], []).append(row)
    data: JsonObject = {
        "events": len(by_event),
        "occurrences": len(listed),
        "window": _window_text(window_start, window_end, zone),
        "timezone": calendar_service.system_timezone_name(),
    }
    if isinstance(query, str):
        data["matching"] = query
    if calendar_service.actions.storage_error:
        data["action_error"] = calendar_service.actions.storage_error
    if note:
        data["note"] = note
    if by_event:
        data["content"] = "\n\n".join(
            _event_block(calendar_service, events[event_id], items, actions.get(event_id, []), runs)
            for event_id, items in by_event.items()
        )
    return tool_success(data)


def _event_for_id(calendar_service: CalendarService, item_id: str) -> CalendarEvent:
    """The event an id names: an event id, or an action id standing for its event."""
    for item in calendar_service.actions.list_actions():
        if item["id"] == item_id:
            return calendar_service.get_event(item["event_id"])
    return calendar_service.get_event(item_id)


def _handle_find_free(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    zone = _server_zone(calendar_service)
    duration = arguments.get("duration", _DEFAULT_FREE_MINUTES)
    window_start, window_end, note = _window(calendar_service, arguments, None)
    slots = calendar_service.find_free_slots(
        window_start, window_end, duration, max_results=FIND_FREE_MAX_RESULTS + 1
    )
    shown = slots[:FIND_FREE_MAX_RESULTS]
    data: JsonObject = {
        "free": len(shown),
        "window": _window_text(window_start, window_end, zone),
        "timezone": calendar_service.system_timezone_name(),
    }
    notes = [note] if note else []
    if not shown:
        notes.append(f"No free span of {_length_text(duration)} or more in this window.")
    elif len(slots) > len(shown):
        notes.append(
            f"More free time follows after {_local_text(shown[-1].end_utc, zone)}; a later "
            "when shows it."
        )
    if notes:
        data["note"] = " ".join(notes)
    if shown:
        data["content"] = "\n".join(
            f"{_local_text(slot.start_utc, zone)} to {_local_text(slot.end_utc, zone)} "
            f"({_length_text(int((slot.end_utc - slot.start_utc).total_seconds() // 60))})"
            for slot in shown
        )
    return tool_success(data)


def _window(
    calendar_service: CalendarService, arguments: JsonObject, default: str | None
) -> tuple[datetime, datetime, str | None]:
    """Resolve the list or find_free window, read in the call's time zone when it names one."""
    server = _server_zone(calendar_service)
    now = datetime.now(UTC)
    zone: tzinfo = server
    name = arguments.get(TIMEZONE_FIELD)
    if isinstance(name, str):
        found = named_zone(name)
        if found is None:
            raise CalendarCallRefusedError(_unknown_zone(name, server, arguments))
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
            f"Read the window in {name}: {_window_text(*window, server)} in the server time "
            f"zone {server}."
        )
    return window[0], window[1], note


def _event_block(
    calendar_service: CalendarService,
    event: CalendarEvent,
    occurrences: list[EventOccurrence],
    actions: list[dict[str, Any]],
    runs: dict[str, list[dict[str, Any]]],
) -> str:
    zone = _server_zone(calendar_service)
    fields = _event_fields(calendar_service, event)
    fields.pop("actions", None)
    notes = fields.pop("notes", None)
    lines = [f"{key}: {value}" for key, value in fields.items()]
    if event.rrule is not None:
        lines.append(_occurrence_line(occurrences))
    if notes:
        lines.append("notes: " + "\n  ".join(str(notes).splitlines()))
    for item in actions:
        lines.extend(_action_lines(item, runs.get(item["id"], []), zone))
    return "\n".join(lines)


def _occurrence_line(occurrences: list[EventOccurrence]) -> str:
    starts = [_minute_text(item.occurrence_start) for item in occurrences]
    if not starts:
        return "occurrences: none in this window"
    if len(starts) <= _LISTED_OCCURRENCES:
        return "occurrences: " + ", ".join(starts)
    return f"occurrences: {len(starts)} in this window, {', '.join(starts[:3])}, ..., {starts[-1]}"


def _action_lines(action: dict[str, Any], rows: list[dict[str, Any]], zone: ZoneInfo) -> list[str]:
    session = action.get("session")
    where = f"in Session {session}" if session else "in a fresh Session"
    lines = [f"action {action['id']}: {action['when']}, runs {action['target']} {where}"]
    lines.append("  prompt: " + "\n    ".join(str(action["prompt"]).splitlines()))
    now = datetime.now(UTC)
    done: list[str] = []
    upcoming = None
    for row in sorted(rows, key=lambda item: _instant(item["scheduled_at"])):
        due = _local_text(_instant(row["scheduled_at"]), zone)
        if row["status"] == "pending":
            if upcoming is None and _instant(row["expires_at"]) > now:
                upcoming = due
            continue
        detail = f", Session {row['session']}" if row.get("session") else ""
        done.append(f"  {due} {row['status']}{detail}")
    if len(done) > _LISTED_RUNS:
        lines.append(f"  {len(done) - _LISTED_RUNS} earlier runs in this window")
    lines.extend(done[-_LISTED_RUNS:])
    if upcoming:
        lines.append(f"  next: {upcoming}")
    return lines


# -- changing events ----------------------------------------------------------------------


def _handle_create(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    missing = [name for name in ("title", "start") if name not in arguments]
    if missing:
        texts = {
            "title": '"title"',
            "start": '"start", a date for an all-day event or a local time for a timed one',
        }
        raise CalendarCallRefusedError(
            refusal(
                "create needs " + " and ".join(texts[name] for name in missing) + ".",
                arguments,
                **{name: _STAND_INS[name] for name in missing},
            )
        )
    if arguments.get("rrule", 0) is None:
        raise CalendarCallRefusedError(
            refusal(
                "rrule null only stops repetition on update; omit it for a single event.",
                arguments,
                rrule=OMIT,
            )
        )
    arguments, note = _apply_timezone(calendar_service, arguments, None)
    arguments = _apply_end(calendar_service, arguments, None)
    arguments = _apply_location(arguments, None)
    start = str(arguments["start"])
    all_day = is_date(start)
    duration = arguments.get("duration")
    event = calendar_service.create_event(
        title=str(arguments["title"]),
        start=start,
        duration_minutes=None if all_day else duration,
        duration_days=duration if all_day else None,
        rrule=arguments.get("rrule"),
        notes=arguments.get("notes"),
    )
    return _event_success(calendar_service, event, note)


def _handle_update(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    event = calendar_service.get_event(str(arguments["id"]))
    arguments, note = _apply_timezone(calendar_service, arguments, event)
    arguments = _apply_end(calendar_service, arguments, event)
    arguments = _apply_location(arguments, event)
    updates: JsonObject = {}
    for name in ("title", "notes"):
        if name in arguments:
            updates[name] = arguments[name]
    if "start" in arguments:
        updates["start"] = arguments["start"]
        # The start form decides the kind; pass it so all-day and timed can switch.
        updates["all_day"] = is_date(str(arguments["start"]))
    if "duration" in arguments:
        all_day = updates.get("all_day", event.all_day)
        updates["duration_days" if all_day else "duration_minutes"] = arguments["duration"]
    if "rrule" in arguments:
        updates["rrule"] = arguments["rrule"]
    if not updates:
        raise CalendarCallRefusedError(
            refusal(
                "update needs a field to change: title, start, duration, rrule or notes.",
                arguments,
                start=_STAND_INS["start"],
            )
        )
    updated = calendar_service.update_event(event.id, **updates)
    return _event_success(calendar_service, updated, note)


def _handle_delete(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    event = calendar_service.get_event(str(arguments["id"]))
    start = arguments.get("start")
    removed_actions = len(calendar_service.actions.list_actions(event.id))
    if not isinstance(start, str):
        calendar_service.delete_event(event.id)
        return _deleted(event, removed_actions, None)
    occurrence = _occurrence_start(calendar_service, event, start, arguments)
    if event.rrule is None:
        calendar_service.delete_event(event.id)
        return _deleted(
            event, removed_actions, "The event does not repeat, so the whole event was deleted."
        )
    calendar_service.add_exdate(event.id, occurrence)
    return tool_success(
        {
            "id": event.id,
            "title": event.title,
            "removed_occurrence": _minute_text(occurrence),
            "status": "occurrence removed; the rest of the series stays",
        }
    )


def _deleted(event: CalendarEvent, removed_actions: int, note: str | None) -> JsonObject:
    data: JsonObject = {"id": event.id, "title": event.title, "status": "deleted"}
    if removed_actions:
        data["actions_removed"] = removed_actions
    if note:
        data["note"] = note
    return tool_success(data)


def _occurrence_start(
    calendar_service: CalendarService, event: CalendarEvent, start: str, arguments: JsonObject
) -> str:
    """Return the stored form of the occurrence start a delete names, or refuse."""
    server = _server_zone(calendar_service)
    # Occurrence starts are written in the event's own wall-clock zone.
    own = ZoneInfo(event.tz_name) if event.tz_name else server
    text = start.strip()
    if event.all_day:
        wanted = text[:10]
        if not is_date(wanted):
            raise CalendarCallRefusedError(
                refusal(
                    f'"{start}" is not a date; this all-day event has dates as occurrence starts.',
                    arguments,
                    start="<occurrence date from list>",
                )
            )
        center = datetime.combine(date.fromisoformat(wanted), time.min, server)
    else:
        parsed = parse_local(text)
        if parsed is None:
            raise CalendarCallRefusedError(
                refusal(
                    f'"{start}" is not a local time; this timed event has times such as '
                    "2030-01-10T15:00 as occurrence starts.",
                    arguments,
                    start="<occurrence start from list>",
                )
            )
        name = arguments.get(TIMEZONE_FIELD)
        if isinstance(name, str) and parsed.tzinfo is None:
            zone = named_zone(name)
            if zone is None:
                raise CalendarCallRefusedError(_unknown_zone(name, server, arguments))
            parsed = parsed.replace(tzinfo=zone)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(own).replace(tzinfo=None)
        wanted = parsed.replace(microsecond=0).isoformat()
        center = parsed.replace(tzinfo=own)
    nearby = calendar_service.event_occurrences(
        event, (center - _NEARBY).astimezone(UTC), (center + _NEARBY).astimezone(UTC)
    )
    starts = [item.occurrence_start for item in nearby]
    if wanted in starts:
        return wanted
    corrected: dict[str, Any] = {TIMEZONE_FIELD: OMIT}
    if event.rrule is None:
        own_start = _event_fields(calendar_service, event)["start"]
        raise CalendarCallRefusedError(
            refusal(
                f'the event does not repeat and starts at {own_start}, not "{start}". To delete '
                "it, send the call without start.",
                arguments,
                start=OMIT,
                **corrected,
            )
        )
    if not starts:
        listing = render_call({"action": "list", "id": event.id, "when": "<its month>"})
        raise CalendarCallRefusedError(
            refusal(
                f'"{start}" is not an occurrence of this event, and none falls within a week of '
                f"it. {listing} shows them.",
                arguments,
                start="<occurrence start from list>",
                **corrected,
            )
        )
    closest = sorted(starts, key=lambda item: abs(_seconds(item) - _seconds(wanted)))[:3]
    calls = [
        render_call(arguments, start=_minute_text(item), **corrected) for item in sorted(closest)
    ]
    raise CalendarCallRefusedError(
        choice(f'"{start}" is not an occurrence of this event. Nearby occurrences:', calls)
    )


def _seconds(value: str) -> float:
    return (datetime.fromisoformat(value) - datetime(2000, 1, 1)).total_seconds()


def _apply_timezone(
    calendar_service: CalendarService, arguments: JsonObject, event: CalendarEvent | None
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
    server = _server_zone(calendar_service)
    now = datetime.now(UTC)
    zone = named_zone(name)
    if zone is None:
        raise CalendarCallRefusedError(_unknown_zone(name, server, result))
    if same_zone(zone, server, now):
        return result, None
    recurring = (
        result["rrule"] is not None
        if "rrule" in result
        else event is not None and event.rrule is not None
    )
    written, shown = [], []
    for field in fields:
        written.append(f'"{result[field]}"')
        result[field] = _in_server_time(
            str(result[field]), field, name, zone, server, recurring, result
        )
        converted = datetime.fromisoformat(result[field]).replace(tzinfo=None)
        shown.append(converted.isoformat(timespec="minutes"))
    return result, (
        f"Read {' and '.join(written)} as {name} time: {' and '.join(shown)} in the server "
        f"time zone {server}."
    )


def _in_server_time(
    text: str,
    field: str,
    name: str,
    zone: tzinfo,
    server: ZoneInfo,
    recurring: bool,
    arguments: JsonObject,
) -> str:
    parsed = parse_local(text)
    assert parsed is not None
    if parsed.tzinfo is not None:
        if parsed.replace(tzinfo=zone).utcoffset() != parsed.utcoffset():
            wall = parsed.replace(tzinfo=None)
            as_written = render_call(arguments, **{field: text})
            zoned = render_call(
                arguments, **{field: server_text(wall.replace(tzinfo=zone), server)}
            )
            raise CalendarCallRefusedError(
                choice(
                    f'"{text}" carries an offset that is not {name} time. Send the one that is '
                    "meant:",
                    [
                        f"{as_written} (the time as written)",
                        f"{zoned} ({wall.isoformat(timespec='minutes')} in {name})",
                    ],
                )
            )
        parsed = parsed.replace(tzinfo=None)
    moment = parsed.replace(tzinfo=zone)
    if not recurring:
        return server_text(moment, server)
    # A repeating event keeps its server wall-clock time on every occurrence.
    converted = parsed + offset(server, moment) - offset(zone, moment)
    constant = len(server_shifts(zone, server, datetime.now(UTC))) == 1
    if constant and converted.date() == parsed.date():
        return converted.isoformat(timespec="minutes")
    reason = (
        "falls on another day there"
        if converted.date() != parsed.date()
        else f"differs from {name} by an offset that changes during the year"
    )
    raise CalendarCallRefusedError(
        refusal(
            f"a repeating event keeps the same wall-clock time in the server time zone {server}, "
            f"which {reason}, so no server time matches {name} time on every occurrence. This "
            f"call uses the server time of {text} {name} time at the first occurrence.",
            arguments,
            **{field: converted.isoformat(timespec="minutes")},
        )
    )


def _apply_end(
    calendar_service: CalendarService, arguments: JsonObject, event: CalendarEvent | None
) -> JsonObject:
    """Turn an end into a duration: minutes for timed events, days for all-day ones."""
    result = dict(arguments)
    end = result.pop(END_FIELD, None)
    if not isinstance(end, str):
        return result
    start = result.get("start")
    if not isinstance(start, str) and event is not None:
        start = str(_event_fields(calendar_service, event)["start"])
    if not isinstance(start, str):
        raise CalendarCallRefusedError(
            refusal('"end" needs a start to measure from.', result, start=_STAND_INS["start"])
        )
    recurring = (
        result["rrule"] is not None
        if "rrule" in result
        else event is not None and event.rrule is not None
    )
    duration = _duration_between(calendar_service, start, end.strip(), recurring, result)
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
    calendar_service: CalendarService,
    start: str,
    end: str,
    recurring: bool,
    arguments: JsonObject,
) -> int:
    if is_date(start):
        if not is_date(end):
            raise CalendarCallRefusedError(
                refusal(
                    "an all-day event ends on a date; send its length in days as duration.",
                    arguments,
                    duration=_STAND_INS["duration"],
                )
            )
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if days < 0:
            raise CalendarCallRefusedError(
                refusal('"end" is before "start".', arguments, duration=_STAND_INS["duration"])
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
                    duration=_STAND_INS["duration"],
                )
            )
        finish = parsed_end
    server = _server_zone(calendar_service)
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
                duration=_STAND_INS["duration"],
            )
        )
    return int(seconds // 60)


def _apply_location(arguments: JsonObject, event: CalendarEvent | None) -> JsonObject:
    """Keep a location as the first line of notes: the calendar has no location field."""
    result = dict(arguments)
    location = result.pop(LOCATION_FIELD, None)
    if not isinstance(location, str):
        return result
    base = result.get("notes", event.notes if event else None)
    lines = [line for line in str(base or "").splitlines() if not line.startswith("Location: ")]
    result["notes"] = "\n".join([f"Location: {location.strip()}", *lines]).strip()
    return result


def _event_success(
    calendar_service: CalendarService, event: CalendarEvent, note: str | None
) -> JsonObject:
    data = _event_fields(calendar_service, event)
    if note:
        data["note"] = note
    return tool_success(data)


def _event_fields(calendar_service: CalendarService, event: CalendarEvent) -> JsonObject:
    """One event in the Agent-facing shape: server-local times, repetition as sent."""
    data: JsonObject = {"id": event.id, "title": event.title}
    if event.all_day:
        data["start"] = event.start_date
        data["days"] = event.duration_days or 1
    else:
        zone = ZoneInfo(event.tz_name) if event.tz_name else _server_zone(calendar_service)
        start_utc, end_utc = calendar_service.event_span(event)
        data["start"] = (
            _minute_text(event.start_local) if event.start_local else _local_text(start_utc, zone)
        )
        data["end"] = _local_text(end_utc, zone)
    if event.rrule is not None:
        # The rule as the Agent would send it: no nulls, no default interval.
        rule = {
            key: value
            for key, value in event.rrule.items()
            if value is not None and not (key == "interval" and value == 1)
        }
        if isinstance(rule.get("by_weekday"), list):
            rule["by_weekday"] = sorted(rule["by_weekday"], key=_WEEKDAY_ORDER.index)
        data["repeats"] = json.dumps(rule, separators=(",", ":"))
        if event.exdates:
            data["removed_occurrences"] = ", ".join(_minute_text(item) for item in event.exdates)
    if event.notes:
        data["notes"] = event.notes
    actions = len(calendar_service.actions.list_actions(event.id))
    if actions:
        data["actions"] = actions
    return data


# -- actions -------------------------------------------------------------------------------


def _handle_add_action(
    calendar_service: CalendarService, arguments: JsonObject, context: ToolContext | None
) -> JsonObject:
    event = calendar_service.get_event(str(arguments["id"]))
    missing = [name for name in ("when", "prompt") if name not in arguments]
    if missing:
        texts = {
            "when": '"when", relative to the event such as "start - 1h"',
            "prompt": '"prompt", the instruction the action\'s Run carries out',
        }
        raise CalendarCallRefusedError(
            refusal(
                "add_action needs " + " and ".join(texts[name] for name in missing) + ".",
                arguments,
                **{name: _STAND_INS[name] for name in missing},
            )
        )
    when, note = _relative_when(calendar_service, event, arguments)
    target = arguments.get("target")
    if not isinstance(target, str) and context is not None:
        target = format_agent_address(context.agent_id, context.project_id)
    target = str(target)
    action = calendar_service.actions.add(
        event.id,
        when=when,
        prompt=str(arguments["prompt"]),
        target=target,
        session=_session(arguments, context, target),
    )
    return _action_success(calendar_service, action, event, note)


def _handle_update_action(
    calendar_service: CalendarService, arguments: JsonObject, context: ToolContext | None
) -> JsonObject:
    action_id = str(arguments["id"])
    current = _action(calendar_service, action_id)
    event = calendar_service.get_event(current["event_id"])
    fields: dict[str, Any] = {}
    note = None
    if "when" in arguments:
        fields["when"], note = _relative_when(calendar_service, event, arguments)
    if "prompt" in arguments:
        fields["prompt"] = arguments["prompt"]
    if "target" in arguments:
        fields["target"] = arguments["target"]
    if "session" in arguments:
        target = str(fields.get("target", current["target"]))
        fields["session"] = _session(arguments, context, target)
    if not fields:
        raise CalendarCallRefusedError(
            refusal(
                "update_action needs a field to change: when, prompt, target or session.",
                arguments,
                when=_STAND_INS["when"],
            )
        )
    action = calendar_service.actions.update(action_id, **fields)
    return _action_success(calendar_service, action, event, note)


def _handle_delete_action(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    action_id = str(arguments["id"])
    current = _action(calendar_service, action_id)
    calendar_service.actions.delete(action_id)
    event = calendar_service.get_event(current["event_id"])
    return tool_success(
        {"id": action_id, "event": f"{event.title} ({event.id})", "status": "deleted"}
    )


def _action(calendar_service: CalendarService, action_id: str) -> dict[str, Any]:
    for item in calendar_service.actions.list_actions():
        if item["id"] == action_id:
            return item
    raise CalendarEventNotFoundError(f"Calendar action not found: {action_id}")


def _relative_when(
    calendar_service: CalendarService, event: CalendarEvent, arguments: JsonObject
) -> tuple[str, str | None]:
    """Keep a relative when; turn a clock time for a single event into one."""
    when = str(arguments["when"])
    moment = parse_local(when)
    if moment is None and not is_date(when):
        return when, None
    server = _server_zone(calendar_service)
    zone: tzinfo = server
    name = arguments.get(TIMEZONE_FIELD)
    if isinstance(name, str):
        found = named_zone(name)
        if found is None:
            raise CalendarCallRefusedError(_unknown_zone(name, server, arguments))
        zone = found
    if moment is None:
        moment = datetime.combine(date.fromisoformat(when), time.min)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    start = _occurrence_start_near(calendar_service, event, moment)
    minutes = round((moment - start).total_seconds() / 60)
    if minutes == 0:
        relative = "start"
    else:
        relative = f"start {'-' if minutes < 0 else '+'} {minutes_text(abs(minutes))}"
    if event.rrule is not None:
        raise CalendarCallRefusedError(
            refusal(
                f'"{when}" is a clock time, but an action repeats with every occurrence of this '
                f'event, so its when counts from the event. At the nearest occurrence, "{when}" '
                f'is "{relative}".',
                arguments,
                when=relative,
                timezone=OMIT,
            )
        )
    return relative, f'Read "{when}" as "{relative}".'


def _occurrence_start_near(
    calendar_service: CalendarService, event: CalendarEvent, near: datetime
) -> datetime:
    """The start of the occurrence closest to ``near``, as an aware moment."""
    server = _server_zone(calendar_service)
    occurrences = calendar_service.event_occurrences(
        event, (near - _ACTION_REACH).astimezone(UTC), (near + _ACTION_REACH).astimezone(UTC)
    )
    starts = [
        item.start_utc
        if item.start_utc is not None
        else datetime.combine(item.start_date or date.min, time.min, server)
        for item in occurrences
    ]
    if starts:
        return min(starts, key=lambda item: abs((item - near).total_seconds()))
    if event.all_day and event.start_date:
        return datetime.combine(date.fromisoformat(event.start_date), time.min, server)
    return calendar_service.event_span(event)[0]


def _session(arguments: JsonObject, context: ToolContext | None, target: str) -> str | None:
    session = arguments.get("session")
    if not isinstance(session, str):
        return None
    if session.strip().casefold().replace(" ", "") not in _CURRENT_SESSION_WORDS:
        return session
    if context is None:
        return None
    own = format_agent_address(context.agent_id, context.project_id)
    if target != own:
        raise CalendarCallRefusedError(
            refusal(
                f"the current Session belongs to {own}, not to the target {target}. Omit "
                "session for a fresh Session each time.",
                arguments,
                session=OMIT,
            )
        )
    return context.session_id


def _action_success(
    calendar_service: CalendarService,
    action: dict[str, Any],
    event: CalendarEvent,
    note: str | None,
) -> JsonObject:
    zone = _server_zone(calendar_service)
    data: JsonObject = {
        "id": action["id"],
        "event": f"{event.title} ({event.id})",
        "when": action["when"],
        "target": action["target"],
        "session": action.get("session") or "a fresh Session each time",
    }
    notes = [note] if note else []
    due = _next_due(calendar_service, action, event)
    if due is None:
        notes.append("No occurrence of the event lies ahead, so the action will not run.")
    else:
        data["next_due"] = _local_text(due, zone)
    if notes:
        data["note"] = " ".join(notes)
    return tool_success(data)


def _next_due(
    calendar_service: CalendarService, action: dict[str, Any], event: CalendarEvent
) -> datetime | None:
    now = datetime.now(UTC)
    if event.all_day and event.start_date:
        first = datetime.combine(
            date.fromisoformat(event.start_date), time.min, _server_zone(calendar_service)
        )
    else:
        first = calendar_service.event_span(event)[0]
    # From now, or from the first occurrence of an event that lies further ahead.
    begin = max(now - _ACTION_REACH, first.astimezone(UTC) - timedelta(days=1))
    occurrences = calendar_service.event_occurrences(
        event, begin, begin + timedelta(days=_UPCOMING_DAYS)
    )
    dues = [
        _instant(row["scheduled_at"])
        for row in calendar_service.actions.project(occurrences)
        if row["action_id"] == action["id"]
        and row["status"] == "pending"
        and _instant(row["expires_at"]) > now
    ]
    return min(dues) if dues else None


# -- rendering -------------------------------------------------------------------------------


def _validation_message(arguments: JsonObject, error: CalendarValidationError) -> str:
    detail = str(error).rstrip(". ")
    if "target does not identify" in detail:
        return refusal(f"{detail}. {_TARGET_GUIDANCE}", arguments, target=_STAND_INS["target"])
    if "session does not exist" in detail:
        return refusal(
            f"{detail}. Omit session for a fresh Session each time.", arguments, session=OMIT
        )
    field = _FIELD_WORDS.get(detail.split(" ", 1)[0].split(".", 1)[0])
    if field is None and detail.startswith("cannot parse when"):
        field = "when"
    if field is not None and field in arguments:
        stand_in = (
            "this week"
            if field == "when" and arguments.get("action") in {"list", "find_free"}
            else _STAND_INS[field]
        )
        return refusal(f"{detail}.", arguments, **{field: stand_in})
    return f"calendar was not run: {detail}."


def _unknown_zone(name: str, server: ZoneInfo, arguments: JsonObject) -> str:
    return refusal(
        f'"timezone" "{name}" is not a known time zone. Use an IANA name such as '
        f'"Europe/Berlin", or omit it for the server time zone {server}.',
        arguments,
        timezone=OMIT,
    )


def _window_text(start: datetime, end: datetime, zone: ZoneInfo) -> str:
    first, last = start.astimezone(zone), end.astimezone(zone)
    if first.time() == time.min and last.time() == time.min:
        final = (last - timedelta(days=1)).date()
        if final == first.date():
            return first.date().isoformat()
        return f"{first.date().isoformat()} to {final.isoformat()}"
    return f"{_local_text(start, zone)} to {_local_text(end, zone)}"


def _local_text(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).replace(tzinfo=None).isoformat(timespec="minutes")


def _minute_text(value: str) -> str:
    """Drop zero seconds from a naive ISO time: 2030-01-10T15:00:00 -> 2030-01-10T15:00."""
    return value[:-3] if len(value) == 19 and value.endswith(":00") else value


def _length_text(minutes: int) -> str:
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins else ""]
    return " ".join(part for part in parts if part) or "0m"


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


def _server_zone(calendar_service: CalendarService) -> ZoneInfo:
    return ZoneInfo(calendar_service.system_timezone_name())


def _calendar_display_parts(raw_arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    # Persisted calls keep the Model's own spelling; label what the call meant.
    try:
        arguments = _normalize_calendar_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        return ()
    action = arguments.get("action")
    if not isinstance(action, str) or action not in CALENDAR_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    for field_name in ("title", "id", "when"):
        value = arguments.get(field_name)
        if isinstance(value, str) and value.strip():
            kind = "identifier" if field_name == "id" else "text"
            truncate = "middle" if kind == "identifier" else "end"
            parts.append(ToolDisplayPart(value.strip(), kind=kind, truncate=truncate))
            break
    return tuple(parts)


__all__ = [
    "CALENDAR_ACTIONS",
    "CALENDAR_TOOL_DESCRIPTION",
    "CALENDAR_TOOL_NAME",
    "CALENDAR_TOOL_PARAMETERS",
    "register_calendar_tool",
]
