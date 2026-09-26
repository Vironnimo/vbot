"""Built-in calendar tool for managing the user's local calendar."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarServiceError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar.service import FIND_FREE_MAX_RESULTS
from core.tools._calendar_actions import (
    action_lines,
    find_action,
    handle_add_action,
    handle_delete_action,
    handle_update_action,
)
from core.tools._calendar_arguments import (
    LOCATION_FIELD,
    OMIT,
    QUERY_FIELD,
    STAND_INS,
    TIMEZONE_FIELD,
    UNADVERTISED_PARAMETERS,
    WHEN_STAND_IN,
    CalendarCallRefusedError,
    choice,
    is_date,
    normalize_calendar_arguments,
    parse_local,
    refusal,
    render_call,
)
from core.tools._calendar_times import (
    apply_end,
    apply_length,
    apply_timezone,
    length_text,
    local_text,
    minute_text,
    named_instant,
    read_window,
    server_zone,
    unknown_zone,
    window_text,
)
from core.tools._named_zones import named_zone
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
    "local to the server time zone, shown by list and by Runtime Environment when present. An "
    "action starts a Run of its target Agent with prompt plus the event's title, time and "
    "notes; it moves with its event and is deleted with it. For schedules that should not "
    "appear in the calendar, use cron if available."
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
_DEFAULT_FREE_MINUTES = 60
_NEARBY = timedelta(days=7)
_LISTED_OCCURRENCES = 8
_TITLE_MATCHES = 5
_WEEKDAY_ORDER = ("mo", "tu", "we", "th", "fr", "sa", "su")
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
            return handle_add_action(calendar_service, arguments, context)
        if action == "update_action":
            return handle_update_action(calendar_service, arguments, context)
        return handle_delete_action(calendar_service, arguments)
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
        current = find_action(calendar_service, item_id)
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
    zone = server_zone(calendar_service)
    window_start, window_end, note = read_window(arguments, zone, "this month")
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
        "window": window_text(window_start, window_end, zone),
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
    zone = server_zone(calendar_service)
    arguments, length_note = apply_length(arguments, zone, stored_start=None, recurring=True)
    duration = arguments.get("duration", _DEFAULT_FREE_MINUTES)
    window_start, window_end, note = read_window(arguments, zone, None)
    slots = calendar_service.find_free_slots(
        window_start, window_end, duration, max_results=FIND_FREE_MAX_RESULTS + 1
    )
    shown = slots[:FIND_FREE_MAX_RESULTS]
    data: JsonObject = {
        "free": len(shown),
        "window": window_text(window_start, window_end, zone),
        "timezone": calendar_service.system_timezone_name(),
    }
    notes = [text for text in (length_note, note) if text]
    if not shown:
        notes.append(f"No free span of {length_text(duration)} or more in this window.")
    elif len(slots) > len(shown):
        notes.append(
            f"More free time follows after {local_text(shown[-1].end_utc, zone)}; a later "
            "when shows it."
        )
    if notes:
        data["note"] = " ".join(notes)
    if shown:
        data["content"] = "\n".join(
            f"{local_text(slot.start_utc, zone)} to {local_text(slot.end_utc, zone)} "
            f"({length_text(int((slot.end_utc - slot.start_utc).total_seconds() // 60))})"
            for slot in shown
        )
    return tool_success(data)


def _event_block(
    calendar_service: CalendarService,
    event: CalendarEvent,
    occurrences: list[EventOccurrence],
    actions: list[dict[str, Any]],
    runs: dict[str, list[dict[str, Any]]],
) -> str:
    zone = server_zone(calendar_service)
    fields = _event_fields(calendar_service, event)
    fields.pop("actions", None)
    notes = fields.pop("notes", None)
    lines = [f"{key}: {value}" for key, value in fields.items()]
    if event.rrule is not None:
        lines.append(_occurrence_line(occurrences))
    if notes:
        lines.append("notes: " + "\n  ".join(str(notes).splitlines()))
    for item in actions:
        lines.extend(action_lines(item, runs.get(item["id"], []), zone))
    return "\n".join(lines)


def _occurrence_line(occurrences: list[EventOccurrence]) -> str:
    starts = [minute_text(item.occurrence_start) for item in occurrences]
    if not starts:
        return "occurrences: none in this window"
    if len(starts) <= _LISTED_OCCURRENCES:
        return "occurrences: " + ", ".join(starts)
    return f"occurrences: {len(starts)} in this window, {', '.join(starts[:3])}, ..., {starts[-1]}"


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
                **{name: STAND_INS[name] for name in missing},
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
    arguments, note = _event_times(calendar_service, arguments, None)
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
    arguments, note = _event_times(calendar_service, arguments, event)
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
                start=STAND_INS["start"],
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
            "removed_occurrence": minute_text(occurrence),
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
    server = server_zone(calendar_service)
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
                raise CalendarCallRefusedError(unknown_zone(name, server, arguments))
            parsed = named_instant(arguments, "start", parsed, name, zone)
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
        render_call(arguments, start=minute_text(item), **corrected) for item in sorted(closest)
    ]
    raise CalendarCallRefusedError(
        choice(f'"{start}" is not an occurrence of this event. Nearby occurrences:', calls)
    )


def _seconds(value: str) -> float:
    return (datetime.fromisoformat(value) - datetime(2000, 1, 1)).total_seconds()


def _event_times(
    calendar_service: CalendarService, arguments: JsonObject, event: CalendarEvent | None
) -> tuple[JsonObject, str | None]:
    """Read a call's zone, end and location into the fields the calendar stores."""
    server = server_zone(calendar_service)
    recurring = (
        arguments["rrule"] is not None
        if "rrule" in arguments
        else event is not None and event.rrule is not None
    )
    arguments, zone_note = apply_timezone(arguments, server, recurring=recurring)
    stored_start = _event_start(calendar_service, event) if event is not None else None
    arguments, length_note = apply_length(
        arguments, server, stored_start=stored_start, recurring=recurring
    )
    arguments = apply_end(arguments, server, stored_start=stored_start, recurring=recurring)
    note = " ".join(text for text in (zone_note, length_note) if text) or None
    return _apply_location(arguments, event), note


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
    data: JsonObject = {
        "id": event.id,
        "title": event.title,
        "start": _event_start(calendar_service, event),
    }
    if event.all_day:
        data["days"] = event.duration_days or 1
    else:
        zone = ZoneInfo(event.tz_name) if event.tz_name else server_zone(calendar_service)
        data["end"] = local_text(calendar_service.event_span(event)[1], zone)
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
            data["removed_occurrences"] = ", ".join(minute_text(item) for item in event.exdates)
    if event.notes:
        data["notes"] = event.notes
    actions = len(calendar_service.actions.list_actions(event.id))
    if actions:
        data["actions"] = actions
    return data


def _event_start(calendar_service: CalendarService, event: CalendarEvent) -> str:
    """The event's start as the Agent sees and sends it: a date, or a server-local time."""
    if event.all_day:
        return event.start_date or ""
    if event.start_local:
        return minute_text(event.start_local)
    zone = ZoneInfo(event.tz_name) if event.tz_name else server_zone(calendar_service)
    return local_text(calendar_service.event_span(event)[0], zone)


# -- rendering -------------------------------------------------------------------------------


def _validation_message(arguments: JsonObject, error: CalendarValidationError) -> str:
    detail = str(error).rstrip(". ")
    if "target does not identify" in detail:
        return refusal(f"{detail}. {_TARGET_GUIDANCE}", arguments, target=STAND_INS["target"])
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
            else STAND_INS[field]
        )
        return refusal(f"{detail}.", arguments, **{field: stand_in})
    return f"calendar was not run: {detail}."


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
