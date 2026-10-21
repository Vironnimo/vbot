"""Built-in cron tool for managing scheduled automation jobs."""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from core.automation.cron import (
    MAX_CONSECUTIVE_CRON_FAILURES,
    CronDelivery,
    CronJobInPastError,
    CronJobNotFoundError,
    CronJobValidationError,
    CronServiceError,
    CronStorageError,
    CronTargetAgentNotFoundError,
    CronTargetError,
    CronTargetProjectNotFoundError,
    CronTargetUnavailableError,
    CronWatch,
)
from core.projects import InvalidAgentAddressError, format_agent_address, parse_agent_address
from core.tools._cron_arguments import (
    CURRENT_SESSION,
    DELIVER_PLATFORM_FIELD,
    ENABLED_FIELD,
    EVENT_REPEAT_REFUSAL,
    EVENT_TIME_STAND_IN,
    HERE,
    NEW_SESSION,
    NO_WATCH,
    OMIT,
    REFUSAL_PREFIX,
    SELF_TARGET,
    UNADVERTISED_PARAMETERS,
    WATCH_STAND_IN,
    WEBHOOK_SECRET_FIELD,
    CronCallRefusedError,
    ambiguous_event_time,
    event_time_readings,
    normalize_cron_arguments,
    refusal,
    render_call,
)
from core.tools._cron_timezone import zoned_schedule
from core.tools._durations import LATE_LIMITS_FIELD, with_late_limits_note
from core.tools.call_syntax import spelling
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

    from core.automation.cron import CronJob, CronService, ParsedSchedule

CRON_TOOL_NAME = "cron"
CRON_TOOL_DESCRIPTION = (
    "Schedule jobs that run an instruction later: once, repeatedly, before, at or after a "
    "calendar event, or when the job's webhook URL receives a request."
)

CRON_ACTIONS = frozenset(("create", "list", "update", "delete", "enable", "disable"))

_ID_ACTIONS = frozenset({"update", "delete", "enable", "disable"})
_UPDATE_FIELDS = (
    "target",
    "name",
    "prompt",
    "schedule",
    "event_id",
    "repeat",
    "session",
    "deliver",
    "watch",
    ENABLED_FIELD,
)
_SCHEDULE_FORMS = (
    'Use five cron fields in server time such as "0 9 * * 1-5" (weekdays at 09:00), '
    '"every 2h", "in 30m", or a local time such as "2030-01-01T09:00".'
)
# Values only the Agent can choose; a call that sends one back unchanged changes nothing.
_SCHEDULE_STAND_IN = "<when>"
_FUTURE_STAND_IN = "<future time>"
_REPEATING_STAND_IN = "<repeating schedule>"
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_TARGET_ADDRESS_RECOMMENDATION = (
    'Set "target" to an existing Agent id, or to agent@project for a member of a Project Team'
)
_TARGET_UNAVAILABLE_RECOMMENDATION = (
    'Choose another "target", or tell the user that the target Agent cannot run and why'
)
# Who can send requests to a webhook job's URL, by the reach Cron reports.
_WEBHOOK_REACH = {
    "internet": "the internet",
    "network": "this computer and the home network",
    "computer": "this computer only",
}

