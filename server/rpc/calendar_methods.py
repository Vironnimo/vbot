"""Calendar RPC handlers."""

from __future__ import annotations

from typing import Any

from core.calendar import CalendarService
from server.events import RESOURCE_KIND_CALENDAR
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import (
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
    _required_string,
)

JsonObject = dict[str, Any]

_WINDOW_FIELDS = frozenset({"from", "to"})
_EVENT_MUTATION_FIELDS = frozenset(
    {
        "title",
        "start",
        "all_day",
        "duration_minutes",
        "duration_days",
        "rrule",
        "exdates",
        "notes",
    }
)
_CREATE_FIELDS = _EVENT_MUTATION_FIELDS
_UPDATE_FIELDS = _EVENT_MUTATION_FIELDS | {"id"}
_DELETE_FIELDS = frozenset({"id"})
_ADD_EXDATE_FIELDS = frozenset({"id", "occurrence_start"})


def _calendar_service(state: Any) -> CalendarService:
    service: CalendarService = state.runtime.calendar_service
    return service


def _calendar_window(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _WINDOW_FIELDS, "calendar.window")
    service = _calendar_service(state)
    window_start_value = _required_string(params, "from")
    window_end_value = _required_string(params, "to")
    try:
        window_start, window_end = service.parse_window(window_start_value, window_end_value)
        occurrences = service.occurrences_in_window(window_start, window_end)
        events = service.list_events()
        cron_occurrences = state.runtime.cron_service.project_occurrences(window_start, window_end)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "occurrences": [_occurrence_payload(occurrence) for occurrence in occurrences],
        "events": [_event_payload(event) for event in events],
        "cron": [_cron_occurrence_payload(occurrence) for occurrence in cron_occurrences],
        "system_timezone": service.system_timezone_name(),
        "actions": service.actions.list_actions(),
        "executions": service.actions.project(occurrences),
        "action_error": service.actions.storage_error,
    }


def _calendar_create(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _CREATE_FIELDS, "calendar.create")
    service = _calendar_service(state)
    title = _required_string(params, "title")
    start = _required_string(params, "start")
    all_day = _optional_bool_value(params.get("all_day"), "all_day")
    duration_minutes = _optional_positive_integer(params, "duration_minutes")
    duration_days = _optional_positive_integer(params, "duration_days")
    rrule = _optional_object(params.get("rrule"), "rrule")
    exdates = _optional_string_list_value(params.get("exdates"), "exdates")
    notes = _optional_string(params, "notes")
    try:
        event = service.create_event(
            title=title,
            start=start,
            all_day=all_day,
            duration_minutes=duration_minutes,
            duration_days=duration_days,
            rrule=rrule,
            exdates=exdates,
            notes=notes,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    result: JsonObject = {"event": _event_payload(event)}
    return result


def _calendar_update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _UPDATE_FIELDS, "calendar.update")
    service = _calendar_service(state)
    event_id = _required_string(params, "id")
    updates: JsonObject = {}
    if "title" in params:
        updates["title"] = _required_string(params, "title")
    if "start" in params:
        updates["start"] = _required_string(params, "start")
    if "all_day" in params:
        updates["all_day"] = _optional_bool_value(params.get("all_day"), "all_day")
    if "duration_minutes" in params:
        updates["duration_minutes"] = _optional_positive_integer(params, "duration_minutes")
    if "duration_days" in params:
        updates["duration_days"] = _optional_positive_integer(params, "duration_days")
    if "rrule" in params:
        updates["rrule"] = _optional_object(params.get("rrule"), "rrule")
    if "exdates" in params:
        updates["exdates"] = _optional_string_list_value(params.get("exdates"), "exdates")
    if "notes" in params:
        updates["notes"] = _optional_string(params, "notes")
    try:
        event = service.update_event(event_id, **updates)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    return {"event": _event_payload(event)}


def _calendar_delete(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _DELETE_FIELDS, "calendar.delete")
    service = _calendar_service(state)
    event_id = _required_string(params, "id")
    try:
        service.delete_event(event_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    return {"id": event_id, "deleted": True}


def _calendar_add_exdate(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _ADD_EXDATE_FIELDS, "calendar.add_exdate")
    service = _calendar_service(state)
    event_id = _required_string(params, "id")
    occurrence_start = _required_string(params, "occurrence_start")
    try:
        event = service.add_exdate(event_id, occurrence_start)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    return {"event": _event_payload(event)}


def _event_payload(event: Any) -> JsonObject:
    payload: JsonObject = event.to_dict()
    payload["recurring"] = event.rrule is not None
    return payload


def _occurrence_payload(occurrence: Any) -> JsonObject:
    return {
        "event_id": occurrence.event_id,
        "title": occurrence.title,
        "all_day": occurrence.all_day,
        "recurring": occurrence.recurring,
        "notes": occurrence.notes,
        "start_utc": occurrence.start_utc.isoformat() if occurrence.start_utc else None,
        "end_utc": occurrence.end_utc.isoformat() if occurrence.end_utc else None,
        "start_date": occurrence.start_date.isoformat() if occurrence.start_date else None,
        "end_date": occurrence.end_date.isoformat() if occurrence.end_date else None,
        "occurrence_start": occurrence.occurrence_start,
    }


def _cron_occurrence_payload(occurrence: Any) -> JsonObject:
    return {
        "job_id": occurrence.job_id,
        "name": occurrence.name,
        "fire_at": occurrence.fire_at_utc.isoformat(),
        "schedule_type": occurrence.schedule_type,
    }


def _optional_bool_value(value: Any, key: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


def _optional_object(value: Any, key: str) -> JsonObject | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be an object")
    return value


def _optional_string_list_value(value: Any, key: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of strings")
    return value


async def _calendar_add_action(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, frozenset({"id", "when", "prompt", "target", "session"}), "calendar.add_action"
    )
    async with _agent_reference_lock(state):
        try:
            result = _calendar_service(state).actions.add(
                _required_string(params, "id"),
                when=_required_string(params, "when"),
                prompt=_required_string(params, "prompt"),
                target=_required_string(params, "target"),
                session=_optional_string(params, "session"),
            )
        except Exception as exc:
            raise _map_expected_error(exc) from exc
    return {"action": result}


async def _calendar_update_action(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, frozenset({"id", "when", "prompt", "target", "session"}), "calendar.update_action"
    )
    async with _agent_reference_lock(state):
        try:
            result = _calendar_service(state).actions.update(
                _required_string(params, "id"),
                **{key: value for key, value in params.items() if key != "id"},
            )
        except Exception as exc:
            raise _map_expected_error(exc) from exc
    return {"action": result}


def _calendar_delete_action(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _DELETE_FIELDS, "calendar.delete_action")
    action_id = _required_string(params, "id")
    try:
        _calendar_service(state).actions.delete(action_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"id": action_id, "deleted": True}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return calendar RPC handlers."""

    return {
        "calendar.window": _calendar_window,
        "calendar.create": _calendar_create,
        "calendar.update": _calendar_update,
        "calendar.delete": _calendar_delete,
        "calendar.add_exdate": _calendar_add_exdate,
        "calendar.add_action": _calendar_add_action,
        "calendar.update_action": _calendar_update_action,
        "calendar.delete_action": _calendar_delete_action,
    }
