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
    "Manage scheduled Runs. A cron schedule uses exactly five fields (minute, hour, "
    "day-of-month, month, day-of-week; minimum cadence one minute); once uses an ISO 8601 "
    "run_at interpreted in timezone when no offset is present. Omit agent_id to target the "
    "current Agent and Project, or use agent / agent@project explicitly. Omit session_id to "
    "create a fresh Session for every fire; otherwise it must name an existing Session. "
    "Missed once jobs do not catch up after restart. List includes terminal history and the "
    "last Run outcome."
)

CRON_ACTIONS = frozenset(("create", "list", "update", "delete", "enable", "disable"))
CRON_SCHEDULE_TYPES = frozenset(("cron", "once"))
CRON_STATUSES = frozenset(("active", "paused"))

_CREATE_ARGUMENTS = frozenset(
    {
        "action",
        "agent_id",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
        "timezone",
        "session_id",
    }
)
_LIST_ARGUMENTS = frozenset({"action"})
_UPDATE_ARGUMENTS = frozenset(
    {
        "action",
        "id",
        "agent_id",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
        "timezone",
        "session_id",
        "status",
    }
)
_ID_ONLY_ARGUMENTS = frozenset({"action", "id"})
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
        "action": {
            "type": "string",
            "enum": sorted(CRON_ACTIONS),
            "description": "Action to perform for cron job management.",
        },
        "id": {
            "type": "string",
            "description": "Cron job id for update/delete/enable/disable actions.",
        },
        "agent_id": {
            "type": "string",
            "description": (
                "Optional target address (agent or agent@project). Create defaults to the "
                "current Agent and Project."
            ),
        },
        "prompt": {
            "type": "string",
            "description": "Prompt sent to the target agent when the schedule fires.",
        },
        "schedule_type": {
            "type": "string",
            "enum": sorted(CRON_SCHEDULE_TYPES),
            "description": "Schedule type for create/update actions: cron or once.",
        },
        "cron_expression": {
            "type": "string",
            "description": (
                "Exactly five cron fields: minute hour day-of-month month day-of-week. "
                "The minimum cadence is one minute."
            ),
        },
        "run_at": {
            "type": "string",
            "description": (
                "ISO 8601 timestamp for once jobs. A timestamp without an offset is "
                "interpreted in timezone."
            ),
        },
        "timezone": {
            "type": "string",
            "description": "Optional IANA timezone name. Defaults to system timezone.",
        },
        "session_id": {
            "type": "string",
            "description": (
                "Optional existing Session id owned by the target. Omit it to create a fresh "
                "Session for every fire."
            ),
        },
        "status": {
            "type": "string",
            "enum": sorted(CRON_STATUSES),
            "description": "Optional status for update action.",
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
        display=ToolDisplay(summary_fields=("action", "id", "agent_id", "schedule_type")),
    )


def _handle_cron_tool(
    cron_service: CronService,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    action_value = arguments.get("action")
    if not isinstance(action_value, str) or action_value not in CRON_ACTIONS:
        return tool_failure(
            "invalid_arguments",
            "action must be one of: create, delete, disable, enable, list, update",
        )

    action = action_value
    unknown_arguments = sorted(set(arguments) - _ACTION_ARGUMENTS[action])
    if unknown_arguments:
        names = ", ".join(unknown_arguments)
        return tool_failure(
            "invalid_arguments",
            f"Unknown argument(s) for action '{action}': {names}",
        )

    try:
        if action == "create":
            return _handle_create(cron_service, context, arguments)
        if action == "list":
            return _handle_list(cron_service)
        if action == "update":
            return _handle_update(cron_service, arguments)
        if action == "delete":
            return _handle_delete(cron_service, arguments)
        if action == "enable":
            return _handle_enable(cron_service, arguments)
        return _handle_disable(cron_service, arguments)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    except CronJobNotFoundError as error:
        return tool_failure("job_not_found", str(error))
    except CronJobValidationError as error:
        return tool_failure("invalid_arguments", str(error))
    except CronServiceError as error:
        _LOGGER.warning("Cron service error for action=%s: %s", action, error)
        return tool_failure("cron_service_error", str(error))


def _handle_create(
    cron_service: CronService, context: ToolContext, arguments: JsonObject
) -> JsonObject:
    target = optional_string(arguments.get("agent_id"), field_name="agent_id")
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
    timezone = optional_string(arguments.get("timezone"), field_name="timezone")
    session_id = optional_string(arguments.get("session_id"), field_name="session_id")

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
        timezone=timezone,
        session_id=session_id,
        project_id=project_id,
    )
    return tool_success({"job": _job_payload(cron_service, job)})


def _handle_list(cron_service: CronService) -> JsonObject:
    jobs = [_job_payload(cron_service, job) for job in cron_service.list_jobs()]
    return tool_success({"jobs": jobs, "system_timezone": cron_service.system_timezone_name()})


def _handle_update(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = required_string(arguments.get("id"), field_name="id")
    updates: dict[str, str | None] = {}

    if "agent_id" in arguments:
        target = required_string(
            arguments.get("agent_id"),
            field_name="agent_id",
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
    if "timezone" in arguments:
        updates["timezone"] = optional_string(arguments.get("timezone"), field_name="timezone")
    if "session_id" in arguments:
        updates["session_id"] = optional_string(
            arguments.get("session_id"), field_name="session_id"
        )
    if "status" in arguments:
        updates["status"] = _required_enum(
            arguments.get("status"),
            field_name="status",
            allowed=CRON_STATUSES,
        )

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
    payload["effective_timezone"] = cron_service.effective_timezone_name(job)
    payload["next_fire_at"] = cron_service.next_fire_at(job)
    return payload


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
