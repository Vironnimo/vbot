"""Built-in calendar tool for managing the user's local calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any

from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarServiceError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar.service import FIND_FREE_MAX_RESULTS
from core.tools._calendar_arguments import (
    LOCATION_FIELD,
    OMIT,
    QUERY_FIELD,
    REFUSAL_PREFIX,
    STAND_INS,
    TIMEZONE_FIELD,
    UNADVERTISED_PARAMETERS,
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
    event_zone,
    length_text,
    local_text,
    minute_text,
    named_instant,
    read_window,
    server_zone,
    unknown_zone,
    window_text,
)
from core.tools._durations import LATE_LIMITS_FIELD, with_late_limits_note
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
CALENDAR_TOOL_DESCRIPTION = "Manage the user's calendar events and find free time."

CALENDAR_ACTIONS = frozenset(("list", "create", "update", "delete", "find_free"))

CALENDAR_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "create", "update", "delete", "find_free"],
            "description": (
                "list shows events with their ids; find_free shows free time. update changes "
                "only the fields you send."
            ),
        },
        "when": {
            "type": "string",
            "minLength": 1,
            "description": (
                "For list and find_free: today, tomorrow, this week, next week, this month, "
                "next month, a date, a year-month (2030-01) or 'start..end'; defaults are this "
                "month and the next 7 days."
            ),
        },
        "id": {
            "type": "string",
            "minLength": 1,
            "description": "Event id from list or an earlier result.",
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
    },
    "required": ["action"],
}

_EVENT_ID_ACTIONS = frozenset({"update", "delete"})
_DEFAULT_FREE_MINUTES = 60
_NEARBY = timedelta(days=7)
_LISTED_OCCURRENCES = 8
_TITLE_MATCHES = 5
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
}

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

    async def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_calendar_tool(calendar_service, arguments)

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


async def _handle_calendar_tool(
    calendar_service: CalendarService, arguments: JsonObject
) -> JsonObject:
    arguments = dict(arguments)
    late_limits = arguments.pop(LATE_LIMITS_FIELD, None)
    result = await _run_calendar_action(calendar_service, arguments)
    if late_limits:
        return with_late_limits_note(result, late_limits, REFUSAL_PREFIX)
    return result


async def _run_calendar_action(
    calendar_service: CalendarService, arguments: JsonObject
) -> JsonObject:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in CALENDAR_ACTIONS:
        options = ", ".join(sorted(CALENDAR_ACTIONS))
        return tool_failure("invalid_arguments", f"action must be one of: {options}.")
    try:
        if action in _EVENT_ID_ACTIONS and "id" not in arguments:
            raise CalendarCallRefusedError(_missing_id(calendar_service, action, arguments))
        if action == "list":
            return _handle_list(calendar_service, arguments)
        if action == "find_free":
            return _handle_find_free(calendar_service, arguments)
        if action == "create":
            return _handle_create(calendar_service, arguments)
        if action == "update":
            return await _handle_update(calendar_service, arguments)
        return await _handle_delete(calendar_service, arguments)
    except CalendarCallRefusedError as error:
        return tool_failure("invalid_arguments", str(error))
    except CalendarEventNotFoundError:
        return tool_failure(
            "event_not_found",
            f'No event has id "{arguments.get("id")}". {{"action":"list"}} shows events and '
            'their ids; add a when such as "next month" to look further ahead.',
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
    title = arguments.get("title")
    if isinstance(title, str):
        wanted = title.strip().casefold()
        events = calendar_service.list_events()
        matches = [event for event in events if event.title.strip().casefold() == wanted]
        matches = matches or [event for event in events if wanted in event.title.casefold()]
        # update may be renaming; delete only used the title to find the event.
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
        f'{action} needs the event "id"; {{"action":"list"}} shows events and their ids.',
        arguments,
        id="<event id from list>",
        **({} if action == "update" else {"title": OMIT}),
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
        chosen = calendar_service.get_event(item_id)
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
            or text in (events[event_id].description or "").casefold()
            or text in (events[event_id].location or "").casefold()
        }
    listed = [item for items in by_event.values() for item in items]
    data: JsonObject = {
        "events": len(by_event),
        "occurrences": len(listed),
        "window": window_text(window_start, window_end, zone),
        "timezone": calendar_service.system_timezone_name(),
    }
    if isinstance(query, str):
        data["matching"] = query
    if note:
        data["note"] = note
    if by_event:
        data["content"] = "\n\n".join(
            _event_block(calendar_service, events[event_id], items)
            for event_id, items in by_event.items()
        )
    return tool_success(data)


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
) -> str:
    fields = _event_fields(calendar_service, event)
    notes = fields.pop("notes", None)
    lines = [f"{key}: {value}" for key, value in fields.items()]
    if event.rrule is not None:
        lines.append(_occurrence_line(occurrences))
    if notes:
        lines.append("notes: " + "\n  ".join(str(notes).splitlines()))
    return "\n".join(lines)


def _occurrence_line(occurrences: list[EventOccurrence]) -> str:
    starts = [minute_text(item.start) for item in occurrences]
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
    event = calendar_service.create_event(
        title=str(arguments["title"]),
        start=str(arguments["start"]),
        end=_end_of(calendar_service, arguments, None),
        description=arguments.get("notes"),
        location=arguments.get(LOCATION_FIELD),
        rrule=_rule_text(arguments.get("rrule")),
        actor="tool",
    )
    return _event_success(calendar_service, event, note)


async def _handle_update(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    event = calendar_service.get_event(str(arguments["id"]))
    arguments, note = _event_times(calendar_service, arguments, event)
    updates: JsonObject = {}
    if "title" in arguments:
        updates["title"] = arguments["title"]
    if "notes" in arguments:
        updates["description"] = arguments["notes"]
    if LOCATION_FIELD in arguments:
        updates["location"] = arguments[LOCATION_FIELD]
    if "start" in arguments:
        # The start form decides the kind: a date makes an all-day event.
        updates["start"] = arguments["start"]
    end = _end_of(calendar_service, arguments, event)
    if end is not None:
        updates["end"] = end
    if "rrule" in arguments:
        updates["rrule"] = _rule_text(arguments["rrule"])
    if not updates:
        raise CalendarCallRefusedError(
            refusal(
                "update needs a field to change: title, start, duration, rrule or notes.",
                arguments,
                start=STAND_INS["start"],
            )
        )
    updated = await calendar_service.update_event(event.id, actor="tool", **updates)
    return _event_success(calendar_service, updated, note)


async def _handle_delete(calendar_service: CalendarService, arguments: JsonObject) -> JsonObject:
    event = calendar_service.get_event(str(arguments["id"]))
    start = arguments.get("start")
    if not isinstance(start, str):
        await calendar_service.delete_event(event.id, actor="tool")
        return _deleted(event, None)
    occurrence = _named_occurrence(calendar_service, event, start, arguments)
    if event.rrule is None:
        await calendar_service.delete_event(event.id, actor="tool")
        return _deleted(event, "The event does not repeat, so the whole event was deleted.")
    await calendar_service.delete_occurrence(occurrence.id, actor="tool")
    return tool_success(
        {
            "id": event.id,
            "title": event.title,
            "removed_occurrence": minute_text(occurrence.original_start or occurrence.start),
            "status": "occurrence removed; the rest of the series stays",
        }
    )


def _deleted(event: CalendarEvent, note: str | None) -> JsonObject:
    data: JsonObject = {"id": event.id, "title": event.title, "status": "deleted"}
    if note:
        data["note"] = note
    return tool_success(data)


def _named_occurrence(
    calendar_service: CalendarService, event: CalendarEvent, start: str, arguments: JsonObject
) -> EventOccurrence:
    """Return the occurrence whose original start a delete names, or refuse."""
    server = server_zone(calendar_service)
    # Occurrence starts are written in the event's own wall-clock zone.
    own = event_zone(calendar_service, event)
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
    by_start = {item.original_start or item.start: item for item in nearby}
    if wanted in by_start:
        return by_start[wanted]
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
    if not by_start:
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
    closest = sorted(by_start, key=lambda item: abs(_seconds(item) - _seconds(wanted)))[:3]
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
    """Read a call's zone and end into a start and a duration in the event's kind."""
    server = server_zone(calendar_service)
    recurring = (
        arguments["rrule"] is not None
        if "rrule" in arguments
        else event is not None and event.rrule is not None
    )
    arguments, zone_note = apply_timezone(arguments, server, recurring=recurring)
    stored_start = _event_start(event) if event is not None else None
    arguments, length_note = apply_length(
        arguments, server, stored_start=stored_start, recurring=recurring
    )
    arguments = apply_end(arguments, server, stored_start=stored_start, recurring=recurring)
    note = " ".join(text for text in (zone_note, length_note) if text) or None
    return arguments, note


def _end_of(
    calendar_service: CalendarService, arguments: JsonObject, event: CalendarEvent | None
) -> str | None:
    """The end a call's duration gives, measured from its start or the event's own.

    All-day events last whole days. A repeating event lasts wall-clock minutes; a
    single one lasts real minutes, so its end may show another clock time across
    a daylight saving change. None when the call gives no duration.
    """
    duration = arguments.get("duration")
    if not isinstance(duration, int) or isinstance(duration, bool):
        return None
    sent = arguments.get("start")
    start = sent if isinstance(sent, str) else None if event is None else event.start
    if start is None:
        return None
    if is_date(start.strip()):
        return (date.fromisoformat(start.strip()) + timedelta(days=duration)).isoformat()
    begin = parse_local(start)
    if begin is None:
        # The calendar explains a malformed start.
        return None
    # A sent start is a server-local time; a stored one is in the event's own zone.
    zone = (
        server_zone(calendar_service)
        if isinstance(sent, str) or event is None
        else event_zone(calendar_service, event)
    )
    begin = begin.astimezone(zone) if begin.tzinfo else begin.replace(tzinfo=zone)
    recurring = (
        arguments["rrule"] is not None
        if "rrule" in arguments
        else event is not None and event.rrule is not None
    )
    if recurring:
        finish = begin + timedelta(minutes=duration)
    else:
        finish = (begin.astimezone(UTC) + timedelta(minutes=duration)).astimezone(zone)
    return finish.isoformat()


def _rule_text(rule: Any) -> str | None:
    """The RRULE text of a call's repetition rule object; text passes through."""
    if rule is None or isinstance(rule, str):
        return rule
    if not isinstance(rule, dict):
        raise CalendarValidationError('rrule must be an object such as {"freq":"weekly"}')
    unknown = sorted(set(rule) - {"freq", "interval", "count", "until", "by_weekday"})
    if unknown:
        raise CalendarValidationError(f"rrule has unknown fields: {', '.join(unknown)}")
    parts = [f"FREQ={str(rule.get('freq', '')).upper()}"]
    for name in ("interval", "count"):
        if rule.get(name) is not None:
            parts.append(f"{name.upper()}={rule[name]}")
    if rule.get("until") is not None:
        parts.append(f"UNTIL={str(rule['until']).replace('-', '')}")
    days = rule.get("by_weekday")
    if isinstance(days, list) and days:
        parts.append("BYDAY=" + ",".join(str(day).upper() for day in days))
    return ";".join(parts)


