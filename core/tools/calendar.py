"""Built-in calendar tool for managing the user's local calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from itertools import islice
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from core.automation.cron import CronServiceError
from core.calendar import (
    MAX_WINDOW_DAYS,
    CalendarEventNotFoundError,
    CalendarServiceError,
    CalendarStorageError,
    CalendarValidationError,
    parse_occurrence_id,
    parse_when,
)
from core.calendar.service import FIND_FREE_MAX_RESULTS
from core.projects import format_agent_address
from core.tools._calendar_arguments import (
    ATTENDEES_FIELD,
    CHANGE_FIELDS,
    LONG_TEXT,
    OMIT,
    REFUSAL_PREFIX,
    REMINDER_FIELD,
    REMINDER_SCHEDULE_STAND_IN,
    STAND_INS,
    UNADVERTISED_PARAMETERS,
    WHEN_FIELD,
    WINDOW_ACTIONS,
    CalendarCallRefusedError,
    length_text,
    normalize_calendar_arguments,
    refusal,
    render_call,
)
from core.tools._cron_arguments import render_call as render_cron_call
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
    import asyncio

    from core.automation.cron import CronService
    from core.calendar import CalendarEvent, CalendarService, EventOccurrence

CALENDAR_TOOL_NAME = "calendar"
CALENDAR_TOOL_DESCRIPTION = "Manage the user's calendar events and find free time."

CALENDAR_ACTIONS = frozenset(("list", "create", "update", "delete", "find_free_time"))

CALENDAR_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "create", "update", "delete", "find_free_time"],
            "description": "update changes only the fields you send.",
        },
        "id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Event id from list. For a repeating event, an occurrence id changes or deletes "
                "only that occurrence. Required for update and delete."
            ),
        },
        "title": {"type": "string", "minLength": 1, "description": "Required for create."},
        "start": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Local date-time such as 2030-01-10T15:00, or a date such as 2030-01-10 for an "
                "all-day event. Required for create."
            ),
        },
        "end": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Same form as start, exclusive: for an all-day event the day after the last day. "
                "Omit for 1 hour or 1 day."
            ),
        },
        "description": {
            "type": "string",
            "description": 'On update, "" clears the description.',
        },
        "location": {"type": "string", "description": 'On update, "" clears the location.'},
        "rrule": {
            "type": "string",
            "description": (
                'RFC 5545 rule such as FREQ=WEEKLY;BYDAY=MO,WE. On update, "" stops repeating.'
            ),
        },
        "time_min": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Start of the window for list and find_free_time: a date or local date-time. "
                "Omit to start now."
            ),
        },
        "time_max": {
            "type": "string",
            "minLength": 1,
            "description": (
                "End of the window, exclusive. Omit for 30 days after time_min, or 7 days for "
                "find_free_time."
            ),
        },
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Text to find in title, description and location.",
        },
        "duration": {
            "type": "integer",
            "minimum": 1,
            "default": 60,
            "description": "Minimum free time in minutes for find_free_time.",
        },
    },
    "required": ["action"],
}

_LIST_DAYS = 30
_FREE_DAYS = 7
_DEFAULT_FREE_MINUTES = 60
_LISTED_OCCURRENCES = 10
_NEXT_OCCURRENCES = 3
# Actions that can let a Cron job bound to the event run again.
_REFERENCE_ACTIONS = frozenset({"update", "delete"})

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


def register_calendar_tool(
    registry: ToolRegistry,
    calendar_service: CalendarService,
    *,
    reference_lock: asyncio.Lock,
    cron_service: CronService | None = None,
) -> None:
    """Register the calendar tool with a vBot tool registry.

    ``reference_lock`` is the Agent reference lock (``AutomationReferences.lock``).
    update and delete hold it like the calendar RPCs, so an event change cannot
    revive a Cron job bound to the event between a removal's reference check and
    the removal. ``cron_service`` names the jobs bound to each event in results.
    """
    tool = _CalendarTool(calendar_service, cron_service)

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        if arguments.get("action") in _REFERENCE_ACTIONS:
            async with reference_lock:
                return await tool.run(context, arguments)
        return await tool.run(context, arguments)

    registry.register(
        CALENDAR_TOOL_NAME,
        CALENDAR_TOOL_DESCRIPTION,
        CALENDAR_TOOL_PARAMETERS,
        handler,
        summary="Manage the user's calendar.",
        open_input_schema=True,
        argument_normalizer=_normalize_calendar_arguments,
        unadvertised_parameters=UNADVERTISED_PARAMETERS,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_calendar_display_parts,
            # Only list results carry an event count.
            fact_builder=result_count_fact_builder("events"),
        ),
    )


class _CalendarTool:
    """The calendar Tool's actions over the calendar and the cron jobs bound to its events."""

    def __init__(self, calendar: CalendarService, cron: CronService | None) -> None:
        self._calendar = calendar
        self._cron = cron

    async def run(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        action = arguments["action"]
        try:
            if REMINDER_FIELD in arguments or ATTENDEES_FIELD in arguments:
                raise CalendarCallRefusedError(
                    self._unsupported_refusal(arguments, context.offers("cron"))
                )
            if action in _REFERENCE_ACTIONS and "id" not in arguments:
                raise CalendarCallRefusedError(_missing_id(action, arguments))
            if action == "list":
                return self._list(arguments)
            if action == "find_free_time":
                return self._find_free_time(arguments)
            if action == "create":
                return self._create(arguments)
            if action == "update":
                return await self._update(arguments)
            return await self._delete(arguments)
        except CalendarCallRefusedError as error:
            return tool_failure("invalid_arguments", str(error))
        except CalendarEventNotFoundError:
            event_ids = {event.id for event in self._calendar.list_events()}
            return tool_failure("event_not_found", _not_found(arguments.get("id"), event_ids))
        except CalendarValidationError as error:
            return tool_failure("invalid_arguments", _validation_message(arguments, error))
        except CalendarStorageError as error:
            # The service rolls a failed save back, and the file is replaced atomically.
            _LOGGER.warning("Calendar storage error for action=%s: %s", action, error)
            if action in WINDOW_ACTIONS:
                text = (
                    "vBot cannot read its calendar file on the server. Retrying will not help; "
                    "tell the user that vBot cannot read its calendar data."
                )
            else:
                text = (
                    "Nothing was changed: vBot cannot read or write its calendar file on the "
                    "server. Retrying will not help; tell the user that vBot cannot save its "
                    "calendar data."
                )
            return tool_failure("calendar_storage_error", text)
        except CalendarServiceError as error:
            _LOGGER.warning("Calendar service error for action=%s: %s", action, error)
            text = f"The calendar could not complete the call: {str(error).rstrip('. ')}."
            if action not in WINDOW_ACTIONS:
                text += ' {"action":"list"} shows whether it changed anything.'
            return tool_failure("calendar_service_error", text)

    # -- reading ------------------------------------------------------------------------

    def _list(self, arguments: JsonObject) -> JsonObject:
        zone = self._zone()
        start, end = self._window(arguments, _LIST_DAYS)
        events = {event.id: event for event in self._calendar.list_events()}
        found: dict[str, list[EventOccurrence]] = {}
        if "id" in arguments:
            # One event, shown even when none of its occurrences falls in the window.
            parsed = parse_occurrence_id(arguments["id"])
            chosen = self._calendar.get_event(parsed[0] if parsed else arguments["id"])
            events, found = {chosen.id: chosen}, {chosen.id: []}
        query = arguments.get("query")
        text = query.casefold() if isinstance(query, str) else ""
        for occurrence in self._calendar.occurrences_in_window(start, end):
            fields = (occurrence.title, occurrence.description or "", occurrence.location or "")
            if occurrence.event_id in events and any(text in field.casefold() for field in fields):
                found.setdefault(occurrence.event_id, []).append(occurrence)
        data: JsonObject = {
            "events": len(found),
            "window": _window_text(start, end, zone),
            "timezone": self._calendar.system_timezone_name(),
        }
        if found:
            data["content"] = "\n\n".join(
                self._event_block(events[event_id], items) for event_id, items in found.items()
            )
        return tool_success(data)

    def _find_free_time(self, arguments: JsonObject) -> JsonObject:
        zone = self._zone()
        start, end = self._window(arguments, _FREE_DAYS)
        duration = arguments.get("duration", _DEFAULT_FREE_MINUTES)
        slots = self._calendar.find_free_slots(
            start, end, duration, max_results=FIND_FREE_MAX_RESULTS + 1
        )
        shown = slots[:FIND_FREE_MAX_RESULTS]
        data: JsonObject = {
            "free": len(shown),
            "window": _window_text(start, end, zone),
            "timezone": self._calendar.system_timezone_name(),
        }
        if not shown:
            data["note"] = f"No free span of {length_text(duration)} or more in this window."
        elif len(slots) > len(shown):
            data["note"] = (
                f"More free time follows after {_local(shown[-1].end_utc, zone)}; a later "
                "time_min shows it."
            )
        if shown:
            data["content"] = "\n".join(
                f"{_local(slot.start_utc, zone)} to {_local(slot.end_utc, zone)} "
                f"({length_text(int((slot.end_utc - slot.start_utc).total_seconds() // 60))})"
                for slot in shown
            )
        return tool_success(data)

    def _window(self, arguments: JsonObject, days: int) -> tuple[datetime, datetime]:
        """The half-open UTC window a list or find_free_time call reads."""
        zone = self._zone()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        when = arguments.get(WHEN_FIELD)
        if isinstance(when, str):
            if "time_min" in arguments or "time_max" in arguments:
                raise CalendarCallRefusedError(
                    refusal(
                        "the window is given twice, as when and as time_min/time_max.",
                        arguments,
                        **{WHEN_FIELD: OMIT},
                    )
                )
            return parse_when(when, now_utc=now, tz=zone)
        start = _bound(arguments, "time_min", zone) or now
        end = _bound(arguments, "time_max", zone) or _days_after(start, days, zone)
        if end <= start:
            raise CalendarCallRefusedError(
                refusal(
                    "time_max must come after time_min.", arguments, time_max=STAND_INS["time_max"]
                )
            )
        if end - start > timedelta(days=MAX_WINDOW_DAYS):
            latest = _local(_days_after(start, MAX_WINDOW_DAYS, zone), zone)
            raise CalendarCallRefusedError(
                refusal(
                    f"a window spans at most {MAX_WINDOW_DAYS} days.",
                    {**arguments, "time_min": _local(start, zone)},
                    time_max=latest,
                )
            )
        return start, end

    # -- changing -------------------------------------------------------------------------

    def _create(self, arguments: JsonObject) -> JsonObject:
        missing = [name for name in ("title", "start") if name not in arguments]
        if missing:
            raise CalendarCallRefusedError(
                refusal(
                    f"create needs {' and '.join(missing)}.",
                    arguments,
                    **{name: STAND_INS[name] for name in missing},
                )
            )
        _check_event_times(arguments)
        event = self._calendar.create_event(
            title=arguments["title"],
            start=arguments["start"],
            end=arguments.get("end"),
            description=arguments.get("description"),
            location=arguments.get("location"),
            rrule=arguments.get("rrule"),
            actor="tool",
        )
        return tool_success(self._event_result(event))

    async def _update(self, arguments: JsonObject) -> JsonObject:
        item_id = arguments["id"]
        fields = {name: arguments[name] for name in CHANGE_FIELDS if name in arguments}
        if not fields:
            raise CalendarCallRefusedError(
                refusal(
                    "update needs a field to change: title, start, end, description, location or "
                    "rrule.",
                    arguments,
                    start=STAND_INS["start"],
                )
            )
        _check_event_times(arguments)
        parsed = parse_occurrence_id(item_id)
        if parsed is None:
            event = await self._calendar.update_event(item_id, actor="tool", **fields)
            return tool_success(self._event_result(event))
        if "rrule" in fields:
            raise CalendarCallRefusedError(
                refusal(
                    "rrule belongs to the whole series, so it takes the event id.",
                    arguments,
                    id=parsed[0],
                )
            )
        occurrence = await self._calendar.update_occurrence(item_id, actor="tool", **fields)
        return tool_success(_occurrence_fields(occurrence))

    async def _delete(self, arguments: JsonObject) -> JsonObject:
        item_id = arguments["id"]
        if parse_occurrence_id(item_id) is not None:
            occurrence = await self._calendar.delete_occurrence(item_id, actor="tool")
            series = self._calendar.get_event(occurrence.event_id)
            return tool_success(
                {
                    "id": occurrence.id,
                    "title": occurrence.title,
                    "start": _time_text(occurrence.start),
                    "status": "deleted; the rest of the series stays",
                    "next": self._next_text(series),
                }
            )
        event = self._calendar.get_event(item_id)
        try:
            jobs = await self._calendar.delete_event(item_id, actor="tool")
        except CronServiceError as error:
            # The event stays deleted; its jobs wait without it and no longer run.
            _LOGGER.warning("Cron jobs of deleted event %s were not deleted: %s", item_id, error)
            return tool_failure(
                "cron_storage_error",
                f'Event {event.id} "{event.title}" was deleted, but the cron jobs bound to it '
                "were not: vBot cannot read or write its cron job file on the server. Those jobs "
                "no longer run. Retrying will not help; tell the user that vBot cannot save its "
                "cron job data.",
            )
        data: JsonObject = {
            "id": event.id,
            "title": event.title,
            "status": "deleted with all its occurrences" if event.recurring else "deleted",
        }
        if jobs:
            data["deleted_cron_jobs"] = ", ".join(f'{job.id} "{job.name}"' for job in jobs)
        return tool_success(data)

    # -- refusals -------------------------------------------------------------------------

    def _unsupported_refusal(self, arguments: JsonObject, cron_offered: bool) -> str:
        """Refuse attendees and reminders, which the calendar does not keep.

        Attendees can be recorded as a description line; reminders are cron jobs
        bound to the event. A corrected update carries the stored description the
        line joins; when that text is unknown or too long to show, the refusal
        names the next step instead of a call.
        """
        call = {
            key: value
            for key, value in arguments.items()
            if key not in (REMINDER_FIELD, ATTENDEES_FIELD)
        }
        texts: list[str] = []
        steps: list[str] = []
        event_call = None
        if ATTENDEES_FIELD in arguments:
            texts.append("the calendar cannot invite attendees.")
            line = "Attendees: " + ", ".join(arguments[ATTENDEES_FIELD])
            description = self._description_with(call, line)
            if description is None:
                steps.append(_attendee_step(call, line))
            else:
                texts.append("To record them, put them in description.")
                event_call = render_call(call, description=description)
        reminder = arguments.get(REMINDER_FIELD)
        if reminder is not None:
            texts.append(f"{'The' if texts else 'the'} calendar has no reminders.")
            if cron_offered:
                texts.append("cron runs an instruction before, at or after an event.")
            changes = call["action"] == "create" or any(name in call for name in CHANGE_FIELDS)
            if changes and not steps and event_call is None:
                event_call = render_call(call)
        if event_call is not None:
            steps.append(f"Send: {event_call}")
        if reminder is not None and cron_offered:
            item_id = call.get("id")
            parsed = parse_occurrence_id(item_id) if isinstance(item_id, str) else None
            cron_call = render_cron_call(
                {
                    "action": "create",
                    "event_id": (parsed[0] if parsed else item_id) or "<event id from the result>",
                    "prompt": reminder.get("prompt"),
                    "schedule": reminder.get("schedule", REMINDER_SCHEDULE_STAND_IN),
                }
            )
            steps.append(f"{'Then send' if steps else 'Send'} to cron: {cron_call}")
        return REFUSAL_PREFIX + " ".join([*texts, *steps])

    def _description_with(self, call: JsonObject, line: str) -> str | None:
        """The description the call leaves, with ``line`` added; None when it cannot be shown.

        A description the call sends replaces the stored one, so the line joins
        it; otherwise the line joins the stored description of the event or
        occurrence the update names.
        """
        if call["action"] == "update" and not isinstance(call.get("id"), str):
            return None
        if "description" in call or call["action"] == "create":
            current = call.get("description")
        else:
            item_id = call["id"]
            if parse_occurrence_id(item_id) is None:
                current = self._calendar.get_event(item_id).description
            else:
                current = self._calendar.get_occurrence(item_id).description
        text = f"{current}\n{line}" if current else line
        return text if len(text) <= LONG_TEXT else None

    # -- rendering ------------------------------------------------------------------------

    def _event_result(self, event: CalendarEvent) -> JsonObject:
        data = self._event_fields(event)
        if event.recurring:
            data["next"] = self._next_text(event)
        return data

    def _next_text(self, event: CalendarEvent) -> str:
        """The next starts of a repeating event, or that none is ahead."""
        upcoming = islice(
            self._calendar.iter_occurrences(event, datetime.now(UTC)), _NEXT_OCCURRENCES
        )
        return ", ".join(_time_text(item.start) for item in upcoming) or "none ahead"

    def _event_fields(self, event: CalendarEvent) -> JsonObject:
        """One event in the Agent-facing shape: local times, the last day of an all-day event."""
        data: JsonObject = {
            "id": event.id,
            "title": event.title,
            "start": _time_text(event.start),
            "end": _end_text(event.end, all_day=event.all_day),
        }
        if event.rrule:
            data["rrule"] = event.rrule
        if event.location:
            data["location"] = event.location
        if event.description:
            data["description"] = event.description
        jobs = self._jobs_text(event.id)
        if jobs:
            data["cron_jobs"] = jobs
        return data

    def _event_block(self, event: CalendarEvent, occurrences: list[EventOccurrence]) -> str:
        lines = [
            f"{key}: " + "\n  ".join(str(value).splitlines())
            for key, value in self._event_fields(event).items()
        ]
        if event.recurring:
            lines.extend(_occurrence_lines(event, occurrences))
        return "\n".join(lines)

    def _jobs_text(self, event_id: str) -> str | None:
        """The cron jobs bound to an event: id, schedule and target, with any other status."""
        if self._cron is None:
            return None
        texts = []
        for job in self._cron.list_jobs(event_id=event_id):
            text = (
                f"{job.id} at {self._cron.format_schedule(job)}, target "
                f"{format_agent_address(job.agent_id, job.project_id)}"
            )
            texts.append(text if job.status == "active" else f"{text}, {job.status}")
        return "; ".join(texts) or None

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self._calendar.system_timezone_name())


