"""Automation RPC handlers."""

from __future__ import annotations

from typing import Any

from core.automation.cron import (
    CronDelivery,
    CronEmailTrigger,
    CronJobValidationError,
    CronService,
    CronWatch,
    cron_run_overrides,
)
from core.projects import format_agent_address
from core.sessions import AGENT_DEFAULT_PROJECT
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_positive_integer,
    _optional_string,
    _optional_working_project,
    _reject_unsupported,
    _required_agent_address,
    _required_string,
)

JsonObject = dict[str, Any]
CRON_SCHEDULE_TYPES = frozenset(("cron", "interval", "once", "event", "webhook", "email"))
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
        "event_id",
        "event_time",
        "repeat",
        "session_id",
        "working_project_id",
        "run_overrides",
        "delivery",
        "notification_policy",
        "watch",
        "email",
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
    # Omitted, a job delivers nowhere; the WebUI form chooses app for new jobs.
    delivery = _optional_delivery(params)
    watch = _optional_watch(params)
    email = _optional_email(params)
    event_fields: JsonObject = {}

    if schedule_type == "event":
        event_id = _optional_string(params, "event_id")
        if event_id is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.event_id is required when params.schedule_type is 'event'",
            )
        event_fields = {
            "event_id": event_id,
            **_event_time_fields(_optional_string(params, "event_time") or "start"),
        }
        cron_expression = None
        interval_seconds = None
        run_at = None
    elif schedule_type == "cron":
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
    elif schedule_type in {"webhook", "email"}:
        # Requests to the job's URL, or mails to its email Channel, start its Runs;
        # it keeps no time.
        if schedule_type == "email" and email is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.email is required when params.schedule_type is 'email'",
            )
        cron_expression = None
        interval_seconds = None
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
            job = await state.runtime.cron_service.create_job(
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
                delivery=delivery,
                notification_policy=params.get("notification_policy", "all"),
                watch=watch,
                email=email,
                actor="rpc",
                **_execution_fields(params, creating=True),
                **event_fields,
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


async def _cron_activity(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "cron.activity")
    try:
        sessions = state.runtime.chat_sessions
        from server.rpc.event_bridge import session_activity_service

        work = session_activity_service(state)
        busy = [address for address, _ in work.snapshot()] if work is not None else []
        activity = await sessions.run_async(sessions.notifications.schedule_activity, busy)
        jobs = {
            job.id: job.notification_activity(activity.get(job.id, {}))
            for job in state.runtime.cron_service.list_jobs()
        }
        return {
            "jobs": jobs,
            "notification_count": sum(job["notification_count"] for job in jobs.values()),
        }
    except Exception as error:
        raise _map_expected_error(error) from error


async def _cron_runs(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "before", "limit"}, "cron.runs")
    job_id = _required_string(params, "id")
    before = _optional_positive_integer(params, "before")
    limit = _optional_positive_integer(params, "limit") or 5
    if limit > 50:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "limit must be at most 50")
    try:
        state.runtime.cron_service.get_job(job_id)
        sessions = state.runtime.chat_sessions
        result: JsonObject = await sessions.run_async(
            sessions.notifications.schedule_runs, job_id, before=before, limit=limit
        )
        from core.sessions import SessionAddress
        from server.rpc.event_bridge import session_activity_service

        work = session_activity_service(state)
        if work is not None:
            for row in result["runs"]:
                address = SessionAddress(
                    row["project_id"] or None, row["agent_id"], row["session_id"]
                )
                row["unread"] = row["unread"] and work.state(address).state == "idle"
        return result
    except Exception as error:
        raise _map_expected_error(error) from error


