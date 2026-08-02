"""Automation RPC handlers."""

from __future__ import annotations

from typing import Any

from core.projects import format_agent_address
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
    _required_string,
)

JsonObject = dict[str, Any]
CRON_SCHEDULE_TYPES = frozenset(("cron", "interval", "once"))
CRON_JOB_STATUSES = frozenset(("active", "paused"))
BOOTSTRAP_MODES = frozenset(("once", "always"))


async def _cron_create(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "agent_id",
        "name",
        "prompt",
        "schedule_type",
        "cron_expression",
        "interval_seconds",
        "run_at",
        "repeat",
        "session_id",
    }
    _reject_unsupported(params, supported_fields, "cron.create")

    # The target rides the outside ``agent@projekt`` address form, parsed once at
    # the edge into a bare ``agent_id`` plus the optional ``project_id`` stored on
    # the job — never an ``@`` string kept in ``agent_id``.
    agent_id, project_id = _required_agent_address(params, "agent_id")
    name = _optional_string(params, "name")
    prompt = _required_string(params, "prompt")
    schedule_type = _required_string(params, "schedule_type")
    if schedule_type not in CRON_SCHEDULE_TYPES:
        options = ", ".join(sorted(CRON_SCHEDULE_TYPES))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.schedule_type must be one of: {options}",
        )

    cron_expression = _optional_string(params, "cron_expression")
    interval_seconds = _optional_positive_integer(params, "interval_seconds")
    run_at = _optional_string(params, "run_at")
    repeat = _optional_positive_integer(params, "repeat")
    session_id = _optional_string(params, "session_id")

    if schedule_type == "cron":
        if cron_expression is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.cron_expression is required when params.schedule_type is 'cron'",
            )
        run_at = None
        interval_seconds = None
    elif schedule_type == "interval":
        if interval_seconds is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.interval_seconds is required when params.schedule_type is 'interval'",
            )
        cron_expression = None
        run_at = None
    else:
        if run_at is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.run_at is required when params.schedule_type is 'once'",
            )
        if "repeat" in params and repeat is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.repeat cannot be null when params.schedule_type is 'once'",
            )
        cron_expression = None
        interval_seconds = None

    try:
        async with _agent_reference_lock(state):
            job = state.runtime.cron_service.create_job(
                agent_id=agent_id,
                name=name,
                prompt=prompt,
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                interval_seconds=interval_seconds,
                run_at=run_at,
                remaining_runs=repeat,
                session_id=session_id,
                project_id=project_id,
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _cron_job_response(state.runtime.cron_service, job)


def _cron_list(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "cron.list does not accept params")

    try:
        jobs = state.runtime.cron_service.list_jobs()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    cron_service = state.runtime.cron_service
    return {
        "jobs": [_cron_job_response(cron_service, job) for job in jobs],
        "system_timezone": cron_service.system_timezone_name(),
    }


async def _cron_update(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "id",
        "agent_id",
        "name",
        "prompt",
        "schedule_type",
        "cron_expression",
        "interval_seconds",
        "run_at",
        "repeat",
        "session_id",
        "status",
    }
    _reject_unsupported(params, supported_fields, "cron.update")

    job_id = _required_string(params, "id")
    updates: JsonObject = {}

    if "agent_id" in params:
        # Re-targeting also re-parses the address form, so changing the project of
        # a cron target is a single ``agent@projekt`` update, not two fields.
        target_agent_id, target_project_id = _required_agent_address(params, "agent_id")
        updates["agent_id"] = target_agent_id
        updates["project_id"] = target_project_id
    if "name" in params:
        updates["name"] = _required_string(params, "name")
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
    if "interval_seconds" in params:
        interval_seconds = _optional_positive_integer(params, "interval_seconds")
        if interval_seconds is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.interval_seconds must be a positive integer",
            )
        updates["interval_seconds"] = interval_seconds
    if "run_at" in params:
        updates["run_at"] = _required_string(params, "run_at")
    if "repeat" in params:
        repeat = _optional_positive_integer(params, "repeat")
        if updates.get("schedule_type") == "once" and repeat is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.repeat cannot be null when params.schedule_type is 'once'",
            )
        updates["remaining_runs"] = repeat
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
                job = state.runtime.cron_service.update_job(job_id, **updates)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _cron_job_response(state.runtime.cron_service, job)

    try:
        job = state.runtime.cron_service.update_job(job_id, **updates)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _cron_job_response(state.runtime.cron_service, job)


