"""Built-in cron tool for managing scheduled automation jobs."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from core.automation.cron import CronJobNotFoundError, CronJobValidationError, CronServiceError
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.cron import CronJob, CronService

CronAction = Literal["create", "list", "update", "delete", "enable", "disable"]

CRON_TOOL_NAME = "cron"
CRON_TOOL_DESCRIPTION = "Create, list, update, delete, enable, and disable scheduled cron jobs."

CRON_ACTIONS = frozenset(("create", "list", "update", "delete", "enable", "disable"))
CRON_SCHEDULE_TYPES = frozenset(("cron", "once"))
CRON_STATUSES = frozenset(("active", "paused", "completed"))

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
            "description": "Target agent id for create/update actions.",
        },
        "prompt": {
            "type": "string",
            "description": "Prompt to trigger when the schedule fires.",
        },
        "schedule_type": {
            "type": "string",
            "enum": sorted(CRON_SCHEDULE_TYPES),
            "description": "Schedule type for create/update actions: cron or once.",
        },
        "cron_expression": {
            "type": "string",
            "description": "Cron expression required for cron schedule jobs.",
        },
        "run_at": {
            "type": "string",
            "description": "ISO 8601 run timestamp required for once schedule jobs.",
        },
        "timezone": {
            "type": "string",
            "description": "Optional IANA timezone name. Defaults to system timezone.",
        },
        "session_id": {
            "type": "string",
            "description": "Optional existing chat session id for triggered runs.",
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
    )


def _handle_cron_tool(
    cron_service: CronService,
    _context: ToolContext,
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
            return _handle_create(cron_service, arguments)
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


def _handle_create(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    agent_id = _required_non_empty_string(arguments.get("agent_id"), field_name="agent_id")
    prompt = _required_non_empty_string(arguments.get("prompt"), field_name="prompt")
    schedule_type = _required_enum(
        arguments.get("schedule_type"),
        field_name="schedule_type",
        allowed=CRON_SCHEDULE_TYPES,
    )

    cron_expression = _optional_string(
        arguments.get("cron_expression"), field_name="cron_expression"
    )
    run_at = _optional_string(arguments.get("run_at"), field_name="run_at")
    timezone = _optional_string(arguments.get("timezone"), field_name="timezone")
    session_id = _optional_string(arguments.get("session_id"), field_name="session_id")

    if schedule_type == "cron":
        if cron_expression is None:
            raise ValueError("cron_expression is required when schedule_type is 'cron'")
        cron_expression = _validated_cron_expression(cron_expression)
        run_at = None
    else:
        if run_at is None:
            raise ValueError("run_at is required when schedule_type is 'once'")
        cron_expression = None

    job = cron_service.create_job(
        agent_id=agent_id,
        prompt=prompt,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        run_at=run_at,
        timezone=timezone,
        session_id=session_id,
    )
    return tool_success({"job": _job_payload(job)})


def _handle_list(cron_service: CronService) -> JsonObject:
    jobs = [_job_payload(job) for job in cron_service.list_jobs()]
    return tool_success({"jobs": jobs})


def _handle_update(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = _required_non_empty_string(arguments.get("id"), field_name="id")
    updates: dict[str, str | None] = {}

    if "agent_id" in arguments:
        updates["agent_id"] = _required_non_empty_string(
            arguments.get("agent_id"),
            field_name="agent_id",
        )
    if "prompt" in arguments:
        updates["prompt"] = _required_non_empty_string(arguments.get("prompt"), field_name="prompt")
    if "schedule_type" in arguments:
        updates["schedule_type"] = _required_enum(
            arguments.get("schedule_type"),
            field_name="schedule_type",
            allowed=CRON_SCHEDULE_TYPES,
        )
    if "cron_expression" in arguments:
        updates["cron_expression"] = _validated_cron_expression(
            _required_non_empty_string(
                arguments.get("cron_expression"), field_name="cron_expression"
            )
        )
    if "run_at" in arguments:
        updates["run_at"] = _required_non_empty_string(arguments.get("run_at"), field_name="run_at")
    if "timezone" in arguments:
        updates["timezone"] = _optional_string(arguments.get("timezone"), field_name="timezone")
    if "session_id" in arguments:
        updates["session_id"] = _optional_string(
            arguments.get("session_id"), field_name="session_id"
        )
    if "status" in arguments:
        updates["status"] = _required_enum(
            arguments.get("status"),
            field_name="status",
            allowed=CRON_STATUSES,
        )

    job = cron_service.update_job(job_id, **updates)
    return tool_success({"job": _job_payload(job)})


def _handle_delete(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = _required_non_empty_string(arguments.get("id"), field_name="id")
    cron_service.delete_job(job_id)
    return tool_success({"id": job_id, "deleted": True})


def _handle_enable(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = _required_non_empty_string(arguments.get("id"), field_name="id")
    job = cron_service.enable_job(job_id)
    return tool_success({"job": _job_payload(job)})


def _handle_disable(cron_service: CronService, arguments: JsonObject) -> JsonObject:
    job_id = _required_non_empty_string(arguments.get("id"), field_name="id")
    job = cron_service.disable_job(job_id)
    return tool_success({"job": _job_payload(job)})


def _job_payload(job: CronJob) -> JsonObject:
    payload = dict(job.to_dict())
    payload["next_fire_at"] = _next_fire_at(job)
    return payload


def _next_fire_at(job: CronJob) -> str | None:
    if job.schedule_type != "cron" or job.status != "active" or job.cron_expression is None:
        return None

    try:
        timezone = _resolve_timezone(job.timezone)
        now_local = datetime.now(timezone)
        next_fire_local = croniter(job.cron_expression, now_local).get_next(datetime)
        if next_fire_local.tzinfo is None:
            next_fire_local = next_fire_local.replace(tzinfo=timezone)
        return next_fire_local.astimezone(UTC).isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        _LOGGER.warning("Unable to compute next_fire_at for cron job id=%s", job.id)
        return None


def _resolve_timezone(timezone_name: str | None) -> tzinfo:
    if timezone_name:
        return ZoneInfo(timezone_name)

    local_timezone = datetime.now().astimezone().tzinfo
    if local_timezone is not None:
        return local_timezone
    return UTC


def _validated_cron_expression(expression: str) -> str:
    normalized = expression.strip()
    if not croniter.is_valid(normalized):
        raise ValueError("cron_expression is invalid")
    return normalized


def _required_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    return normalized or None


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
