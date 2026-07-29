"""Built-in cron tool for managing scheduled automation jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.automation.cron import CronJobNotFoundError, CronJobValidationError, CronServiceError
from core.projects import format_agent_address, parse_agent_address
from core.tools.arguments import optional_string, required_string
from core.tools.contracts import action_schema
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.cron import CronJob, CronService

CRON_TOOL_NAME = "cron"
CRON_TOOL_DESCRIPTION = (
    "Create and manage persisted schedules that start Runs. Set action to create, list, "
    "update, delete, enable, or disable. New jobs are enabled immediately, use the server "
    "timezone, and start a fresh Session on every fire. Use list to obtain job ids; disable "
    "pauses a job without deleting it."
)

CRON_ACTIONS = frozenset(("create", "list", "update", "delete", "enable", "disable"))

_CREATE_ARGUMENTS = frozenset({"target", "name", "prompt", "schedule", "repeat"})
_LIST_ARGUMENTS: frozenset[str] = frozenset()
_UPDATE_ARGUMENTS = frozenset({"id", "target", "name", "prompt", "schedule", "repeat"})
_ID_ONLY_ARGUMENTS = frozenset({"id"})
_ACTION_ARGUMENTS: dict[str, frozenset[str]] = {
    "create": _CREATE_ARGUMENTS,
    "list": _LIST_ARGUMENTS,
    "update": _UPDATE_ARGUMENTS,
    "delete": _ID_ONLY_ARGUMENTS,
    "enable": _ID_ONLY_ARGUMENTS,
    "disable": _ID_ONLY_ARGUMENTS,
}
_ACTION_RECOMMENDATIONS = {
    "create": (
        'Use {"action":"create","prompt":"<instruction>","schedule":"every 2h"} for an '
        'interval, {"action":"create","prompt":"<instruction>","schedule":"0 9 * * *"} '
        'for cron, or an ISO timestamp / "in 30m" for one fire'
    ),
    "list": 'Use {"action":"list"}',
    "update": (
        'Use {"action":"update","id":"<job-id>","schedule":"every 4h"}; include only fields '
        "that should change"
    ),
    "delete": 'Use {"action":"delete","id":"<job-id>"}',
    "enable": 'Use {"action":"enable","id":"<job-id>"}',
    "disable": 'Use {"action":"disable","id":"<job-id>"}',
}

_CRON_ID_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Existing job id returned by list.",
}
_CRON_TARGET_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Target Agent address for create or update: agent or agent@project. Omit on create "
        "to use the current Agent and Project; omit on update to keep the existing target."
    ),
}
_CRON_NAME_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Optional human-readable job name for create or update. If omitted on create, a stable "
        "name is derived from the first useful prompt line."
    ),
}
_CRON_PROMPT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Instruction for create or update. Required on create and must be self-contained "
        "because every fire starts a fresh Session. Omit on update to keep the existing prompt."
    ),
}
_CRON_SCHEDULE_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Schedule for create or update: ISO 8601 timestamp, 'in <duration>', "
        "'every <duration>', or exactly five cron fields. Durations use a positive whole "
        "number plus m, h, or d. Bare durations, fuzzy dates, and six-field cron are invalid. "
        "Omit on update to keep the existing schedule."
    ),
}
_CRON_REPEAT_PARAMETER: JsonObject = {
    "type": ["integer", "null"],
    "minimum": 1,
    "description": (
        "Number of future fires, including the next one. A positive integer sets the count. "
        "On update, omit repeat to keep the current count or set it to null to make a recurring "
        "job unlimited. On create, omit it for an unlimited recurring job. One-time schedules "
        "accept only 1 and never null."
    ),
}

CRON_TOOL_PARAMETERS: JsonObject = action_schema(
    {
        "create": {
            "type": "object",
            "description": "Create and immediately enable one persisted schedule.",
            "properties": {
                "target": _CRON_TARGET_PARAMETER,
                "name": _CRON_NAME_PARAMETER,
                "prompt": _CRON_PROMPT_PARAMETER,
                "schedule": _CRON_SCHEDULE_PARAMETER,
                "repeat": _CRON_REPEAT_PARAMETER,
            },
            "required": ["prompt", "schedule"],
        },
        "list": {
            "type": "object",
            "description": "List all persisted schedules and their current ids and state.",
            "properties": {},
            "required": [],
        },
        "update": {
            "type": "object",
            "description": (
                "Update one schedule. Include id and at least one field that should change."
            ),
            "properties": {
                "id": _CRON_ID_PARAMETER,
                "target": _CRON_TARGET_PARAMETER,
                "name": _CRON_NAME_PARAMETER,
                "prompt": _CRON_PROMPT_PARAMETER,
                "schedule": _CRON_SCHEDULE_PARAMETER,
                "repeat": _CRON_REPEAT_PARAMETER,
            },
            "required": ["id"],
            "minProperties": 3,
        },
        "delete": {
            "type": "object",
            "description": "Delete one persisted schedule.",
            "properties": {"id": _CRON_ID_PARAMETER},
            "required": ["id"],
        },
        "enable": {
            "type": "object",
            "description": "Enable one persisted schedule.",
            "properties": {"id": _CRON_ID_PARAMETER},
            "required": ["id"],
        },
        "disable": {
            "type": "object",
            "description": "Pause one persisted schedule without deleting it.",
            "properties": {"id": _CRON_ID_PARAMETER},
            "required": ["id"],
        },
    },
    description=(
        "Flat action interface. Each action exposes only its valid arguments and "
        "structurally requires every field it needs."
    ),
    action_description=("Create, list, update, delete, enable, or disable a persisted schedule."),
)

_LOGGER = get_logger("tools.cron")


def register_cron_tool(registry: ToolRegistry, cron_service: CronService) -> None:
    """Register the cron tool with a vBot tool registry."""

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return _handle_cron_tool(cron_service, context, arguments)

    registry.register(
        CRON_TOOL_NAME,
        CRON_TOOL_DESCRIPTION,
        CRON_TOOL_PARAMETERS,
        handler,
        result_schema={"type": "object"},
        display=ToolDisplay(summary_builder=_cron_display_summary),
    )


def _handle_cron_tool(
    cron_service: CronService,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    raw_action = arguments.get("action")
    if not isinstance(raw_action, str) or raw_action not in CRON_ACTIONS:
        options = ", ".join(sorted(CRON_ACTIONS))
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
        if action == "create":
            return _handle_create(cron_service, context, operation_arguments)
        if action == "list":
            return _handle_list(cron_service)
        if action == "update":
            return _handle_update(cron_service, operation_arguments)
        if action == "delete":
            return _handle_delete(cron_service, operation_arguments)
        if action == "enable":
            return _handle_enable(cron_service, operation_arguments)
        return _handle_disable(cron_service, operation_arguments)
    except ValueError as error:
        return tool_failure(
            "invalid_arguments",
            _with_action_recommendation(action, str(error)),
            retryable=False,
        )
    except CronJobNotFoundError as error:
        return tool_failure(
            "job_not_found",
            f'{error}. Use {{"action":"list"}} to get current job ids',
            retryable=False,
        )
    except CronJobValidationError as error:
        return tool_failure(
            "invalid_arguments",
            _with_action_recommendation(action, str(error)),
            retryable=False,
        )
    except CronServiceError as error:
        _LOGGER.warning("Cron service error for action=%s: %s", action, error)
        return tool_failure(
            "cron_service_error",
            f"{error}. Do not repeat the same call unchanged",
            retryable=False,
        )


def _handle_create(
    cron_service: CronService, context: ToolContext, arguments: JsonObject
) -> JsonObject:
    target = optional_string(arguments.get("target"), field_name="target")
    if target is None:
        agent_id, project_id = context.agent_id, context.project_id
    else:
        agent_id, project_id = parse_agent_address(target)
    name = optional_string(arguments.get("name"), field_name="name")
    prompt = required_string(arguments.get("prompt"), field_name="prompt")
    schedule = required_string(arguments.get("schedule"), field_name="schedule")
    parsed_schedule = cron_service.parse_schedule(schedule)
    repeat = _optional_positive_integer(arguments.get("repeat"), field_name="repeat")
    if parsed_schedule.schedule_type == "once":
        if "repeat" in arguments and repeat is None:
            raise ValueError("repeat cannot be null for a one-time schedule; omit it or use 1")
        if repeat not in {None, 1}:
            raise ValueError("repeat must be 1 for a one-time schedule")

    job = cron_service.create_job(
        agent_id=agent_id,
        name=name,
        prompt=prompt,
        schedule_type=parsed_schedule.schedule_type,
        cron_expression=parsed_schedule.cron_expression,
        interval_seconds=parsed_schedule.interval_seconds,
        interval_anchor_at=parsed_schedule.interval_anchor_at,
        run_at=parsed_schedule.run_at,
        remaining_runs=repeat,
        session_id=None,
        project_id=project_id,
    )
    return tool_success({"job": _job_payload(cron_service, job)})


def _handle_list(cron_service: CronService) -> JsonObject:
    jobs = [_job_payload(cron_service, job) for job in cron_service.list_jobs()]
    return tool_success({"jobs": jobs, "system_timezone": cron_service.system_timezone_name()})


def _handle_update(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = required_string(arguments.get("id"), field_name="id")
    updates: dict[str, str | int | None] = {}

    if "target" in arguments:
        target = required_string(
            arguments.get("target"),
            field_name="target",
        )
        agent_id, project_id = parse_agent_address(target)
        updates["agent_id"] = agent_id
        updates["project_id"] = project_id
    if "name" in arguments:
        updates["name"] = required_string(arguments.get("name"), field_name="name")
    if "prompt" in arguments:
        updates["prompt"] = required_string(arguments.get("prompt"), field_name="prompt")
    parsed_schedule = None
    if "schedule" in arguments:
        schedule = required_string(arguments.get("schedule"), field_name="schedule")
        parsed_schedule = cron_service.parse_schedule(schedule)
        updates.update(parsed_schedule.as_job_fields())
    if "repeat" in arguments:
        repeat = _optional_positive_integer(arguments.get("repeat"), field_name="repeat")
        if parsed_schedule is not None and parsed_schedule.schedule_type == "once":
            if repeat is None:
                raise ValueError("repeat cannot be null for a one-time schedule; use 1")
            if repeat != 1:
                raise ValueError("repeat must be 1 for a one-time schedule")
        updates["remaining_runs"] = repeat
    if not updates:
        raise ValueError("update requires at least one field to change")

    job = cron_service.update_job(job_id, **updates)
    return tool_success({"job": _job_payload(cron_service, job)})


def _handle_delete(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = required_string(arguments.get("id"), field_name="id")
    cron_service.delete_job(job_id)
    return tool_success({"id": job_id, "deleted": True})


def _handle_enable(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = required_string(arguments.get("id"), field_name="id")
    job = cron_service.enable_job(job_id)
    return tool_success({"job": _job_payload(cron_service, job)})


def _handle_disable(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = required_string(arguments.get("id"), field_name="id")
    job = cron_service.disable_job(job_id)
    return tool_success({"job": _job_payload(cron_service, job)})


def _job_payload(cron_service: CronService, job: CronJob) -> JsonObject:
    payload = dict(job.to_dict())
    payload["target"] = format_agent_address(job.agent_id, job.project_id)
    payload["schedule"] = cron_service.format_schedule(job)
    payload["next_fire_at"] = cron_service.next_fire_at(job)
    return payload


def _with_action_recommendation(action: str, message: str) -> str:
    recommendation = _ACTION_RECOMMENDATIONS[action]
    return f"{message.rstrip('. ')}. {recommendation}"


def _cron_display_summary(arguments: JsonObject) -> str:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in CRON_ACTIONS:
        return ""
    parts = [action]
    for field_name in ("name", "id", "target", "schedule"):
        value = arguments.get(field_name)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " · ".join(parts)


def _optional_positive_integer(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


__all__ = [
    "CRON_ACTIONS",
    "CRON_TOOL_DESCRIPTION",
    "CRON_TOOL_NAME",
    "CRON_TOOL_PARAMETERS",
    "register_cron_tool",
]