async def _cron_mark_read(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "through", "alert_at"}, "cron.mark_read")
    job_id = _required_string(params, "id")
    through = _optional_positive_integer(params, "through")
    alert_at = _optional_string(params, "alert_at")
    if through is None and alert_at is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "through or alert_at is required")
    try:
        state.runtime.cron_service.get_job(job_id)
        sessions = state.runtime.chat_sessions
        count = (
            await sessions.run_async(sessions.notifications.mark_schedule_read, job_id, through)
            if through is not None
            else 0
        )
        if alert_at is not None:
            await state.runtime.cron_service.acknowledge_alert(
                job_id, actor="rpc", expected_at=alert_at
            )
        return {"marked_read": count}
    except Exception as error:
        raise _map_expected_error(error) from error


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
        "event_id",
        "event_time",
        "repeat",
        "session_id",
        "working_project_id",
        "run_overrides",
        "delivery",
        "notification_policy",
        "watch",
        "email",
        "status",
    }
    _reject_unsupported(params, supported_fields, "cron.update")

    job_id = _required_string(params, "id")
    updates: JsonObject = {}
    updates.update(_execution_fields(params, creating=False))

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
    if "event_id" in params:
        updates["event_id"] = _required_string(params, "event_id")
    if "event_time" in params:
        updates.update(_event_time_fields(_required_string(params, "event_time")))
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
    if "notification_policy" in params:
        updates["notification_policy"] = _required_string(params, "notification_policy")
    if "delivery" in params:
        updates["delivery"] = _optional_delivery(params)
    if "watch" in params:
        updates["watch"] = _optional_watch(params)
    if "email" in params:
        updates["email"] = _optional_email(params)
    if "status" in params:
        status = _required_string(params, "status")
        if status not in CRON_JOB_STATUSES:
            options = ", ".join(sorted(CRON_JOB_STATUSES))
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.status must be one of: {options}",
            )
        updates["status"] = status
    try:
        # Any update may select another Agent, Project or Session.
        async with _agent_reference_lock(state):
            job = await state.runtime.cron_service.update_job(job_id, actor="rpc", **updates)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _cron_job_response(state.runtime.cron_service, job)


async def _cron_webhook_secret(state: Any, params: JsonObject) -> JsonObject:
    """The secret a webhook job's requests prove, with the job's ``webhook``."""
    _reject_unsupported(params, {"id"}, "cron.webhook_secret")

    job_id = _required_string(params, "id")
    cron_service = state.runtime.cron_service
    try:
        secret = await cron_service.webhook_secret(job_id)
        job = cron_service.get_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"secret": secret, "webhook": _webhook_response(cron_service, job)}


async def _cron_rotate_webhook_secret(state: Any, params: JsonObject) -> JsonObject:
    """Replace a webhook job's secret; the old one stops working at once."""
    _reject_unsupported(params, {"id"}, "cron.rotate_webhook_secret")

    job_id = _required_string(params, "id")
    cron_service = state.runtime.cron_service
    try:
        secret = await cron_service.rotate_webhook_secret(job_id, actor="rpc")
        job = cron_service.get_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"secret": secret, "webhook": _webhook_response(cron_service, job)}