def _occurrence_lines(event: CalendarEvent, occurrences: list[EventOccurrence]) -> list[str]:
    if not occurrences:
        return ["occurrences: none in this window"]
    lines = ["occurrences:"]
    for item in occurrences[:_LISTED_OCCURRENCES]:
        line = f"  {item.id} {_time_text(item.start)}"
        if item.overridden:
            line += f" to {_end_text(item.end, all_day=item.all_day)}"
            changed = [
                f"{name}: {_brief(value)}"
                for name, value, own in (
                    ("title", item.title, event.title),
                    ("location", item.location, event.location),
                    ("description", item.description, event.description),
                )
                if value != own
            ]
            line += "".join(f", {text}" for text in changed)
        lines.append(line)
    if len(occurrences) > _LISTED_OCCURRENCES:
        lines.append(
            f"  ...{len(occurrences) - _LISTED_OCCURRENCES} more, the last at "
            f"{_time_text(occurrences[-1].start)}; a shorter window lists them"
        )
    return lines


def _occurrence_fields(occurrence: EventOccurrence) -> JsonObject:
    data: JsonObject = {
        "id": occurrence.id,
        "series": occurrence.event_id,
        "title": occurrence.title,
        "start": _time_text(occurrence.start),
        "end": _end_text(occurrence.end, all_day=occurrence.all_day),
    }
    if occurrence.location:
        data["location"] = occurrence.location
    if occurrence.description:
        data["description"] = occurrence.description
    return data


