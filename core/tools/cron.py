"""Built-in cron tool for managing scheduled automation jobs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, cast

from core.automation.cron import CronJobNotFoundError, CronJobValidationError, CronServiceError
from core.projects import format_agent_address, parse_agent_address
from core.tools.arguments import optional_string, required_string
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

CronScheduleType = Literal["cron", "once"]

CRON_TOOL_NAME = "cron"
CRON_TOOL_DESCRIPTION = (
    "Create and manage persisted schedules that start Runs. Calls are flat: set action to "
    'create, list, update, delete, enable, or disable. List with {"action":"list"}. New '
    "jobs are enabled immediately, use the server timezone, and start a fresh Session on "
    "every fire. Use list to obtain job ids; disable pauses a job without deleting it."
)

CRON_ACTIONS = frozenset(("create", "list", "update", "delete", "enable", "disable"))
CRON_SCHEDULE_TYPES = frozenset(("cron", "once"))

_CREATE_ARGUMENTS = frozenset(
    {
        "target",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
    }
)
_LIST_ARGUMENTS: frozenset[str] = frozenset()
_UPDATE_ARGUMENTS = frozenset(
    {
        "id",
        "target",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
    }
)
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
        'For a recurring job use {"action":"create","prompt":"<instruction>",'
        '"schedule_type":"cron","cron_expression":"0 9 * * *"}. For a one-time job use '
        '{"action":"create","prompt":"<instruction>","schedule_type":"once",'
        '"run_at":"2026-07-25T09:00:00+02:00"}'
    ),
    "list": 'Use {"action":"list"}',
    "update": (
        'Use {"action":"update","id":"<job-id>","prompt":"<replacement instruction>"}; '
        "include only fields that should change"
    ),
    "delete": 'Use {"action":"delete","id":"<job-id>"}',
    "enable": 'Use {"action":"enable","id":"<job-id>"}',
    "disable": 'Use {"action":"disable","id":"<job-id>"}',
}

CRON_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(CRON_ACTIONS),
            "description": (
                "Operation to perform. Use list by itself to obtain current job ids before "
                "changing or deleting a job."
            ),
        },
        "id": {
            "type": "string",
            "description": (
                "Existing job id from list. Required for update, delete, enable, and disable."
            ),
        },
        "target": {
            "type": "string",
            "description": (
                "Agent address for create or update: agent or agent@project. Create defaults "
                "to the current Agent and Project."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "Self-contained instruction for create, or a replacement instruction for "
                "update. Required for create because every fire starts a fresh Session."
            ),
        },
        "schedule_type": {
            "type": "string",
            "enum": sorted(CRON_SCHEDULE_TYPES),
            "description": (
                "Schedule type for create or update: cron for recurring, once for one fire. "
                "Required for create."
            ),
        },
        "cron_expression": {
            "type": "string",
            "description": (
                "Recurring schedule for create or update. Exactly five fields: minute hour "
                "day-of-month month day-of-week. Required with schedule_type cron. Seconds "
                "are unsupported; minimum cadence is one minute."
            ),
        },
        "run_at": {
            "type": "string",
            "description": (
                "One-time ISO 8601 date-time for create or update. Required with "
                "schedule_type once; a value without an offset uses the server timezone."
            ),
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

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
        display=ToolDisplay(summary_builder=_cron_display_summary),
    )


def _handle_cron_tool(
    cron_service: CronService,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    operation = _extract_operation(arguments)
    if isinstance(operation, str):
        return tool_failure("invalid_arguments", operation, retryable=False)
    action, operation_arguments = operation

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
    prompt = required_string(arguments.get("prompt"), field_name="prompt")
    schedule_type = _required_enum(
        arguments.get("schedule_type"),
        field_name="schedule_type",
        allowed=CRON_SCHEDULE_TYPES,
    )

    cron_expression = optional_string(
        arguments.get("cron_expression"), field_name="cron_expression"
    )
    run_at = optional_string(arguments.get("run_at"), field_name="run_at")
    if schedule_type == "cron":
        if cron_expression is None:
            raise ValueError("cron_expression is required when schedule_type is 'cron'")
        run_at = None
    else:
        if run_at is None:
            raise ValueError("run_at is required when schedule_type is 'once'")
        cron_expression = None

    job = cron_service.create_job(
        agent_id=agent_id,
        prompt=prompt,
        schedule_type=cast(CronScheduleType, schedule_type),
        cron_expression=cron_expression,
        run_at=run_at,
        session_id=None,
        project_id=project_id,
    )
    return tool_success({"job": _job_payload(cron_service, job)})


def _handle_list(cron_service: CronService) -> JsonObject:
    jobs = [_job_payload(cron_service, job) for job in cron_service.list_jobs()]
    return tool_success({"jobs": jobs, "system_timezone": cron_service.system_timezone_name()})


def _handle_update(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = required_string(arguments.get("id"), field_name="id")
    updates: dict[str, str | None] = {}

    if "target" in arguments:
        target = required_string(
            arguments.get("target"),
            field_name="target",
        )
        agent_id, project_id = parse_agent_address(target)
        updates["agent_id"] = agent_id
        updates["project_id"] = project_id
    if "prompt" in arguments:
        updates["prompt"] = required_string(arguments.get("prompt"), field_name="prompt")
    if "schedule_type" in arguments:
        updates["schedule_type"] = _required_enum(
            arguments.get("schedule_type"),
            field_name="schedule_type",
            allowed=CRON_SCHEDULE_TYPES,
        )
    if "cron_expression" in arguments:
        updates["cron_expression"] = required_string(
            arguments.get("cron_expression"), field_name="cron_expression"
        )
    if "run_at" in arguments:
        updates["run_at"] = required_string(arguments.get("run_at"), field_name="run_at")
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
    payload["next_fire_at"] = cron_service.next_fire_at(job)
    return payload


def _extract_operation(arguments: JsonObject) -> tuple[str, JsonObject] | str:
    """Normalize the flat action contract and the legacy operation envelope."""

    if "action" in arguments:
        action = arguments.get("action")
        if not isinstance(action, str) or action not in CRON_ACTIONS:
            return (
                "action must be one of: create, delete, disable, enable, list, update. "
                'To inspect jobs use {"action":"list"}'
            )
        action_arguments = {key: value for key, value in arguments.items() if key != "action"}
        if "agent_id" in action_arguments:
            if "target" in action_arguments:
                return "Use only target, not both target and legacy agent_id"
            action_arguments["target"] = action_arguments.pop("agent_id")
        return action, action_arguments

    if len(arguments) != 1:
        return (
            "Missing flat action. Set action to create, list, update, delete, enable, or "
            'disable. To inspect jobs use {"action":"list"}'
        )
    action, operation_arguments = next(iter(arguments.items()))
    if action not in CRON_ACTIONS:
        return (
            f"Unknown action: {action}. Set action to create, list, update, delete, enable, "
            'or disable. To inspect jobs use {"action":"list"}'
        )

    normalized_arguments = _normalize_legacy_operation_arguments(action, operation_arguments)
    if isinstance(normalized_arguments, str):
        return normalized_arguments
    return action, normalized_arguments


def _normalize_legacy_operation_arguments(
    action: str, operation_arguments: object
) -> JsonObject | str:
    """Accept the retired envelope without making its quirks model-facing."""

    if isinstance(operation_arguments, dict):
        return dict(operation_arguments)

    if action == "list" and operation_arguments in (None, True, ""):
        return {}

    if isinstance(operation_arguments, str):
        try:
            decoded_arguments = json.loads(operation_arguments)
        except json.JSONDecodeError:
            decoded_arguments = None
        if isinstance(decoded_arguments, dict):
            return decoded_arguments

    return _with_action_recommendation(
        action,
        f"Legacy field '{action}' could not be interpreted as operation arguments",
    )


def _with_action_recommendation(action: str, message: str) -> str:
    recommendation = _ACTION_RECOMMENDATIONS[action]
    return f"{message.rstrip('. ')}. {recommendation}"


def _cron_display_summary(arguments: JsonObject) -> str:
    operation = _extract_operation(arguments)
    if isinstance(operation, str):
        return ""
    action, operation_arguments = operation
    parts = [action]
    for field_name in ("id", "target", "schedule_type"):
        value = operation_arguments.get(field_name)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " · ".join(parts)


def _required_enum(value: object, *, field_name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {options}")
    return value


__all__ = [
    "CRON_ACTIONS",
    "CRON_TOOL_DESCRIPTION",
    "CRON_TOOL_NAME",
    "CRON_TOOL_PARAMETERS",
    "register_cron_tool",
]
