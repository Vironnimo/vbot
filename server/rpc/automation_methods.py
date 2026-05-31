"""Automation RPC handlers."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter  # type: ignore[import-untyped]

from core.automation.cron import CronJobValidationError
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _optional_string, _required_string

JsonObject = dict[str, Any]
CRON_SCHEDULE_TYPES = frozenset(("cron", "once"))
CRON_JOB_STATUSES = frozenset(("active", "paused", "completed"))


def _validate_cron_agent_exists(state: Any, agent_id: str) -> None:
    agents = getattr(state.runtime, "agents", None)
    if agents is None:
        return
    try:
        agents.get(agent_id)
    except Exception as error:
        raise CronJobValidationError(f"Unknown agent_id: {agent_id}") from error


async def _cron_create(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "agent_id",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
        "timezone",
        "session_id",
    }
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.create fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    prompt = _required_string(params, "prompt")
    schedule_type = _required_string(params, "schedule_type")
    if schedule_type not in CRON_SCHEDULE_TYPES:
        options = ", ".join(sorted(CRON_SCHEDULE_TYPES))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.schedule_type must be one of: {options}",
        )

    cron_expression = _optional_string(params, "cron_expression")
    run_at = _optional_string(params, "run_at")
    timezone = _optional_string(params, "timezone")
    session_id = _optional_string(params, "session_id")

    if schedule_type == "cron":
        if cron_expression is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.cron_expression is required when params.schedule_type is 'cron'",
            )
        run_at = None
    else:
        if run_at is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.run_at is required when params.schedule_type is 'once'",
            )
        cron_expression = None

    try:
        async with _agent_reference_lock(state):
            _validate_cron_agent_exists(state, agent_id)
            job = state.runtime.cron_service.create_job(
                agent_id=agent_id,
                prompt=prompt,
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                run_at=run_at,
                timezone=timezone,
                session_id=session_id,
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"id": job.id}


def _cron_list(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "cron.list does not accept params")

    try:
        jobs = state.runtime.cron_service.list_jobs()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"jobs": [_cron_job_response(job) for job in jobs]}


async def _cron_update(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
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
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.update fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    updates: JsonObject = {}

    if "agent_id" in params:
        updates["agent_id"] = _required_string(params, "agent_id")
    if "prompt" in params:
        updates["prompt"] = _required_string(params, "prompt")
    if "schedule_type" in params:
        schedule_type = _required_string(params, "schedule_type")
        if schedule_type not in CRON_SCHEDULE_TYPES:
            options = ", ".join(sorted(CRON_SCHEDULE_TYPES))
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.schedule_type must be one of: {options}",
            )
        updates["schedule_type"] = schedule_type
    if "cron_expression" in params:
        updates["cron_expression"] = _required_string(params, "cron_expression")
    if "run_at" in params:
        updates["run_at"] = _required_string(params, "run_at")
    if "timezone" in params:
        updates["timezone"] = _optional_string(params, "timezone")
    if "session_id" in params:
        updates["session_id"] = _optional_string(params, "session_id")
    if "status" in params:
        status = _required_string(params, "status")
        if status not in CRON_JOB_STATUSES:
            options = ", ".join(sorted(CRON_JOB_STATUSES))
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.status must be one of: {options}",
            )
        updates["status"] = status

    if "agent_id" in updates:
        try:
            async with _agent_reference_lock(state):
                _validate_cron_agent_exists(state, updates["agent_id"])
                state.runtime.cron_service.update_job(job_id, **updates)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return {"ok": True}

    try:
        state.runtime.cron_service.update_job(job_id, **updates)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_delete(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.delete fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    try:
        state.runtime.cron_service.delete_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_enable(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.enable fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    try:
        state.runtime.cron_service.enable_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_disable(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.disable fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    try:
        state.runtime.cron_service.disable_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_job_response(job: Any) -> JsonObject:
    return {
        "id": job.id,
        "agent_id": job.agent_id,
        "prompt": job.prompt,
        "schedule_type": job.schedule_type,
        "cron_expression": job.cron_expression,
        "run_at": job.run_at,
        "timezone": job.timezone,
        "session_id": job.session_id,
        "status": job.status,
        "last_fired_at": job.last_fired_at,
        "next_fire_at": _cron_next_fire_at(job),
        "created_at": job.created_at,
    }


def _cron_next_fire_at(job: Any) -> str | None:
    if job.schedule_type != "cron" or job.status != "active" or job.cron_expression is None:
        return None

    try:
        timezone = _resolve_cron_timezone(job.timezone)
        now_local = datetime.now(timezone)
        next_fire_local = cast(
            datetime,
            croniter(job.cron_expression, now_local).get_next(datetime),
        )
        if next_fire_local.tzinfo is None:
            next_fire_local = next_fire_local.replace(tzinfo=timezone)
        return next_fire_local.astimezone(UTC).isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _resolve_cron_timezone(timezone_name: str | None) -> tzinfo:
    if timezone_name:
        normalized_timezone = timezone_name.strip()
        if normalized_timezone.upper() == "UTC":
            return UTC
        return ZoneInfo(normalized_timezone)

    local_timezone = datetime.now().astimezone().tzinfo
    if local_timezone is not None:
        return local_timezone
    return UTC


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return automation RPC handlers."""

    return {
        "cron.create": _cron_create,
        "cron.list": _cron_list,
        "cron.update": _cron_update,
        "cron.delete": _cron_delete,
        "cron.enable": _cron_enable,
        "cron.disable": _cron_disable,
    }