def _event_success(
    calendar_service: CalendarService, event: CalendarEvent, note: str | None
) -> JsonObject:
    data = _event_fields(calendar_service, event)
    if note:
        data["note"] = note
    return tool_success(data)


def _event_fields(calendar_service: CalendarService, event: CalendarEvent) -> JsonObject:
    """One event in the Agent-facing shape: times in the event's own zone, rule as RRULE."""
    data: JsonObject = {"id": event.id, "title": event.title, "start": _event_start(event)}
    if event.all_day:
        days = (date.fromisoformat(event.end) - date.fromisoformat(event.start)).days
        data["days"] = days
    else:
        data["end"] = minute_text(event.end)
    if event.rrule is not None:
        data["repeats"] = event.rrule
        if event.exdates:
            data["removed_occurrences"] = ", ".join(minute_text(item) for item in event.exdates)
    if event.location:
        data["location"] = event.location
    if event.description:
        data["notes"] = event.description
    return data


def _event_start(event: CalendarEvent) -> str:
    """The event's start as the Agent sees and sends it: a date, or a local time."""
    return event.start if event.all_day else minute_text(event.start)


# -- rendering -------------------------------------------------------------------------------


def _validation_message(arguments: JsonObject, error: CalendarValidationError) -> str:
    detail = str(error).rstrip(". ")
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