def _attendee_step(call: JsonObject, line: str) -> str:
    """The way to record attendees when no corrected call can carry the description."""
    if call["action"] == "create" or "description" in call:
        return (
            f'To record them, send the call again without attendees and with the line "{line}" '
            "at the end of description."
        )
    item_id = call.get("id")
    if isinstance(item_id, str):
        parsed = parse_occurrence_id(item_id)
        listing = render_call({"action": "list", "id": parsed[0] if parsed else item_id})
    else:
        listing = _listing(call)
    return (
        f'To record them, add the line "{line}" to the end of the event\'s description, after '
        f"its current text; {listing} shows the event with its description."
    )


def _listing(arguments: JsonObject) -> str:
    """The list call that shows the event a call names by its title, or all events."""
    title = arguments.get("title")
    return render_call({"action": "list", "query": title}) if title else '{"action":"list"}'


def _missing_id(action: str, arguments: JsonObject) -> str:
    return refusal(
        f'{action} needs the event "id"; {_listing(arguments)} shows events and their ids.',
        arguments,
        id=STAND_INS["id"],
        **({"title": OMIT} if action == "delete" else {}),
    )


def _not_found(item_id: Any, event_ids: set[str]) -> str:
    """Refuse an unknown id, naming the list call that shows the valid ones."""
    parsed = parse_occurrence_id(item_id) if isinstance(item_id, str) else None
    if parsed is None:
        text = f'no event has id "{item_id}"'
    else:
        text = f'no occurrence has id "{item_id}"'
        if parsed[0] in event_ids:
            listing = render_call({"action": "list", "id": parsed[0]})
            return f"{REFUSAL_PREFIX}{text}; {listing} shows the event's occurrences."
    return f'{REFUSAL_PREFIX}{text}; {{"action":"list"}} shows events and their ids.'


