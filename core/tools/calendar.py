"""Built-in calendar tool for managing the user's local calendar."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarServiceError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar.when import looks_like_date
from core.tools.arguments import optional_int, optional_string, required_string
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
    from core.calendar import CalendarEvent, CalendarService, EventOccurrence, FreeSlot

CALENDAR_TOOL_NAME = "calendar"
CALENDAR_TOOL_DESCRIPTION = (
    "Manage the calendar: list events with their occurrences, create single or repeating "
    "events, update or delete them, and find free slots."
)

CALENDAR_ACTIONS = frozenset(("list", "create", "update", "delete", "find_free"))

_LIST_ARGUMENTS = frozenset({"when"})
_CREATE_ARGUMENTS = frozenset({"title", "start", "duration", "rrule", "notes"})
_UPDATE_ARGUMENTS = frozenset({"id", "title", "start", "duration", "rrule", "notes"})
_DELETE_ARGUMENTS = frozenset({"id", "start"})
_FIND_FREE_ARGUMENTS = frozenset({"when", "duration"})
_ACTION_ARGUMENTS: dict[str, frozenset[str]] = {
    "list": _LIST_ARGUMENTS,
    "create": _CREATE_ARGUMENTS,
    "update": _UPDATE_ARGUMENTS,
    "delete": _DELETE_ARGUMENTS,
    "find_free": _FIND_FREE_ARGUMENTS,
}
_ACTION_RECOMMENDATIONS = {
    "list": 'Use {"action":"list","when":"this week"}',
    "create": (
        'Use {"action":"create","title":"Dentist","start":"2026-09-10T15:00"} for a timed '
        'event or {"action":"create","title":"Trip","start":"2026-09-10","duration":3} for '
        "an all-day event"
    ),
    "update": (
        'Use {"action":"update","id":"<event-id>","start":"2026-09-10T16:00"}; include only '
        "fields that should change"
    ),
    "delete": (
        'Use {"action":"delete","id":"<event-id>"} for the whole event; add the occurrence '
        '"start" from list to remove one occurrence of a repeating event'
    ),
    "find_free": 'Use {"action":"find_free","when":"next week","duration":60}',
}

_DEFAULT_EVENT_DURATION_MINUTES = 60
_DEFAULT_ALL_DAY_DURATION_DAYS = 1
_DEFAULT_FREE_SLOT_MINUTES = 60
_DEFAULT_FREE_WINDOW_DAYS = 7
_ONE_DAY = timedelta(days=1)

_CALENDAR_WHEN_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Time window for list and find_free: today, tomorrow, this week, next week, "
        "this month, next month, a date, a year-month, or 'start..end'. Omit for the "
        "current month on list and the next 7 days on find_free."
    ),
}
_CALENDAR_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Event id from a previous list. Required for update and delete.",
}
_CALENDAR_TITLE_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": ("Event title. Required for create; omit on update to keep the current title."),
}
_CALENDAR_START_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Required for create; omit on update to keep the current start. A date "
        "(YYYY-MM-DD) makes the event all-day, a datetime makes it timed. On delete of a "
        "repeating event, one occurrence's start from list removes only that occurrence; "
        "omit to delete the whole event."
    ),
}
_CALENDAR_DURATION_PARAMETER: JsonObject = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "Length in minutes for a timed event, days for an all-day event, minutes for "
        "find_free slots. Omit for the default (60 minutes, 1 day, 60-minute slots) and "
        "on update to keep the current length."
    ),
}
_CALENDAR_RRULE_PARAMETER: JsonObject = {
    "type": ["object", "null"],
    "description": (
        "Repeat rule for create; omit for a single event and on update to keep the "
        "current rule; null on update stops repeating. Object: freq (daily, weekly, "
        "monthly, or yearly), optional interval (default 1), optional end as count or "
        "until (inclusive date, not both), and by_weekday for weekly rules (list from "
        "mo, tu, we, th, fr, sa, su)."
    ),
}
_CALENDAR_NOTES_PARAMETER: JsonObject = {
    "type": "string",
    "description": "Free-text notes. Omit on update to keep the current notes.",
}

CALENDAR_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "create", "update", "delete", "find_free"],
            "description": "Calendar action to perform.",
        },
        "when": _CALENDAR_WHEN_PARAMETER,
        "id": _CALENDAR_ID_PARAMETER,
        "title": _CALENDAR_TITLE_PARAMETER,
        "start": _CALENDAR_START_PARAMETER,
        "duration": _CALENDAR_DURATION_PARAMETER,
        "rrule": _CALENDAR_RRULE_PARAMETER,
        "notes": _CALENDAR_NOTES_PARAMETER,
    },
    "required": ["action"],
}

_LOGGER = get_logger("tools.calendar")


def register_calendar_tool(registry: ToolRegistry, calendar_service: CalendarService) -> None:
    """Register the calendar tool with a vBot tool registry."""

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _handle_calendar_tool(calendar_service, arguments)

    registry.register(
        CALENDAR_TOOL_NAME,
        CALENDAR_TOOL_DESCRIPTION,
        CALENDAR_TOOL_PARAMETERS,
        handler,
        open_input_schema=True,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_calendar_display_parts,
            fact_builder=result_count_fact_builder(
                "occurrences", when_arguments={"action": "list"}
            ),
        ),
    )


def _handle_calendar_tool(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    raw_action = arguments.get("action")
    if not isinstance(raw_action, str) or raw_action not in CALENDAR_ACTIONS:
        options = ", ".join(sorted(CALENDAR_ACTIONS))
        return tool_failure(
            "invalid_arguments",
            f"action must be one of: {options}. {_ACTION_RECOMMENDATIONS['list']}",
            retryable=False,
        )
    action = raw_action
    operation_arguments = dict(arguments)
    operation_arguments.pop("action", None)

    unknown_arguments = sorted(set(operation_arguments) - _ACTION_ARGUMENTS[action])
    if unknown_arguments:
        names = ", ".join(unknown_arguments)
        allowed = ", ".join(sorted(_ACTION_ARGUMENTS[action])) or "no additional fields"
        return tool_failure(
            "invalid_arguments",
            _with_action_recommendation(
                action,
                f"Action '{action}' does not accept: {names}. Allowed: {allowed}",
            ),
            retryable=False,
        )

    try:
        if action == "list":
            return _handle_list(calendar_service, operation_arguments)
        if action == "create":
            return _handle_create(calendar_service, operation_arguments)
        if action == "update":
            return _handle_update(calendar_service, operation_arguments)
        if action == "delete":
            return _handle_delete(calendar_service, operation_arguments)
        return _handle_find_free(calendar_service, operation_arguments)
    except (ValueError, CalendarValidationError) as error:
        return tool_failure(
            "invalid_arguments",
            _with_action_recommendation(action, str(error)),
            retryable=False,
        )
    except CalendarEventNotFoundError as error:
        return tool_failure(
            "event_not_found",
            f'{error}. Use {{"action":"list"}} to get current event ids',
            retryable=False,
        )
    except CalendarStorageError as error:
        _LOGGER.warning("Calendar storage error for action=%s: %s", action, error)
        return tool_failure(
            "calendar_storage_error",
            f"{error}. Do not repeat the same call unchanged",
            retryable=False,
        )
    except CalendarServiceError as error:
        _LOGGER.warning("Calendar service error for action=%s: %s", action, error)
        return tool_failure(
            "calendar_service_error",
            f"{error}. Do not repeat the same call unchanged",
            retryable=False,
        )


def _handle_list(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    window_start, window_end = _resolve_window(calendar_service, arguments, "this month")
    occurrences = calendar_service.occurrences_in_window(window_start, window_end)
    listed_ids = {occurrence.event_id for occurrence in occurrences}
    events = [
        _event_payload(event, calendar_service)
        for event in calendar_service.list_events()
        if event.id in listed_ids
    ]
    return tool_success(
        {
            "events": events,
            "occurrences": [_occurrence_payload(occurrence) for occurrence in occurrences],
            "system_timezone": calendar_service.system_timezone_name(),
        }
    )


def _handle_create(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    title = required_string(arguments.get("title"), field_name="title")
    start = required_string(arguments.get("start"), field_name="start")
    duration = optional_int(arguments.get("duration"), field_name="duration", minimum=1)
    rrule = _validate_rrule(arguments["rrule"], allow_null=False) if "rrule" in arguments else None
    notes = optional_string(arguments.get("notes"), field_name="notes")
    all_day = looks_like_date(start)
    event = calendar_service.create_event(
        title=title,
        start=start,
        duration_minutes=None if all_day else duration,
        duration_days=duration if all_day else None,
        rrule=rrule,
        notes=notes,
    )
    return tool_success({"event": _event_payload(event, calendar_service)})


def _handle_update(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    event_id = required_string(arguments.get("id"), field_name="id")
    updates: JsonObject = {}
    if "title" in arguments:
        updates["title"] = required_string(arguments.get("title"), field_name="title")
    if "start" in arguments:
        start = required_string(arguments.get("start"), field_name="start")
        updates["start"] = start
        # The start form decides the event kind; pass it explicitly so switching
        # between all-day and timed works without an all_day parameter.
        updates["all_day"] = looks_like_date(start)
    if "duration" in arguments:
        duration = optional_int(arguments.get("duration"), field_name="duration", minimum=1)
        if _update_targets_all_day(calendar_service, event_id, arguments):
            updates["duration_days"] = duration
        else:
            updates["duration_minutes"] = duration
    if "rrule" in arguments:
        updates["rrule"] = _validate_rrule(arguments.get("rrule"), allow_null=True)
    if "notes" in arguments:
        updates["notes"] = optional_string(arguments.get("notes"), field_name="notes")
    if not updates:
        raise ValueError("update requires at least one field to change")

    event = calendar_service.update_event(event_id, **updates)
    return tool_success({"event": _event_payload(event, calendar_service)})


def _handle_delete(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    event_id = required_string(arguments.get("id"), field_name="id")
    occurrence_start = optional_string(arguments.get("start"), field_name="start")
    if occurrence_start is None:
        calendar_service.delete_event(event_id)
        return tool_success({"id": event_id, "deleted": True})
    calendar_service.add_exdate(event_id, occurrence_start)
    return tool_success({"id": event_id, "excluded_occurrence": occurrence_start})


def _handle_find_free(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    duration = optional_int(
        arguments.get("duration"),
        field_name="duration",
        default=_DEFAULT_FREE_SLOT_MINUTES,
        minimum=1,
    )
    window_start, window_end = _resolve_window(calendar_service, arguments, "today")
    if "when" not in arguments:
        window_end = window_start + _DEFAULT_FREE_WINDOW_DAYS * _ONE_DAY
    slots = calendar_service.find_free_slots(window_start, window_end, duration)
    zone = _server_zone(calendar_service)
    return tool_success(
        {
            "slots": [_free_slot_payload(slot, zone) for slot in slots],
            "system_timezone": calendar_service.system_timezone_name(),
        }
    )


def _resolve_window(
    calendar_service: CalendarService, arguments: JsonObject, default_when: str
) -> tuple[datetime, datetime]:
    when = optional_string(arguments.get("when"), field_name="when")
    if when is None:
        when = default_when
    return calendar_service.resolve_when(when)


def _update_targets_all_day(
    calendar_service: CalendarService, event_id: str, arguments: JsonObject
) -> bool:
    start = optional_string(arguments.get("start"), field_name="start")
    if start is not None:
        return looks_like_date(start)
    return bool(calendar_service.get_event(event_id).all_day)


def _validate_rrule(value: object, *, allow_null: bool) -> object:
    if value is None:
        if allow_null:
            return None
        raise ValueError("rrule must be an object when provided; omit it for a single event")
    if not isinstance(value, dict):
        raise ValueError("rrule must be an object with a freq field")
    return value


def _event_payload(event: CalendarEvent, calendar_service: CalendarService) -> JsonObject:
    """Render one event record in the agent-facing shape (server-local times)."""
    payload: JsonObject = {
        "id": event.id,
        "title": event.title,
        "all_day": event.all_day,
        "recurring": event.rrule is not None,
        "notes": event.notes,
        "rrule": event.rrule,
    }
    if event.all_day:
        payload["start"] = event.start_date
        payload["duration"] = event.duration_days or _DEFAULT_ALL_DAY_DURATION_DAYS
        return payload
    zone = _server_zone(calendar_service)
    start_local = _instant_to_local(event, zone)
    payload["start"] = start_local
    payload["end"] = _local_end(start_local, event.duration_minutes)
    return payload


def _instant_to_local(event: CalendarEvent, zone: ZoneInfo) -> str:
    if event.start_local is not None:
        return event.start_local
    assert event.start_utc is not None
    start_utc = datetime.fromisoformat(event.start_utc)
    return start_utc.astimezone(zone).replace(tzinfo=None, microsecond=0).isoformat()


def _local_end(start_local: str, duration_minutes: int | None) -> str:
    start = datetime.fromisoformat(start_local)
    end = start + timedelta(minutes=duration_minutes or _DEFAULT_EVENT_DURATION_MINUTES)
    return end.replace(microsecond=0).isoformat()


def _occurrence_payload(occurrence: EventOccurrence) -> JsonObject:
    payload: JsonObject = {
        "event_id": occurrence.event_id,
        "title": occurrence.title,
        "start": occurrence.occurrence_start,
        "all_day": occurrence.all_day,
        "recurring": occurrence.recurring,
    }
    if occurrence.occurrence_end is not None:
        payload["end"] = occurrence.occurrence_end
    return payload


def _free_slot_payload(slot: FreeSlot, zone: ZoneInfo) -> JsonObject:
    return {
        "start": _to_local_naive(slot.start_utc, zone),
        "end": _to_local_naive(slot.end_utc, zone),
    }


def _to_local_naive(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).replace(tzinfo=None, microsecond=0).isoformat()


def _server_zone(calendar_service: CalendarService) -> ZoneInfo:
    return ZoneInfo(calendar_service.system_timezone_name())


def _with_action_recommendation(action: str, message: str) -> str:
    recommendation = _ACTION_RECOMMENDATIONS[action]
    return f"{message.rstrip('. ')}. {recommendation}"


def _calendar_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
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