async def _cron_delete(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.delete")

    job_id = _required_string(params, "id")
    try:
        await state.runtime.cron_service.delete_job(job_id, actor="rpc")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


async def _cron_enable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.enable")

    job_id = _required_string(params, "id")
    try:
        job = await state.runtime.cron_service.enable_job(job_id, actor="rpc")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _cron_job_response(state.runtime.cron_service, job)


async def _cron_retry_delivery(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.retry_delivery")
    job_id = _required_string(params, "id")
    try:
        service = state.runtime.cron_service
        await service.retry_delivery(job_id)
        return _cron_job_response(service, service.get_job(job_id))
    except Exception as error:
        raise _map_expected_error(error) from error


async def _cron_acknowledge_alert(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.acknowledge_alert")
    job_id = _required_string(params, "id")
    try:
        job = await state.runtime.cron_service.acknowledge_alert(job_id, actor="rpc")
    except Exception as error:
        raise _map_expected_error(error) from error
    return _cron_job_response(state.runtime.cron_service, job)


async def _cron_disable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "cron.disable")

    job_id = _required_string(params, "id")
    try:
        job = await state.runtime.cron_service.disable_job(job_id, actor="rpc")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _cron_job_response(state.runtime.cron_service, job)


_DELIVERY_FIELDS = frozenset(("kind", "channel_id", "platform_target", "thread_id"))


def _optional_delivery(params: JsonObject) -> CronDelivery | None:
    """``params.delivery`` as a job keeps it: null and kind none deliver nowhere."""
    value = params.get("delivery")
    if value is None:
        return None
    if isinstance(value, dict):
        _reject_unsupported(value, set(_DELIVERY_FIELDS), "params.delivery")
    try:
        return CronDelivery.from_dict(value).stored()
    except CronJobValidationError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{error}") from error


def _optional_watch(params: JsonObject) -> CronWatch | None:
    """``params.watch`` as a job keeps it: ``{"command": ...}`` or ``{"url": ...}``; null: none.

    ``"always": true`` starts a Run at every due time instead of only on a change.
    """
    value = params.get("watch")
    if value is None:
        return None
    if isinstance(value, dict):
        _reject_unsupported(value, {"command", "url", "always"}, "params.watch")
    try:
        return CronWatch.from_dict(value)
    except CronJobValidationError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{error}") from error


def _optional_email(params: JsonObject) -> CronEmailTrigger | None:
    """``params.email`` as a job keeps it: ``{"channel_id", "senders"?, "subject"?}``; null: none.

    ``senders`` lists addresses or ``@domain`` suffixes; ``subject`` is text the
    subject contains. Omitted filters match every mail.
    """
    value = params.get("email")
    if value is None:
        return None
    if isinstance(value, dict):
        _reject_unsupported(value, {"channel_id", "senders", "subject"}, "params.email")
    try:
        return CronEmailTrigger.from_dict(value)
    except CronJobValidationError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{error}") from error


def _event_time_fields(event_time: str) -> JsonObject:
    """The job fields of an event time such as ``start - 30m``."""
    try:
        edge, offset = CronService.parse_event_time(event_time)
    except CronJobValidationError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.event_time: {error}") from error
    return {"event_edge": edge, "event_offset_minutes": offset}


def _cron_job_response(cron_service: Any, job: Any) -> JsonObject:
    event = cron_service.bound_event(job) if job.schedule_type == "event" else None
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
        "event_id": job.event_id,
        "event_edge": job.event_edge,
        "event_offset_minutes": job.event_offset_minutes,
        # None for another schedule, or while the event is gone or unreadable.
        "event_title": None if event is None else event.title,
        "remaining_runs": job.remaining_runs,
        "session_id": job.session_id,
        **(
            {"working_project_id": job.working_project_id}
            if job.working_project_id is not AGENT_DEFAULT_PROJECT
            else {}
        ),
        "run_overrides": None if job.run_overrides is None else job.run_overrides.as_dict(),
        "notification_policy": job.notification_policy,
        "delivery": (job.delivery or CronDelivery(kind="none")).to_dict(),
        "status": job.status,
        "last_fired_at": job.last_fired_at,
        "last_attempt_at": job.last_attempt_at,
        "last_completed_at": job.last_completed_at,
        "last_run_id": job.last_run_id,
        "last_outcome": job.last_outcome,
        # Whether the latest Run succeeded with a [SILENT] answer and delivered nothing.
        "last_run_silent": job.last_run_silent,
        # The latest answer that was not [SILENT]: text (at most 2,000 characters), at, run_id.
        "last_result": None if job.last_result is None else job.last_result.to_dict(),
        # Waiting and uncertain chat messages; ``dropped`` counts older ones dropped unsent,
        # which the next sent message names.
        "delivery_progress": {**job.delivery_progress(), "dropped": job.dropped_notifications},
        # The latest alert no Run reports, {message, at, kind} until acknowledged; null for none.
        "unread_alert": None if job.unread_alert is None else job.unread_alert.to_dict(),
        "last_error": job.last_error,
        # What the job checks before each run, {"command": ...} or {"url": ...}; null for none.
        "watch": None if job.watch is None else job.watch.to_dict(),
        # The latest check, the latest change whose Run succeeded, and why the latest check failed.
        "last_checked_at": job.last_checked_at,
        "last_changed_at": job.last_changed_at,
        "last_check_error": job.last_check_error,
        "consecutive_failures": job.consecutive_failures,
        "next_fire_at": cron_service.next_fire_at(job),
        "created_at": job.created_at,
        # A webhook job's URL and requests; null for another schedule.
        "webhook": _webhook_response(cron_service, job),
        # An email job's Channel, filters and mails; null for another schedule.
        "email": _email_response(cron_service, job),
    }


def _execution_fields(params: JsonObject, *, creating: bool) -> JsonObject:
    """Parse execution choices; updates retain a leaf patch for owner-side merging."""
    fields: JsonObject = {}
    if "working_project_id" in params:
        fields["working_project_id"] = _optional_working_project(params, None)
    if "run_overrides" in params:
        raw = params["run_overrides"]
        try:
            parsed = cron_run_overrides(raw, strict=True)
        except CronJobValidationError as error:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
        fields["run_overrides"] = parsed if creating else raw
    return fields


def _webhook_response(cron_service: Any, job: Any) -> JsonObject | None:
    """``url`` is null while the server address is unknown; ``reach`` names who can call it
    (internet, network or computer); ``waiting`` requests wait for the next Run and
    ``dropped`` ones were dropped unseen since the last Run started."""
    if job.schedule_type != "webhook":
        return None
    status = cron_service.webhook_status(job)
    if status is None:
        return None
    return {
        "url": status.url,
        "path": status.path,
        "reach": status.reach,
        "last_received_at": status.last_received_at,
        **({"error": status.error} if status.error else {}),
        "waiting": status.waiting,
        "dropped": status.dropped,
    }


def _email_response(cron_service: Any, job: Any) -> JsonObject | None:
    """The job's ``email`` trigger with its mails: ``waiting`` ones wait for the next Run and
    ``dropped`` ones were dropped unseen since the last Run started; ``error`` says why
    waiting mails cannot be read."""
    if job.email is None:
        return None
    status = cron_service.email_status(job)
    if status is None:
        return None
    return {
        **job.email.to_dict(),
        "last_received_at": status.last_received_at,
        **({"error": status.error} if status.error else {}),
        "waiting": status.waiting,
        "dropped": status.dropped,
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
                actor="rpc",
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
        # Any update may select another Agent, Project or Session.
        async with _agent_reference_lock(state):
            job = state.runtime.bootstrap_service.update_job(job_id, actor="rpc", **updates)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _bootstrap_job_response(job)


def _bootstrap_delete(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "bootstrap.delete")
    job_id = _required_string(params, "id")
    try:
        state.runtime.bootstrap_service.delete_job(job_id, actor="rpc")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True, "id": job_id}


def _bootstrap_enable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "bootstrap.enable")
    try:
        return _bootstrap_job_response(
            state.runtime.bootstrap_service.enable_job(_required_string(params, "id"), actor="rpc")
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _bootstrap_disable(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "bootstrap.disable")
    try:
        return _bootstrap_job_response(
            state.runtime.bootstrap_service.disable_job(_required_string(params, "id"), actor="rpc")
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
        "cron.activity": _cron_activity,
        "cron.runs": _cron_runs,
        "cron.mark_read": _cron_mark_read,
        "cron.update": _cron_update,
        "cron.delete": _cron_delete,
        "cron.webhook_secret": _cron_webhook_secret,
        "cron.rotate_webhook_secret": _cron_rotate_webhook_secret,
        "cron.enable": _cron_enable,
        "cron.disable": _cron_disable,
        "cron.retry_delivery": _cron_retry_delivery,
        "cron.acknowledge_alert": _cron_acknowledge_alert,
    }