def _validation_message(arguments: JsonObject, error: CalendarValidationError) -> str:
    detail = str(error).rstrip(". ").replace("duration_minutes", "duration")
    field = WHEN_FIELD if detail.startswith("cannot parse when") else detail.split(" ", 1)[0]
    if field in STAND_INS and field in arguments:
        return refusal(f"{detail}.", arguments, **{field: STAND_INS[field]})
    return f"{REFUSAL_PREFIX}{detail}."


def _bound(arguments: JsonObject, name: str, zone: ZoneInfo) -> datetime | None:
    """A window bound: a date is local midnight, a date-time without offset local time."""
    value = arguments.get(name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time.min, zone).astimezone(UTC)
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise _not_a_time(arguments, name) from None
    return (moment if moment.tzinfo else moment.replace(tzinfo=zone)).astimezone(UTC)


def _check_event_times(arguments: JsonObject) -> None:
    """Refuse a start or end that is neither a date nor a date-time, naming the value."""
    for name in ("start", "end"):
        value = arguments.get(name)
        if not isinstance(value, str):
            continue
        try:
            datetime.fromisoformat(value.strip())
        except ValueError:
            raise _not_a_time(arguments, name) from None


def _not_a_time(arguments: JsonObject, name: str) -> CalendarCallRefusedError:
    return CalendarCallRefusedError(
        refusal(
            f'{name} "{arguments[name]}" is not a date or local date-time.',
            arguments,
            **{name: STAND_INS[name]},
        )
    )


