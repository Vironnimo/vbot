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
    extract_tool_operation,
    operation_envelope_schema,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.cron import CronJob, CronService

CronScheduleType = Literal["cron", "once"]

CRON_TOOL_NAME = "cron"
CRON_TOOL_DESCRIPTION = (
    "Create and manage named persisted schedules that start Runs. Set request.operation to "
    "create, list, update, delete, enable, or disable. New "
    "jobs are enabled immediately, use the server timezone, and start a fresh Session on "
    "every fire. Use list to obtain job ids; disable pauses a job without deleting it."
)

CRON_ACTIONS = frozenset(("create", "list", "update", "delete", "enable", "disable"))
CRON_SCHEDULE_TYPES = frozenset(("cron", "once"))

_CREATE_ARGUMENTS = frozenset(
    {
        "target",
        "name",
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
        "name",
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
        'For a recurring job use {"request":{"operation":"create","name":"<job name>",'
        '"prompt":"<instruction>",'
        '"schedule_type":"cron","cron_expression":"0 9 * * *"}}. For a one-time job use '
        '{"request":{"operation":"create","name":"<job name>","prompt":"<instruction>",'
        '"schedule_type":"once",'
        '"run_at":"2026-07-25T09:00:00+02:00"}}'
    ),
    "list": 'Use {"request":{"operation":"list"}}',
    "update": (
        'Use {"request":{"operation":"update","id":"<job-id>","name":"<replacement name>"}}; '
        "include only fields that should change"
    ),
    "delete": 'Use {"request":{"operation":"delete","id":"<job-id>"}}',
    "enable": 'Use {"request":{"operation":"enable","id":"<job-id>"}}',
    "disable": 'Use {"request":{"operation":"disable","id":"<job-id>"}}',
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
        "Agent address: agent or agent@project. Create defaults to the current Agent and Project."
    ),
}
_CRON_NAME_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Human-readable job name. It does not need to be unique.",
}
_CRON_PROMPT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Self-contained instruction because every fire starts a fresh Session.",
}
_CRON_SCHEDULE_TYPE_PARAMETER: JsonObject = {
    "type": "string",
    "enum": sorted(CRON_SCHEDULE_TYPES),
    "description": "cron for a recurring schedule; once for one fire.",
}
_CRON_EXPRESSION_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Recurring five-field schedule: minute hour day-of-month month day-of-week. "
        "Seconds are unsupported; minimum cadence is one minute."
    ),
}
_CRON_RUN_AT_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "One-time ISO 8601 date-time. A value without an offset uses the server timezone."
    ),
}


def _cron_operation(
    description: str,
    properties: JsonObject,
    *,
    required: tuple[str, ...] = (),
) -> JsonObject:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


CRON_TOOL_PARAMETERS: JsonObject = operation_envelope_schema(
    {
        "create": {
            "type": "object",
            "oneOf": [
                _cron_operation(
                    "Create and immediately enable one recurring schedule.",
                    {
                        "target": _CRON_TARGET_PARAMETER,
                        "name": _CRON_NAME_PARAMETER,
                        "prompt": _CRON_PROMPT_PARAMETER,
                        "schedule_type": {"type": "string", "enum": ["cron"]},
                        "cron_expression": _CRON_EXPRESSION_PARAMETER,
                    },
                    required=("name", "prompt", "schedule_type", "cron_expression"),
                ),
                _cron_operation(
                    "Create and immediately enable one one-time schedule.",
                    {
                        "target": _CRON_TARGET_PARAMETER,
                        "name": _CRON_NAME_PARAMETER,
                        "prompt": _CRON_PROMPT_PARAMETER,
                        "schedule_type": {"type": "string", "enum": ["once"]},
                        "run_at": _CRON_RUN_AT_PARAMETER,
                    },
                    required=("name", "prompt", "schedule_type", "run_at"),
                ),
            ],
        },
        "list": _cron_operation(
            "List current jobs and obtain ids for later operations.",
            {},
        ),
        "update": {
            **_cron_operation(
                "Update an existing job. Include only fields that should change.",
                {
                    "id": _CRON_ID_PARAMETER,
                    "target": _CRON_TARGET_PARAMETER,
                    "name": _CRON_NAME_PARAMETER,
                    "prompt": _CRON_PROMPT_PARAMETER,
                    "schedule_type": _CRON_SCHEDULE_TYPE_PARAMETER,
                    "cron_expression": _CRON_EXPRESSION_PARAMETER,
                    "run_at": _CRON_RUN_AT_PARAMETER,
                },
                required=("id",),
            ),
            "minProperties": 2,
        },
        "delete": _cron_operation(
            "Delete an existing job.",
            {"id": _CRON_ID_PARAMETER},
            required=("id",),
        ),
        "enable": _cron_operation(
            "Enable a paused job.",
            {"id": _CRON_ID_PARAMETER},
            required=("id",),
        ),
        "disable": _cron_operation(
            "Pause a job without deleting it.",
            {"id": _CRON_ID_PARAMETER},
            required=("id",),
        ),
    },
    description=(
        "Set request.operation to exactly one supported operation and include only that "
        "operation's fields."
    ),
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
            f'{error}. Use {{"list":{{}}}} to get current job ids',
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
    name = required_string(arguments.get("name"), field_name="name")
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
        name=name,
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
    if "name" in arguments:
        updates["name"] = required_string(arguments.get("name"), field_name="name")
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
    """Extract the canonical request discriminator with actionable recovery text."""
    try:
        return extract_tool_operation(arguments, sorted(CRON_ACTIONS))
    except ValueError as error:
        return f"{error}. {_ACTION_RECOMMENDATIONS['list']}"


def _with_action_recommendation(action: str, message: str) -> str:
    recommendation = _ACTION_RECOMMENDATIONS[action]
    return f"{message.rstrip('. ')}. {recommendation}"


def _cron_display_summary(arguments: JsonObject) -> str:
    operation = _extract_operation(arguments)
    if isinstance(operation, str):
        return ""
    action, operation_arguments = operation
    parts = [action]
    for field_name in ("name", "id", "target", "schedule_type"):
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
