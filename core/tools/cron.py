"""Built-in cron tool for managing scheduled automation jobs."""

from __future__ import annotations

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
    "Create and manage persisted schedules that start Runs. Each call must contain exactly "
    "one operation. New jobs are enabled immediately, use the server timezone, and start a "
    "fresh Session on every fire. Use list to obtain job ids; disable pauses a job without "
    "deleting it."
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

CRON_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "create": {
            "type": "object",
            "description": (
                "Create an enabled schedule. The prompt must be self-contained because each "
                "fire starts a fresh Session."
            ),
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Agent address that runs the prompt: agent or agent@project. Omit to "
                        "use the current Agent and Project."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Self-contained instruction sent to the target Agent on every fire."
                    ),
                },
                "schedule_type": {
                    "type": "string",
                    "enum": sorted(CRON_SCHEDULE_TYPES),
                    "description": "cron for a recurring schedule; once for one execution.",
                },
                "cron_expression": {
                    "type": "string",
                    "description": (
                        "Required for cron. Exactly five fields: minute hour day-of-month "
                        "month day-of-week. Seconds are unsupported; minimum cadence is one "
                        "minute."
                    ),
                },
                "run_at": {
                    "type": "string",
                    "description": (
                        "Required for once. ISO 8601 date-time; a value without an offset uses "
                        "the server timezone."
                    ),
                },
            },
            "required": ["prompt", "schedule_type"],
            "additionalProperties": False,
        },
        "list": {
            "type": "object",
            "description": (
                "List all jobs, including terminal history, ids, schedules, status, and the "
                "last Run outcome."
            ),
            "properties": {},
            "additionalProperties": False,
        },
        "update": {
            "type": "object",
            "description": (
                "Change one job. Include id and at least one field to change. Use enable or "
                "disable for status changes."
            ),
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Job id returned by create or list.",
                },
                "target": {
                    "type": "string",
                    "description": "Replacement target address: agent or agent@project.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Replacement self-contained instruction for every fire.",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": sorted(CRON_SCHEDULE_TYPES),
                    "description": "cron for a recurring schedule; once for one execution.",
                },
                "cron_expression": {
                    "type": "string",
                    "description": (
                        "Recurring five-field expression. Required when changing to cron."
                    ),
                },
                "run_at": {
                    "type": "string",
                    "description": (
                        "One-time ISO 8601 date-time. Required when changing to once; a value "
                        "without an offset uses the server timezone."
                    ),
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "delete": {
            "type": "object",
            "description": "Permanently delete one job. Use disable to pause it reversibly.",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Job id returned by create or list.",
                }
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "enable": {
            "type": "object",
            "description": "Enable a paused or failed job.",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Job id returned by create or list.",
                }
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "disable": {
            "type": "object",
            "description": "Pause a job without deleting it.",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Job id returned by create or list.",
                }
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    "minProperties": 1,
    "maxProperties": 1,
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
        return tool_failure(
            "invalid_arguments",
            f"Unknown argument(s) for action '{action}': {names}",
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
        return tool_failure("invalid_arguments", str(error), retryable=False)
    except CronJobNotFoundError as error:
        return tool_failure("job_not_found", str(error), retryable=False)
    except CronJobValidationError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)
    except CronServiceError as error:
        _LOGGER.warning("Cron service error for action=%s: %s", action, error)
        return tool_failure("cron_service_error", str(error))


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
    """Normalize the operation envelope and the legacy flat action shape."""

    if "action" in arguments:
        action = arguments.get("action")
        if not isinstance(action, str) or action not in CRON_ACTIONS:
            return "action must be one of: create, delete, disable, enable, list, update"
        legacy_arguments = {key: value for key, value in arguments.items() if key != "action"}
        if "agent_id" in legacy_arguments:
            if "target" in legacy_arguments:
                return "Use only target, not both target and legacy agent_id"
            legacy_arguments["target"] = legacy_arguments.pop("agent_id")
        return action, legacy_arguments

    if len(arguments) != 1:
        return "Exactly one operation is required: create, delete, disable, enable, list, or update"
    action, operation_arguments = next(iter(arguments.items()))
    if action not in CRON_ACTIONS:
        return f"Unknown operation: {action}"
    if not isinstance(operation_arguments, dict):
        return f"{action} must be a JSON object"
    return action, dict(operation_arguments)


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