def _days_after(start: datetime, days: int, zone: ZoneInfo) -> datetime:
    """``days`` later on the local wall clock."""
    local = start.astimezone(zone).replace(tzinfo=None) + timedelta(days=days)
    return local.replace(tzinfo=zone).astimezone(UTC)


def _window_text(start: datetime, end: datetime, zone: ZoneInfo) -> str:
    """Render a window as dates when it spans whole days, else as local times."""
    first, last = start.astimezone(zone), end.astimezone(zone)
    if first.time() == time.min and last.time() == time.min:
        final = (last - timedelta(days=1)).date()
        if final == first.date():
            return first.date().isoformat()
        return f"{first.date().isoformat()} to {final.isoformat()}"
    return f"{_local(start, zone)} to {_local(end, zone)}"


def _local(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).replace(tzinfo=None).isoformat(timespec="minutes")


def _time_text(value: str) -> str:
    """A stored date stays; a local time drops zero seconds: 2030-01-10T15:00."""
    return value[:-3] if len(value) == 19 and value.endswith(":00") else value


def _end_text(end: str, *, all_day: bool) -> str:
    if not all_day:
        return _time_text(end)
    return f"{end} (last day {(date.fromisoformat(end) - timedelta(days=1)).isoformat()})"


def _brief(text: str | None) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= 40 else line[:37] + "..."


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
    for field_name in ("title", "id", "query", "time_min", WHEN_FIELD):
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
