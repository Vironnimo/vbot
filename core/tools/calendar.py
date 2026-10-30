"""Built-in calendar tool for managing the user's local calendar."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from core.automation.cron import CronServiceError
from core.calendar import (
    CalendarEventNotFoundError,
    CalendarServiceError,
    CalendarStorageError,
    CalendarValidationError,
    occurrence_id,
    parse_occurrence_id,
)
from core.projects import format_agent_address
from core.tools._calendar_arguments import (
    ATTENDEES_FIELD,
    CHANGE_FIELDS,
    LONG_TEXT,
    OMIT,
    READ_ACTIONS,
    REFUSAL_PREFIX,
    REMINDER_FIELD,
    REMINDER_SCHEDULE_STAND_IN,
    STAND_INS,
    UNADVERTISED_PARAMETERS,
    WHEN_FIELD,
    CalendarCallRefusedError,
    normalize_calendar_arguments,
    refusal,
    render_call,
)
from core.tools._calendar_read import _CalendarRead, _not_a_time
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
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    import asyncio

    from core.automation.cron import CronService
    from core.calendar import CalendarService

CALENDAR_TOOL_NAME = "calendar"
CALENDAR_TOOL_DESCRIPTION = "Manage events in vBot's local calendar and find free time."

CALENDAR_ACTIONS = frozenset(
    ("list", "get", "search", "create", "update", "delete", "find_free_time")
)

CALENDAR_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "get", "search", "create", "update", "delete", "find_free_time"],
            "description": (
                "list shows occurrences in a time window; search finds stored "
                "events across all dates; get reads one exact id. update changes "
                "only the fields you send."
            ),
        },
        "id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Event or occurrence id from list or search. Required for get, update and delete. "
                "An event id targets the whole series; "
                "an occurrence id targets only that occurrence."
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
                "Omit on create for 1 hour or 1 day; on update to keep the length."
            ),
        },
        "description": {
            "type": "string",
            "description": 'On update, "" clears the description.',
        },
        "location": {"type": "string", "description": 'On update, "" clears the location.'},
        "timezone": {
            "type": "string",
            "description": (
                "IANA zone for timed create/update, such as America/New_York. Omit"
                " on create for the application zone; on update to keep the stored"
                " zone. Changing it keeps local clock times. An occurrence keeps "
                "its series zone."
            ),
        },
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
            "description": (
                "For search or list: text to find in title, description and "
                "location, ignoring case. Omit for all results."
            ),
        },
        "duration": {
            "type": "integer",
            "minimum": 1,
            "default": 60,
            "description": "Minimum free time in minutes for find_free_time.",
        },
        "cursor": {
            "type": "string",
            "minLength": 1,
            "description": (
                "For reads: continuation from next_cursor. "
                "Omit for the first page; to continue, send only action and cursor."
            ),
        },
    },
    "required": ["action"],
}

# Actions that can let a Cron job bound to the event run again.
_REFERENCE_ACTIONS = frozenset({"update", "delete"})

_LOGGER = get_logger("tools.calendar")
_CALENDAR_READS = BoundedWorkerPool(name="calendar-tool", max_workers=2)


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

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        tool = _CalendarTool(calendar_service, cron_service)
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
        announce_changes=("description", "parameters"),
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_calendar_display_parts,
            # Lists count concrete occurrences; searches count stored events.
            fact_builder=lambda arguments, result: (
                result_count_fact_builder("occurrences")(arguments, result)
                or result_count_fact_builder("events")(arguments, result)
            ),
        ),
    )


class _CalendarTool(_CalendarRead):
    """The calendar Tool's actions over the calendar and the cron jobs bound to its events."""

    def __init__(self, calendar: CalendarService, cron: CronService | None) -> None:
        self._calendar: CalendarService = calendar
        self._cron = cron
        self._jobs: dict[str, str] = {}
        self._now = datetime.now(UTC)

    async def run(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        action = arguments["action"]
        try:
            await self._calendar.load()
            if (
                action in {"update", "delete", "get"}
                and "original_start" in arguments
                and "id" in arguments
            ):
                arguments = dict(arguments)
                parsed = parse_occurrence_id(arguments["id"])
                event = self._calendar.get_event(parsed[0] if parsed else arguments["id"])
                original = arguments.pop("original_start")
                moment = datetime.fromisoformat(original)
                if moment.tzinfo is not None:
                    moment = moment.astimezone(
                        ZoneInfo(event.tz_name or self._calendar.system_timezone_name())
                    )
                key = (
                    moment.date().isoformat()
                    if event.all_day
                    else moment.replace(tzinfo=None).isoformat(timespec="seconds")
                )
                if parsed is not None and parsed[1] != key:
                    raise CalendarCallRefusedError(
                        "calendar was not run: id and original_start name different occurrences. "
                        "Send the intended occurrence id from list."
                    )
                arguments["id"] = occurrence_id(event.id, key)
            if self._cron is not None:
                await self._cron.load_projection()
                jobs: dict[str, list[str]] = {}
                for job in self._cron.list_jobs():
                    if job.event_id is None:
                        continue
                    text = (
                        f"{job.id} at {self._cron.format_schedule(job)}, target "
                        f"{format_agent_address(job.agent_id, job.project_id)}"
                    )
                    jobs.setdefault(job.event_id, []).append(
                        text if job.status == "active" else f"{text}, {job.status}"
                    )
                self._jobs = {key: "; ".join(values) for key, values in jobs.items()}
            if REMINDER_FIELD in arguments or ATTENDEES_FIELD in arguments:
                raise CalendarCallRefusedError(
                    self._unsupported_refusal(arguments, context.offers("cron"))
                )
            if action in _REFERENCE_ACTIONS and "id" not in arguments:
                raise CalendarCallRefusedError(_missing_id(action, arguments))
            if action in {"list", "get", "search"}:
                reader = _CalendarRead(self._calendar.projection(), self._jobs, self._now)
                return await _CALENDAR_READS.run(getattr(reader, "_" + action), arguments)
            if action == "find_free_time":
                return await _CALENDAR_READS.run(
                    _CalendarRead(
                        self._calendar.projection(), self._jobs, self._now
                    )._find_free_time,
                    arguments,
                )
            if action == "create":
                return await self._create(arguments)
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
            if action in READ_ACTIONS:
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
            if action not in READ_ACTIONS:
                check = (
                    {"action": "get", "id": arguments["id"]}
                    if "id" in arguments
                    else {"action": "search", "query": arguments.get("title", "")}
                )
                text += f" {render_call(check)} shows whether it changed anything."
            return tool_failure("calendar_service_error", text)

    # -- changing -------------------------------------------------------------------------

    async def _create(self, arguments: JsonObject) -> JsonObject:
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
        event = await self._calendar.create_event_async(
            title=arguments["title"],
            start=arguments["start"],
            end=arguments.get("end"),
            description=arguments.get("description"),
            location=arguments.get("location"),
            rrule=arguments.get("rrule"),
            timezone=arguments.get("timezone"),
            actor="tool",
        )
        reader = _CalendarRead(self._calendar.projection(), self._jobs, self._now)
        return tool_success(await _CALENDAR_READS.run(reader._event_result, event))

    async def _update(self, arguments: JsonObject) -> JsonObject:
        item_id = arguments["id"]
        fields = {name: arguments[name] for name in CHANGE_FIELDS if name in arguments}
        if not fields:
            raise CalendarCallRefusedError(
                refusal(
                    "update needs a field to change: title, start, end, description, location, "
                    "rrule or timezone.",
                    arguments,
                    start=STAND_INS["start"],
                )
            )
        _check_event_times(arguments)
        parsed = parse_occurrence_id(item_id)
        if parsed is None:
            if "timezone" not in fields and any(name in fields for name in ("start", "end")):
                stored = self._calendar.get_event(item_id)
                if stored.tz_name is not None and not (
                    "start" in fields and len(fields["start"]) == 10
                ):
                    fields["timezone"] = stored.tz_name
            event = await self._calendar.update_event(item_id, actor="tool", **fields)
            reader = _CalendarRead(self._calendar.projection(), self._jobs, self._now)
            return tool_success(await _CALENDAR_READS.run(reader._event_result, event))
        if "rrule" in fields:
            raise CalendarCallRefusedError(
                refusal(
                    "rrule belongs to the whole series, so it takes the event id.",
                    arguments,
                    id=parsed[0],
                )
            )
        if "timezone" in fields:
            event = self._calendar.get_event(parsed[0])
            if fields.pop("timezone") != event.tz_name:
                raise CalendarCallRefusedError(
                    "calendar was not run: an occurrence keeps its series timezone. "
                    "Use an offset in start/end to name an instant in another zone, or"
                    " update the series id to change its timezone."
                )
        event = self._calendar.get_event(parsed[0])
        if event.tz_name:
            for name in ("start", "end"):
                if name in fields:
                    moment = datetime.fromisoformat(fields[name])
                    if moment.tzinfo is None:
                        fields[name] = moment.replace(tzinfo=ZoneInfo(event.tz_name)).isoformat()
        occurrence = await self._calendar.update_occurrence(item_id, actor="tool", **fields)
        return tool_success(
            self._occurrence_fields(occurrence, self._calendar.get_event(occurrence.event_id))
        )

    async def _delete(self, arguments: JsonObject) -> JsonObject:
        item_id = arguments["id"]
        if parse_occurrence_id(item_id) is not None:
            occurrence = await self._calendar.delete_occurrence(item_id, actor="tool")
            series = self._calendar.get_event(occurrence.event_id)
            reader = _CalendarRead(self._calendar.projection(), self._jobs, self._now)
            return tool_success(
                {
                    "id": occurrence.id,
                    "title": occurrence.title,
                    "start": self._occurrence_fields(occurrence, series)["start"],
                    "status": "deleted; the rest of the series stays",
                    "next": await _CALENDAR_READS.run(reader._next_text, series),
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
            if parsed is not None:
                steps.append(
                    "Use a one-time cron job for this occurrence; an event-bound job would "
                    "cover the whole series."
                )
                return REFUSAL_PREFIX + " ".join([*texts, *steps])
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


def _attendee_step(call: JsonObject, line: str) -> str:
    """The way to record attendees when no corrected call can carry the description."""
    if call["action"] == "create" or "description" in call:
        return (
            f'To record them, send the call again without attendees and with the line "{line}" '
            "at the end of description."
        )
    item_id = call.get("id")
    if isinstance(item_id, str):
        listing = render_call({"action": "get", "id": item_id})
    else:
        listing = _listing(call)
    return (
        f'To record them, add the line "{line}" to the end of the event\'s description, after '
        f"its current text; {listing} shows the event with its description."
    )


def _listing(arguments: JsonObject) -> str:
    """The list call that shows the event a call names by its title, or all events."""
    title = arguments.get("title")
    return render_call({"action": "search", "query": title}) if title else '{"action":"search"}'


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
            listing = render_call({"action": "get", "id": parsed[0]})
            return f"{REFUSAL_PREFIX}{text}; {listing} shows the series."
    return f'{REFUSAL_PREFIX}{text}; {{"action":"list"}} shows events and their ids.'


def _validation_message(arguments: JsonObject, error: CalendarValidationError) -> str:
    detail = str(error).rstrip(". ").replace("duration_minutes", "duration")
    field = WHEN_FIELD if detail.startswith("cannot parse when") else detail.split(" ", 1)[0]
    if field in STAND_INS and field in arguments:
        return refusal(f"{detail}.", arguments, **{field: STAND_INS[field]})
    return f"{REFUSAL_PREFIX}{detail}."


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