def _cron_delete(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.delete")

    job_id = _required_string(params, "id")
    try:
        state.runtime.cron_service.delete_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_enable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.enable")

    job_id = _required_string(params, "id")
    try:
        job = state.runtime.cron_service.enable_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _cron_job_response(state.runtime.cron_service, job)


def _cron_disable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.disable")

    job_id = _required_string(params, "id")
    try:
        job = state.runtime.cron_service.disable_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _cron_job_response(state.runtime.cron_service, job)


def _cron_job_response(cron_service: Any, job: Any) -> JsonObject:
    return {
        "id": job.id,
        "agent_id": job.agent_id,
        "project_id": job.project_id,
        # The address form keeps "builder" unambiguous across projects in
        # listings: a bare target shows ``builder``, a project target ``builder@projekt``.
        "target": format_agent_address(job.agent_id, job.project_id),
        "name": job.name,
        "prompt": job.prompt,
        "schedule_type": job.schedule_type,
        "schedule": cron_service.format_schedule(job),
        "cron_expression": job.cron_expression,
        "interval_seconds": job.interval_seconds,
        "interval_anchor_at": job.interval_anchor_at,
        "run_at": job.run_at,
        "remaining_runs": job.remaining_runs,
        "session_id": job.session_id,
        "status": job.status,
        "last_fired_at": job.last_fired_at,
        "last_attempt_at": job.last_attempt_at,
        "last_completed_at": job.last_completed_at,
        "last_run_id": job.last_run_id,
        "last_outcome": job.last_outcome,
        "last_error": job.last_error,
        "consecutive_failures": job.consecutive_failures,
        "next_fire_at": cron_service.next_fire_at(job),
        "created_at": job.created_at,
    }


async def _bootstrap_create(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"agent_id", "name", "prompt", "mode", "session_id"},
        "bootstrap.create",
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    name = _optional_string(params, "name")
    prompt = _required_string(params, "prompt")
    mode = _bootstrap_mode(params)
    session_id = _optional_string(params, "session_id")
    try:
        async with _agent_reference_lock(state):
            job = state.runtime.bootstrap_service.create_job(
                agent_id=agent_id,
                project_id=project_id,
                name=name,
                prompt=prompt,
                mode=mode,
                session_id=session_id,
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _bootstrap_job_response(job)


def _bootstrap_list(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "bootstrap.list does not accept params")
    try:
        jobs = state.runtime.bootstrap_service.list_jobs()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"jobs": [_bootstrap_job_response(job) for job in jobs]}


async def _bootstrap_update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"id", "agent_id", "name", "prompt", "mode", "session_id"},
        "bootstrap.update",
    )
    job_id = _required_string(params, "id")
    updates: JsonObject = {}
    if "agent_id" in params:
        agent_id, project_id = _required_agent_address(params, "agent_id")
        updates["agent_id"] = agent_id
        updates["project_id"] = project_id
    if "name" in params:
        updates["name"] = _required_string(params, "name")
    if "prompt" in params:
        updates["prompt"] = _required_string(params, "prompt")
    if "mode" in params:
        updates["mode"] = _bootstrap_mode(params)
    if "session_id" in params:
        updates["session_id"] = _optional_string(params, "session_id")
    try:
        if "agent_id" in updates or "session_id" in updates:
            async with _agent_reference_lock(state):
                job = state.runtime.bootstrap_service.update_job(job_id, **updates)
        else:
            job = state.runtime.bootstrap_service.update_job(job_id, **updates)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _bootstrap_job_response(job)


def _bootstrap_delete(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "bootstrap.delete")
    job_id = _required_string(params, "id")
    try:
        state.runtime.bootstrap_service.delete_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True, "id": job_id}


def _bootstrap_enable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "bootstrap.enable")
    try:
        return _bootstrap_job_response(
            state.runtime.bootstrap_service.enable_job(_required_string(params, "id"))
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _bootstrap_disable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "bootstrap.disable")
    try:
        return _bootstrap_job_response(
            state.runtime.bootstrap_service.disable_job(_required_string(params, "id"))
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _bootstrap_mode(params: JsonObject) -> str:
    mode = _required_string(params, "mode")
    if mode not in BOOTSTRAP_MODES:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.mode must be one of: {', '.join(sorted(BOOTSTRAP_MODES))}",
        )
    return mode


def _bootstrap_job_response(job: Any) -> JsonObject:
    return {
        "id": job.id,
        "agent_id": job.agent_id,
        "project_id": job.project_id,
        "target": format_agent_address(job.agent_id, job.project_id),
        "name": job.name,
        "prompt": job.prompt,
        "mode": job.mode,
        "session_id": job.session_id,
        "status": job.status,
        "created_at": job.created_at,
        "armed_after_startup_id": job.armed_after_startup_id,
        "last_started_startup_id": job.last_started_startup_id,
        "last_started_at": job.last_started_at,
        "last_completed_at": job.last_completed_at,
        "last_run_id": job.last_run_id,
        "last_session_id": job.last_session_id,
        "last_outcome": job.last_outcome,
        "last_error": job.last_error,
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return automation RPC handlers."""

    return {
        "bootstrap.create": _bootstrap_create,
        "bootstrap.list": _bootstrap_list,
        "bootstrap.update": _bootstrap_update,
        "bootstrap.delete": _bootstrap_delete,
        "bootstrap.enable": _bootstrap_enable,
        "bootstrap.disable": _bootstrap_disable,
        "cron.create": _cron_create,
        "cron.list": _cron_list,
        "cron.update": _cron_update,
        "cron.delete": _cron_delete,
        "cron.enable": _cron_enable,
        "cron.disable": _cron_disable,
    }