CRON_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "list", "update", "delete", "enable", "disable"],
            "description": (
                "list shows jobs and their ids. update changes only the fields you send. "
                "disable pauses a job, enable resumes it, delete removes it."
            ),
        },
        "id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Job id from list or an earlier result. Required for update, delete, enable, "
                "and disable."
            ),
        },
        "target": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Agent that runs the job: agent or agent@project. Omit to use the current Agent."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Short label. Omit to derive it from prompt.",
        },
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": ("Self-contained instruction for each run. Required on create."),
        },
        "schedule": {
            "type": "string",
            "minLength": 1,
            "description": (
                "When to run, in local time: five cron fields (minute hour day month "
                "weekday), e.g. '0 9 * * 1-5' for weekdays at 09:00; 'every 30m', 'every 2h' "
                "or 'every 1d'; or once with 'in 45m' or '2030-08-07T09:00'; or 'webhook' to "
                "run whenever a request reaches the job's URL, and each run gets the request's "
                "headers and body. Required on create."
            ),
        },
        "event_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Calendar event to run the job at, for every occurrence. schedule is then start "
                "or end, optionally +/- a duration, e.g. 'start - 30m'."
            ),
        },
        "repeat": {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": (
                "Number of runs before the job ends. Omit for no limit; null on update removes "
                "a limit."
            ),
        },
        "session": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Session each run continues: current for this conversation, or a Session id. "
                "Omit to start each run in a new Session without this conversation; on update, "
                "new does that again."
            ),
        },
        "deliver": {
            "type": "string",
            "enum": ["here", "app", "none"],
            "description": (
                "Where each run's final answer goes: here (this chat, or vBot outside a chat), "
                "app (vBot, with a notification) or none. Omit on create for here."
            ),
        },
        "watch": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Command or http(s) URL to check before each run; the job runs only when the "
                "output changed, and the run gets the diff. A command's non-zero exit counts as "
                "a failed run. Omit to run every time; on update, none stops watching."
            ),
        },
    },
    "required": ["action"],
}

_LOGGER = get_logger("tools.cron")


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name=CRON_TOOL_NAME,
        input_schema={
            **CRON_TOOL_PARAMETERS,
            "properties": {**CRON_TOOL_PARAMETERS["properties"], **UNADVERTISED_PARAMETERS},
        },
        require_closed_input=False,
    )


def _normalize_cron_arguments(arguments: Any) -> Any:
    return normalize_cron_arguments(_repair_contract(), arguments)


# Actions that choose a job's target Agent and Project.
_REFERENCE_ACTIONS = frozenset({"create", "update"})


def register_cron_tool(
    registry: ToolRegistry, cron_service: CronService, *, reference_lock: asyncio.Lock
) -> None:
    """Register the cron tool with a vBot tool registry.

    ``reference_lock`` is the Agent reference lock (``AutomationReferences.lock``).
    create and update hold it like the cron RPCs, so a job cannot select an Agent
    or Project between a removal's reference check and the removal.
    """

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        if arguments.get("action") in _REFERENCE_ACTIONS:
            async with reference_lock:
                before = _stored_watch(cron_service, arguments)
                result = await _handle_cron_tool(cron_service, context, arguments)
            # A check can take a minute; other Agent and Project changes need not wait for it.
            if arguments.get("watch", NO_WATCH) != NO_WATCH:
                return await _with_first_check(cron_service, result, before)
            return result
        return await _handle_cron_tool(cron_service, context, arguments)

    registry.register(
        CRON_TOOL_NAME,
        CRON_TOOL_DESCRIPTION,
        CRON_TOOL_PARAMETERS,
        handler,
        summary=(
            "Schedule an instruction to run later, around a calendar event, or on a webhook "
            "request."
        ),
        open_input_schema=True,
        argument_normalizer=_normalize_cron_arguments,
        unadvertised_parameters=UNADVERTISED_PARAMETERS,
        result_schema={"type": "object"},
        display=ToolDisplay(
            parts_builder=_cron_display_parts,
            fact_builder=result_count_fact_builder("jobs"),
        ),
    )


