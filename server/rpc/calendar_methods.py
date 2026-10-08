"""Calendar RPC handlers."""

from __future__ import annotations

from typing import Any

from core.calendar import CalendarService, parse_occurrence_id
from server.events import RESOURCE_KIND_CALENDAR
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported, _required_string

JsonObject = dict[str, Any]

_WINDOW_FIELDS = frozenset({"from", "to"})
# Texts a call may clear with null or "".
_TEXT_FIELDS = ("description", "location", "rrule")
_EVENT_FIELDS = frozenset({"title", "start", "end", *_TEXT_FIELDS})
_CREATE_FIELDS = _EVENT_FIELDS
_UPDATE_FIELDS = _EVENT_FIELDS | {"id"}
_DELETE_FIELDS = frozenset({"id"})


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
    }


def _calendar_create(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _CREATE_FIELDS, "calendar.create")
    service = _calendar_service(state)
    fields = _event_fields(params)
    title = _required_string(params, "title")
    start = _required_string(params, "start")
    fields.pop("title")
    fields.pop("start")
    try:
        event = service.create_event(title=title, start=start, actor="rpc", **fields)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    return {"event": _event_payload(event)}


async def _calendar_update(state: Any, params: JsonObject) -> JsonObject:
    """Change a whole event, or with an occurrence id one occurrence of a repeating event."""
    _reject_unsupported(params, _UPDATE_FIELDS, "calendar.update")
    service = _calendar_service(state)
    item_id = _required_string(params, "id")
    fields = _event_fields(params)
    occurrence = parse_occurrence_id(item_id) is not None
    if occurrence and "rrule" in fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.rrule changes a whole event; send the event id, not an occurrence id",
        )
    try:
        # A moved event can let a Cron job that no longer fires run again; its target
        # check and the change must not interleave with a reference removal.
        async with _agent_reference_lock(state):
            if occurrence:
                changed = await service.update_occurrence(item_id, actor="rpc", **fields)
                result: JsonObject = {
                    "occurrence": _occurrence_payload(changed),
                    "event": _event_payload(service.get_event(changed.event_id)),
                }
            else:
                event = await service.update_event(item_id, actor="rpc", **fields)
                result = {"event": _event_payload(event)}
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    return result


async def _calendar_delete(state: Any, params: JsonObject) -> JsonObject:
    """Delete a whole event with its Cron jobs, or with an occurrence id one occurrence."""
    _reject_unsupported(params, _DELETE_FIELDS, "calendar.delete")
    service = _calendar_service(state)
    item_id = _required_string(params, "id")
    try:
        if parse_occurrence_id(item_id) is not None:
            # Without the occurrence, an earlier one's Cron job may catch up again.
            async with _agent_reference_lock(state):
                removed = await service.delete_occurrence(item_id, actor="rpc")
            result: JsonObject = {
                "id": item_id,
                "deleted": True,
                "event": _event_payload(service.get_event(removed.event_id)),
            }
        else:
            jobs = await service.delete_event(item_id, actor="rpc")
            result = {
                "id": item_id,
                "deleted": True,
                "cron_jobs": [{"id": job.id, "name": job.name} for job in jobs],
            }
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    return result


def _event_fields(params: JsonObject) -> JsonObject:
    """The event fields of a call; null or "" clears a text field."""
    fields: JsonObject = {}
    for key in ("title", "start", "end"):
        if key in params:
            fields[key] = _required_string(params, key)
    for key in _TEXT_FIELDS:
        if key in params:
            value = params[key]
            if value is not None and not isinstance(value, str):
                raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a string or null")
            fields[key] = value or None
    return fields


def _event_payload(event: Any) -> JsonObject:
    payload: JsonObject = event.to_dict()
    payload["all_day"] = event.all_day
    payload["recurring"] = event.recurring
    return payload


def _occurrence_payload(occurrence: Any) -> JsonObject:
    return {
        "id": occurrence.id,
        "event_id": occurrence.event_id,
        "title": occurrence.title,
        "description": occurrence.description,
        "location": occurrence.location,
        "all_day": occurrence.all_day,
        "recurring": occurrence.recurring,
        "start": occurrence.start,
        "end": occurrence.end,
        "start_utc": occurrence.start_utc.isoformat(),
        "end_utc": occurrence.end_utc.isoformat(),
        "original_start": occurrence.original_start,
        "overridden": occurrence.overridden,
    }


def _cron_occurrence_payload(occurrence: Any) -> JsonObject:
    return {
        "job_id": occurrence.job_id,
        "name": occurrence.name,
        "fire_at": occurrence.fire_at_utc.isoformat(),
        "schedule_type": occurrence.schedule_type,
        "event_id": occurrence.event_id,
        "occurrence_id": occurrence.occurrence_id,
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return calendar RPC handlers."""

    return {
        "calendar.window": _calendar_window,
        "calendar.create": _calendar_create,
        "calendar.update": _calendar_update,
        "calendar.delete": _calendar_delete,
    }