async def _handle_cron_tool(
    cron_service: CronService,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    arguments = dict(arguments)
    late_limits = arguments.pop(LATE_LIMITS_FIELD, None)
    result = await _run_cron_action(cron_service, context, arguments)
    if late_limits:
        return with_late_limits_note(result, late_limits, REFUSAL_PREFIX)
    return result


async def _run_cron_action(
    cron_service: CronService,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    action = arguments.get("action")
    if action not in CRON_ACTIONS:
        options = ", ".join(sorted(CRON_ACTIONS))
        return tool_failure("invalid_arguments", f"action must be one of: {options}.")
    try:
        if action in _ID_ACTIONS and "id" not in arguments:
            raise CronCallRefusedError(
                refusal(
                    f'{action} needs the job "id"; {{"action":"list"}} shows the ids.',
                    arguments,
                    id="<job id from list>",
                )
            )
        if action == "create":
            return await _handle_create(cron_service, context, arguments)
        if action == "list":
            return _handle_list(cron_service, arguments)
        if action == "update":
            return await _handle_update(cron_service, context, arguments)
        if action == "delete":
            return await _handle_delete(cron_service, arguments)
        if action == "enable":
            return _job_success(
                cron_service, await cron_service.enable_job(arguments["id"], actor="tool")
            )
        return _job_success(
            cron_service, await cron_service.disable_job(arguments["id"], actor="tool")
        )
    except CronCallRefusedError as error:
        return tool_failure("invalid_arguments", str(error))
    except (CronTargetError, InvalidAgentAddressError) as error:
        # Checked before ValueError: missing-target errors are also resolver
        # ValueErrors, and a malformed target address is one too.
        return _target_failure(error)
    except CronJobNotFoundError:
        return tool_failure(
            "job_not_found",
            f'No job has id "{arguments.get("id")}". {{"action":"list"}} shows the current jobs '
            "and their ids.",
        )
    except CronJobInPastError as error:
        return tool_failure("invalid_arguments", _past_message(action, arguments, error))
    except CronJobValidationError as error:
        return tool_failure(
            "invalid_arguments",
            _validation_message(action, arguments, error, context.offers("calendar")),
        )
    except CronServiceError as error:
        _LOGGER.warning("Cron service error for action=%s: %s", action, error)
        return tool_failure(
            "cron_service_error", f"{error}. Do not repeat the same call unchanged."
        )


def _target_failure(error: CronTargetError | InvalidAgentAddressError) -> JsonObject:
    """Report a target failure with its precise code and target guidance."""
    recommendation = _TARGET_ADDRESS_RECOMMENDATION
    if isinstance(error, CronTargetAgentNotFoundError):
        code = "agent_not_found"
    elif isinstance(error, CronTargetProjectNotFoundError):
        code = "project_not_found"
    elif isinstance(error, CronTargetUnavailableError):
        code = "agent_unavailable"
        recommendation = _TARGET_UNAVAILABLE_RECOMMENDATION
    else:
        # A malformed address names no target that could be looked up.
        code = "invalid_arguments"
    return tool_failure(code, f"{str(error).rstrip('. ')}. {recommendation}.")


async def _handle_create(
    cron_service: CronService, context: ToolContext, arguments: JsonObject
) -> JsonObject:
    missing = [name for name in ("prompt", "schedule") if name not in arguments]
    if missing:
        if "schedule" not in missing and "event_id" not in arguments:
            # Show the schedule the job would get: a server time zone drops out.
            with suppress(CronCallRefusedError):
                arguments, _note = _server_time(cron_service, arguments)
        raise CronCallRefusedError(_missing_create_fields(missing, arguments))
    arguments, note = _server_time(cron_service, arguments)
    secret_name = arguments.pop(WEBHOOK_SECRET_FIELD, None)
    agent_id, project_id = _target_agent(context, arguments.get("target", SELF_TARGET))
    parsed = _parse_schedule(cron_service, arguments)
    repeat = arguments.get("repeat")
    if parsed.schedule_type == "once" and "repeat" in arguments and repeat != 1:
        raise CronCallRefusedError(
            refusal(
                f'"{arguments["schedule"]}" fires once, so repeat cannot be {_json(repeat)}. For '
                'several fires use "every <duration>" or five cron fields.',
                arguments,
                repeat=1,
            )
        )
    paused = arguments.get(ENABLED_FIELD) is False
    session_id = (
        _session_id(context, arguments, agent_id, project_id) if "session" in arguments else None
    )
    delivery, delivery_note = _delivery(context, arguments, agent_id, project_id)
    watch = _watch(arguments)
    job = await cron_service.create_job(
        agent_id=agent_id,
        name=arguments.get("name"),
        prompt=arguments["prompt"],
        schedule_type=parsed.schedule_type,
        cron_expression=parsed.cron_expression,
        interval_seconds=parsed.interval_seconds,
        interval_anchor_at=parsed.interval_anchor_at,
        run_at=parsed.run_at,
        event_id=parsed.event_id,
        event_edge=parsed.event_edge,
        event_offset_minutes=parsed.event_offset_minutes,
        remaining_runs=repeat,
        session_id=session_id,
        status="paused" if paused else "active",
        project_id=project_id,
        delivery=delivery.stored(),
        watch=watch,
        actor="tool",
    )
    notes = [note] if note else []
    if delivery_note:
        notes.append(delivery_note)
    if paused:
        notes.append(f'The job is paused; {{"action":"enable","id":"{job.id}"}} starts it.')
    secret = await _new_webhook_secret(cron_service, job, notes)
    if secret_name:
        notes.append(_sent_secret_note(secret_name, shown=secret is not None))
    return _job_success(cron_service, job, notes, secret=secret)


def _handle_list(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    jobs = (
        [cron_service.get_job(arguments["id"])]
        if "id" in arguments
        else cron_service.list_jobs(event_id=arguments.get("event_id"))
    )
    zone = ZoneInfo(cron_service.system_timezone_name())
    data: JsonObject = {"jobs": len(jobs), "timezone": cron_service.system_timezone_name()}
    if jobs:
        data["content"] = "\n\n".join(_job_block(cron_service, job, zone) for job in jobs)
    return tool_success(data)


async def _handle_update(
    cron_service: CronService, context: ToolContext, arguments: JsonObject
) -> JsonObject:
    job_id = arguments["id"]
    # A time zone alone cannot change a job: another zone is refused here, the server's drops.
    arguments, note = _server_time(cron_service, arguments)
    if not any(name in arguments for name in _UPDATE_FIELDS):
        raise CronCallRefusedError(
            refusal(
                "update needs a field to change: name, prompt, schedule, event_id, repeat, "
                "target, session, deliver or watch. To pause or resume the job, use disable or "
                "enable.",
                arguments,
                schedule=_SCHEDULE_STAND_IN,
            )
        )
    secret_name = arguments.pop(WEBHOOK_SECRET_FIELD, None)
    was_webhook = cron_service.get_job(job_id).schedule_type == "webhook"
    updates: dict[str, Any] = {}
    if "target" in arguments:
        updates["agent_id"], updates["project_id"] = _target_agent(context, arguments["target"])
    for name in ("name", "prompt"):
        if name in arguments:
            updates[name] = arguments[name]
    if "schedule" in arguments or "event_id" in arguments:
        parsed = _parse_schedule(cron_service, arguments, cron_service.get_job(job_id))
        updates.update(parsed.as_job_fields())
        if parsed.schedule_type == "once":
            # A one-time schedule fires once; an old repeat count does not carry over.
            repeat = arguments.get("repeat", 1)
            if repeat != 1:
                raise CronCallRefusedError(
                    refusal(
                        f'"{arguments["schedule"]}" fires once, so repeat cannot be '
                        f"{_json(repeat)}.",
                        arguments,
                        repeat=1,
                    )
                )
            updates["remaining_runs"] = 1
    if "repeat" in arguments:
        updates["remaining_runs"] = arguments["repeat"]
    if "session" in arguments or "deliver" in arguments:
        # Both are read against the Agent that runs the job after this update.
        current = cron_service.get_job(job_id)
        agent_id, project_id = (
            (updates["agent_id"], updates["project_id"])
            if "agent_id" in updates
            else (current.agent_id, current.project_id)
        )
        if "session" in arguments:
            updates["session_id"] = _session_id(context, arguments, agent_id, project_id)
        if "deliver" in arguments:
            delivery, _note = _delivery(context, arguments, agent_id, project_id)
            updates["delivery"] = delivery.stored()
    if "watch" in arguments:
        updates["watch"] = _watch(arguments)
    if ENABLED_FIELD in arguments:
        updates["status"] = "active" if arguments[ENABLED_FIELD] else "paused"
    job = await cron_service.update_job(job_id, actor="tool", **updates)
    notes = [note] if note else []
    # A job that just became a webhook job got its secret with this call.
    secret = None if was_webhook else await _new_webhook_secret(cron_service, job, notes)
    if secret_name:
        notes.append(_sent_secret_note(secret_name, shown=secret is not None))
    return _job_success(cron_service, job, notes, secret=secret)


async def _new_webhook_secret(
    cron_service: CronService, job: CronJob, notes: list[str]
) -> str | None:
    """The secret of a webhook job this call made; None for any other job."""
    if job.schedule_type != "webhook":
        return None
    try:
        return await cron_service.webhook_secret(job.id)
    except CronStorageError as error:
        notes.append(
            f"The job's secret could not be read ({str(error).rstrip('. ')}); the user can "
            "show it in vBot's Schedules."
        )
        return None


def _sent_secret_note(name: str, *, shown: bool) -> str:
    if shown:
        return f'"{name}" had no effect: vBot made the job\'s secret, shown as secret.'
    return f'"{name}" had no effect: vBot makes a webhook job\'s secret itself.'


def _target_agent(context: ToolContext, target: str) -> tuple[str, str | None]:
    """The Agent and Project a target names; "self" is the calling Agent."""
    if target == SELF_TARGET:
        return context.agent_id, context.project_id
    return parse_agent_address(target)


def _session_id(
    context: ToolContext, arguments: JsonObject, agent_id: str, project_id: str | None
) -> str | None:
    """The Session a call pins the job to; None starts each run in a new Session."""
    session: str = arguments["session"]
    if session == NEW_SESSION:
        return None
    if session != CURRENT_SESSION:
        return session
    if (agent_id, project_id) != (context.agent_id, context.project_id):
        raise CronCallRefusedError(
            refusal(
                'session "current" is your conversation, so a job of '
                f"{format_agent_address(agent_id, project_id)} cannot continue it.",
                arguments,
                session=OMIT,
            )
        )
    return context.session_id


def _delivery(
    context: ToolContext, arguments: JsonObject, agent_id: str, project_id: str | None
) -> tuple[CronDelivery, str | None]:
    """Where a call sends the job's answers, with a note when here is not this chat.

    here is the chat this Run answers, when the job runs as the Agent whose Channel
    it is; outside a chat it is vBot.
    """
    choice = arguments.get("deliver", HERE)
    if choice != HERE:
        return CronDelivery(kind=choice), None
    surface = context.reply_surface
    chat = (
        surface
        if surface is not None
        and surface.kind == "channel"
        and surface.channel_id
        and surface.platform_target
        else None
    )
    platform = arguments.get(DELIVER_PLATFORM_FIELD)
    if platform is not None and (chat is None or spelling(chat.platform or "") != platform):
        raise CronCallRefusedError(
            refusal(
                f"this conversation is not a {platform} chat, and a job delivers each run's "
                "answer only to this conversation (here), to vBot (app) or nowhere (none).",
                arguments,
                deliver="app",
            )
        )
    if chat is None:
        return CronDelivery(kind="app"), None
    assert chat.channel_id is not None and chat.platform_target is not None
    if (agent_id, project_id) != (context.agent_id, None):
        target = format_agent_address(agent_id, project_id)
        if "deliver" in arguments:
            raise CronCallRefusedError(
                refusal(
                    f"this chat belongs to your Channel, so a job of {target} cannot deliver here.",
                    arguments,
                    deliver="app",
                )
            )
        return (
            CronDelivery(kind="app"),
            f"Each run's answer goes to vBot: this chat belongs to your Channel, not to {target}.",
        )
    return (
        CronDelivery(
            kind="chat",
            channel_id=chat.channel_id,
            platform_target=chat.platform_target,
            thread_id=chat.thread_id,
        ),
        None,
    )


def _watch(arguments: JsonObject) -> CronWatch | None:
    """What a call has the job check before each run: an http(s) URL, else a command."""
    text = arguments.get("watch", NO_WATCH)
    if text == NO_WATCH:
        return None
    kind = "url" if _URL.fullmatch(text) else "command"
    try:
        return CronWatch.parse(kind, text)
    except CronJobValidationError as error:
        problem = (
            f'watch "{text}" is not a valid http or https URL.'
            if kind == "url"
            else f"watch is a command of {len(text)} characters, and a command can have at "
            "most 4000. Save the check as a script file and watch the command that runs it."
        )
        raise CronCallRefusedError(refusal(problem, arguments, watch=WATCH_STAND_IN)) from error


def _stored_watch(cron_service: CronService, arguments: JsonObject) -> CronWatch | None:
    """The watch of the job an update names, before the update."""
    if arguments.get("action") != "update" or not isinstance(arguments.get("id"), str):
        return None
    try:
        return cron_service.get_job(arguments["id"]).watch
    except CronJobNotFoundError:
        return None


async def _with_first_check(
    cron_service: CronService, result: JsonObject, before: CronWatch | None
) -> JsonObject:
    """Wait for the first check of a watch the call set or changed, and show its outcome."""
    data = result.get("data")
    if not result.get("ok") or not isinstance(data, dict) or "watch" not in data:
        return result
    finished = await cron_service.await_first_check(data["id"])
    try:
        job = cron_service.get_job(data["id"])
    except CronJobNotFoundError:
        return result
    if job.watch is None or job.watch == before:
        return result
    notes = [data["note"]] if data.get("note") else []
    if not finished:
        notes.append("The first check is still running; list shows its outcome as last_check.")
    elif job.last_checked_at is not None and job.last_check_error is not None:
        notes.append(
            "The first check failed (last_check_error). Fix watch with update, or the job stops "
            f"after {MAX_CONSECUTIVE_CRON_FAILURES} failed checks in a row."
        )
    elif job.last_checked_at is not None:
        notes.append("The first check recorded the current output; runs start when it changes.")
    return _job_success(cron_service, job, notes)


async def _handle_delete(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job = cron_service.get_job(arguments["id"])
    await cron_service.delete_job(job.id, actor="tool")
    return tool_success({"id": job.id, "name": job.name, "status": "deleted"})


def _server_time(cron_service: CronService, arguments: JsonObject) -> tuple[JsonObject, str | None]:
    server = ZoneInfo(cron_service.system_timezone_name())
    return zoned_schedule(dict(arguments), server, datetime.now(UTC))


def _parse_schedule(
    cron_service: CronService, arguments: JsonObject, job: CronJob | None = None
) -> ParsedSchedule:
    """The schedule a call sets; with an event_id, or for an event job, an event time."""
    schedule = arguments.get("schedule")
    event_id = arguments.get("event_id")
    on_event = job is not None and job.schedule_type == "event"
    if event_id is None and on_event and (schedule is None or event_time_readings(schedule)):
        # The job stays at its event unless the new schedule is of another kind.
        assert job is not None
        event_id = job.event_id
    if event_id is not None:
        return _parse_event_time(cron_service, arguments, event_id, schedule, job)
    # Without an event the call sent a schedule: create requires one, and update
    # parses only when the call sends a schedule or an event_id.
    text: str = arguments["schedule"]
    try:
        return cron_service.parse_schedule(text)
    except CronJobInPastError:
        raise
    except CronJobValidationError as error:
        if event_time_readings(text) == [" ".join(text.split())]:
            # An event time such as "start - 30m" without the event it counts from.
            raise CronCallRefusedError(
                refusal(
                    f'schedule "{schedule}" is a time at a calendar event; send its event_id.',
                    arguments,
                    event_id="<calendar event id>",
                )
            ) from error
        detail = str(error).rstrip(". ")
        raise CronCallRefusedError(
            f'cron was not run: schedule "{schedule}" is not valid ({detail}). {_SCHEDULE_FORMS}'
        ) from error


def _parse_event_time(
    cron_service: CronService,
    arguments: JsonObject,
    event_id: str,
    schedule: str | None,
    job: CronJob | None,
) -> ParsedSchedule:
    if schedule is None:
        # A job moved to another event keeps its event time; another job runs at the start.
        schedule = (
            cron_service.format_schedule(job)
            if job is not None and job.schedule_type == "event"
            else "start"
        )
    readings = event_time_readings(schedule)
    if len(readings) > 1:
        choices = " or ".join(render_call(arguments, schedule=reading) for reading in readings)
        text = ambiguous_event_time("schedule", schedule, readings)
        raise CronCallRefusedError(f"{REFUSAL_PREFIX}{text} {choices}")
    try:
        return cron_service.parse_event_schedule(
            event_id, readings[0] if len(readings) == 1 else schedule
        )
    except CronJobValidationError as error:
        detail = str(error).rstrip(". ")
        raise CronCallRefusedError(
            refusal(
                f'schedule "{schedule}" is not an event time: {detail}.',
                arguments,
                schedule=EVENT_TIME_STAND_IN,
            )
        ) from error


def _missing_create_fields(missing: list[str], arguments: JsonObject) -> str:
    event = "event_id" in arguments
    stand_ins = {
        "prompt": "<instruction>",
        "schedule": EVENT_TIME_STAND_IN if event else _SCHEDULE_STAND_IN,
    }
    texts = []
    if "prompt" in missing:
        texts.append('"prompt", the complete instruction the Agent runs at each fire')
    if "schedule" in missing and event:
        texts.append('"schedule", start or end of the event, optionally +/- a duration')
    elif "schedule" in missing:
        texts.append(f'"schedule". {_SCHEDULE_FORMS}')
    return refusal(
        "create needs " + " and ".join(texts).rstrip(".") + ".",
        arguments,
        **{name: stand_ins[name] for name in missing},
    )


def _past_message(action: str, arguments: JsonObject, error: CronJobInPastError) -> str:
    detail = str(error).rstrip(". ")
    if action == "enable":
        return refusal(
            f"{detail}. Give the job a future time first, then enable it.",
            {"action": "update", "id": arguments.get("id")},
            schedule=_FUTURE_STAND_IN,
        )
    return refusal(f"{detail}.", arguments, schedule=_FUTURE_STAND_IN)


def _validation_message(
    action: str, arguments: JsonObject, error: CronJobValidationError, calendar_offered: bool
) -> str:
    detail = str(error).rstrip(". ")
    if detail.startswith("Calendar event not found"):
        listing = " The calendar list shows events and their ids." if calendar_offered else ""
        return f'cron was not run: no calendar event has id "{arguments.get("event_id")}".{listing}'
    if detail.startswith("repeat is not available"):
        # Only an update of a job bound to an event sends repeat without event_id.
        others = [name for name in _UPDATE_FIELDS if name in arguments and name != "repeat"]
        if others:
            return refusal(EVENT_REPEAT_REFUSAL, arguments, repeat=OMIT)
        return f"{REFUSAL_PREFIX}{EVENT_REPEAT_REFUSAL}"
    if detail.startswith("Session does not exist"):
        return (
            f"{REFUSAL_PREFIX}{detail}. Continue this conversation or start each run in a new "
            f"Session: {render_call(arguments, session=CURRENT_SESSION)} or "
            f"{render_call(arguments, session=OMIT)}"
        )
    if detail.startswith(("delivery", "a job of a Project Agent cannot deliver")):
        # The chat cannot take the job's answers; vBot always can.
        return refusal(f"{detail}.", arguments, deliver="app")
    if detail.startswith("watch applies only"):
        text = 'watch needs a repeating schedule (five cron fields or "every <duration>")'
        alternatives: list[dict[str, Any]] = [{"schedule": _REPEATING_STAND_IN}]
        if action == "create":
            text += "; to keep this schedule, leave watch out"
            alternatives.append({"watch": OMIT})
        calls = " or ".join(render_call(arguments, **fields) for fields in alternatives)
        return f"{REFUSAL_PREFIX}{text}: {calls}"
    if "Completed or missed jobs" in detail:
        return (
            f"cron was not run: {detail}. The job has finished; create a new job instead, or "
            f'delete this one with {{"action":"delete","id":"{arguments.get("id")}"}}.'
        )
    return f"cron was not run: {detail}."


def _job_success(
    cron_service: CronService,
    job: CronJob,
    notes: list[str] | None = None,
    *,
    secret: str | None = None,
) -> JsonObject:
    data = _job_fields(cron_service, job, ZoneInfo(cron_service.system_timezone_name()))
    if secret is not None:
        data["secret"] = secret
        data["authenticate"] = (
            f'Send each request as POST to url with the header "Authorization: Bearer {secret}", '
            "or enter secret as the secret of a GitHub webhook (X-Hub-Signature-256)."
        )
    if notes:
        data["note"] = " ".join(notes)
    return tool_success(data)


def _job_fields(cron_service: CronService, job: CronJob, zone: ZoneInfo) -> JsonObject:
    data: JsonObject = {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "schedule": _schedule_text(cron_service, job, zone),
    }
    if job.schedule_type == "event":
        event = cron_service.bound_event(job)
        data["event"] = job.event_id if event is None else f"{event.title} ({job.event_id})"
    webhook = cron_service.webhook_status(job)
    if webhook is not None:
        if webhook.error is not None:
            data["webhook_error"] = webhook.error
        data["url"] = webhook.url or f"{webhook.path} on vBot's server address"
        if webhook.reach is not None:
            data["reachable_from"] = _WEBHOOK_REACH[webhook.reach]
    # A one-time schedule is its own next run; a webhook job has none.
    if job.schedule_type not in {"once", "webhook"}:
        data["next_run"] = _local_time(cron_service.next_fire_at(job), zone)
    if job.schedule_type == "event" and job.status == "active" and data["next_run"] is None:
        data["next_run"] = "none; no occurrence of the event lies ahead"
    data["target"] = format_agent_address(job.agent_id, job.project_id)
    if job.session_id:
        data["session"] = job.session_id
    data["deliver"] = _delivery_text(job.delivery)
    if job.schedule_type != "once" and job.remaining_runs is not None:
        data["repeat"] = job.remaining_runs
    if job.watch is not None:
        data["watch"] = job.watch.target
    if webhook is not None and webhook.last_received_at:
        data["last_request"] = _local_time(webhook.last_received_at, zone)
    if webhook is not None and webhook.waiting:
        data["requests_waiting"] = webhook.waiting
    last_run = job.last_fired_at or job.last_attempt_at
    if last_run:
        data["last_run"] = _local_time(last_run, zone)
    if job.last_outcome:
        silent = job.last_outcome == "success" and job.last_run_silent
        data["last_outcome"] = "success (answered [SILENT])" if silent else job.last_outcome
    if job.last_error:
        data["last_error"] = job.last_error
    if job.watch is not None and job.last_checked_at:
        data["last_check"] = _local_time(job.last_checked_at, zone)
    if job.watch is not None and job.last_check_error:
        data["last_check_error"] = job.last_check_error
    if job.consecutive_failures:
        data["failures_in_a_row"] = job.consecutive_failures
    return data


def _delivery_text(delivery: CronDelivery | None) -> str:
    if delivery is None or delivery.kind != "chat":
        return "none" if delivery is None else delivery.kind
    thread = f", thread {delivery.thread_id}," if delivery.thread_id else ""
    return f"chat {delivery.platform_target}{thread} of Channel {delivery.channel_id}"


def _job_block(cron_service: CronService, job: CronJob, zone: ZoneInfo) -> str:
    fields = {key: value for key, value in _job_fields(cron_service, job, zone).items() if value}
    lines = [f"{key}: {value}" for key, value in fields.items()]
    prompt_lines = job.prompt.splitlines() or [""]
    lines.append("prompt: " + "\n  ".join(prompt_lines))
    if job.status == "failed":
        lines.append(
            f"note: stopped after {'failed checks or runs' if job.watch else 'failed runs'}; "
            f'{{"action":"enable","id":"{job.id}"}} restarts it.'
        )
    return "\n".join(lines)


def _schedule_text(cron_service: CronService, job: CronJob, zone: ZoneInfo) -> str:
    if job.schedule_type == "once":
        return _local_time(job.run_at, zone) or ""
    return cron_service.format_schedule(job)


def _local_time(value: str | None, zone: ZoneInfo) -> str | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone).replace(microsecond=0).isoformat()


def _json(value: object) -> str:
    return "null" if value is None else str(value)


def _cron_display_parts(raw_arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    # Persisted calls keep the Model's own spelling; label what the call meant.
    try:
        arguments = _normalize_cron_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        return ()
    action = arguments.get("action")
    if not isinstance(action, str) or action not in CRON_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    for field_name in ("name", "id", "target", "schedule", "event_id"):
        value = arguments.get(field_name)
        if isinstance(value, str) and value.strip():
            kind = "identifier" if field_name in {"id", "target", "event_id"} else "text"
            truncate = "middle" if kind == "identifier" else "end"
            parts.append(ToolDisplayPart(value.strip(), kind=kind, truncate=truncate))
            break
    return tuple(parts)


__all__ = [
    "CRON_ACTIONS",
    "CRON_TOOL_DESCRIPTION",
    "CRON_TOOL_NAME",
    "CRON_TOOL_PARAMETERS",
    "register_cron_tool",
]
