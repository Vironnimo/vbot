"""Tests for cron (automation) RPC handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.automation.cron import CronJobValidationError, CronServiceError
from server.rpc.methods import dispatch_rpc


def _state_with_cron_service(
    cron_service: Any,
    *,
    resolver: object | None = None,
) -> SimpleNamespace:
    # The cron RPC validates the target through the agent resolver (the same seam
    # every run path uses), so the stub state carries one. A bare resolver Mock
    # resolves any target; tests that exercise the rejection path inject a side
    # effect.
    agent_resolver = resolver if resolver is not None else Mock()
    if isinstance(agent_resolver, Mock):
        agent_resolver.resolve_agent.return_value = SimpleNamespace(id="main")
    if isinstance(cron_service, Mock):
        cron_service.format_schedule.side_effect = lambda job: (
            job.cron_expression
            if job.schedule_type == "cron"
            else f"every {job.interval_seconds // 3600}h"
            if job.schedule_type == "interval"
            else job.run_at
        )
    return SimpleNamespace(
        runtime=SimpleNamespace(cron_service=cron_service, agent_resolver=agent_resolver)
    )


def _cron_job(**changes: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": "job-123",
        "agent_id": "main",
        "project_id": None,
        "name": "Status check",
        "prompt": "Run status check",
        "schedule_type": "cron",
        "cron_expression": "*/5 * * * *",
        "interval_seconds": None,
        "interval_anchor_at": None,
        "run_at": None,
        "remaining_runs": None,
        "session_id": "session-1",
        "status": "active",
        "last_fired_at": None,
        "last_attempt_at": None,
        "last_completed_at": None,
        "last_run_id": None,
        "last_outcome": None,
        "last_error": None,
        "consecutive_failures": 0,
        "created_at": "2026-05-14T09:00:00+00:00",
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _bootstrap_job(**changes: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": "bootstrap-123",
        "agent_id": "main",
        "project_id": None,
        "name": "Verify update",
        "prompt": "Check status and logs",
        "mode": "once",
        "session_id": "session-one",
        "status": "active",
        "created_at": "2026-08-02T12:00:00+00:00",
        "armed_after_startup_id": "startup-one",
        "last_started_startup_id": None,
        "last_started_at": None,
        "last_completed_at": None,
        "last_run_id": None,
        "last_session_id": None,
        "last_outcome": None,
        "last_error": None,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _state_with_bootstrap_service(service: Any) -> SimpleNamespace:
    return SimpleNamespace(runtime=SimpleNamespace(bootstrap_service=service))


@pytest.mark.asyncio
async def test_bootstrap_create_parses_project_target_and_session() -> None:
    service = Mock()
    service.create_job.return_value = _bootstrap_job(agent_id="builder", project_id="vbot")
    state = _state_with_bootstrap_service(service)

    response = await dispatch_rpc(
        state,
        {
            "method": "bootstrap.create",
            "params": {
                "agent_id": "builder@vbot",
                "name": "Verify update",
                "prompt": "Check status and logs",
                "mode": "once",
                "session_id": "session-one",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["target"] == "builder@vbot"
    service.create_job.assert_called_once_with(
        agent_id="builder",
        project_id="vbot",
        name="Verify update",
        prompt="Check status and logs",
        mode="once",
        session_id="session-one",
    )


@pytest.mark.asyncio
async def test_bootstrap_update_can_clear_session() -> None:
    service = Mock()
    service.update_job.return_value = _bootstrap_job(session_id=None)
    state = _state_with_bootstrap_service(service)

    response = await dispatch_rpc(
        state,
        {
            "method": "bootstrap.update",
            "params": {"id": "bootstrap-123", "session_id": None},
        },
    )

    assert response["ok"] is True
    service.update_job.assert_called_once_with("bootstrap-123", session_id=None)


@pytest.mark.asyncio
async def test_bootstrap_rejects_unknown_mode() -> None:
    response = await dispatch_rpc(
        _state_with_bootstrap_service(Mock()),
        {
            "method": "bootstrap.create",
            "params": {"agent_id": "main", "prompt": "Check", "mode": "sometimes"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_cron_create_happy_path() -> None:
    cron_service = Mock()
    cron_service.create_job.return_value = _cron_job()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "main",
                "name": "Status check",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
                "session_id": "session-1",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["id"] == "job-123"
    assert response["result"]["name"] == "Status check"
    assert response["result"]["target"] == "main"
    assert response["result"]["status"] == "active"
    cron_service.create_job.assert_called_once_with(
        agent_id="main",
        name="Status check",
        prompt="Run status check",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        interval_seconds=None,
        run_at=None,
        remaining_runs=None,
        session_id="session-1",
        project_id=None,
    )


@pytest.mark.asyncio
async def test_cron_create_parses_project_qualified_target() -> None:
    cron_service = Mock()
    cron_service.create_job.return_value = _cron_job(
        agent_id="builder", project_id="vbot", session_id=None
    )
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "builder@vbot",
                "name": "Status check",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["id"] == "job-123"
    assert response["result"]["target"] == "builder@vbot"
    # The address form is split once at the edge: agent_id + project_id, never an
    # "@" string in agent_id. CronService owns target validation.
    cron_service.create_job.assert_called_once_with(
        agent_id="builder",
        name="Status check",
        prompt="Run status check",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        interval_seconds=None,
        run_at=None,
        remaining_runs=None,
        session_id=None,
        project_id="vbot",
    )


@pytest.mark.asyncio
async def test_cron_create_accepts_interval_repeat_and_omitted_name() -> None:
    cron_service = Mock()
    cron_service.create_job.return_value = _cron_job(
        name="Check status",
        schedule_type="interval",
        cron_expression=None,
        interval_seconds=7200,
        interval_anchor_at="2026-07-28T12:00:00+00:00",
        remaining_runs=3,
    )
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "main",
                "prompt": "Check status",
                "schedule_type": "interval",
                "interval_seconds": 7200,
                "repeat": 3,
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["schedule"] == "every 2h"
    assert response["result"]["remaining_runs"] == 3
    cron_service.create_job.assert_called_once_with(
        agent_id="main",
        name=None,
        prompt="Check status",
        schedule_type="interval",
        cron_expression=None,
        interval_seconds=7200,
        run_at=None,
        remaining_runs=3,
        session_id=None,
        project_id=None,
    )


@pytest.mark.asyncio
async def test_cron_create_rejects_null_repeat_for_once_schedule() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "main",
                "prompt": "Run once",
                "schedule_type": "once",
                "run_at": "2026-08-01T09:00:00+00:00",
                "repeat": None,
            },
        },
    )

    assert response["ok"] is False
    assert "params.repeat cannot be null" in response["error"]["message"]
    cron_service.create_job.assert_not_called()


@pytest.mark.asyncio
async def test_cron_list_happy_path_includes_canonical_service_projection() -> None:
    job = SimpleNamespace(
        id="job-1",
        agent_id="builder",
        project_id="vbot",
        name="Report check",
        prompt="Check reports",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        interval_seconds=None,
        interval_anchor_at=None,
        run_at=None,
        remaining_runs=None,
        session_id="session-1",
        status="active",
        last_fired_at="2026-05-14T09:55:00+00:00",
        last_attempt_at="2026-05-14T09:55:00+00:00",
        last_completed_at="2026-05-14T09:56:00+00:00",
        last_run_id="run-1",
        last_outcome="success",
        last_error=None,
        consecutive_failures=0,
        created_at="2026-05-14T09:00:00+00:00",
    )
    cron_service = Mock()
    cron_service.list_jobs.return_value = [job]
    cron_service.system_timezone_name.return_value = "Europe/Berlin"
    cron_service.next_fire_at.return_value = "2026-05-14T10:05:00+00:00"
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(state, {"method": "cron.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "jobs": [
                {
                    "id": "job-1",
                    "agent_id": "builder",
                    "project_id": "vbot",
                    "target": "builder@vbot",
                    "name": "Report check",
                    "prompt": "Check reports",
                    "schedule_type": "cron",
                    "schedule": "*/5 * * * *",
                    "cron_expression": "*/5 * * * *",
                    "interval_seconds": None,
                    "interval_anchor_at": None,
                    "run_at": None,
                    "remaining_runs": None,
                    "session_id": "session-1",
                    "status": "active",
                    "last_fired_at": "2026-05-14T09:55:00+00:00",
                    "last_attempt_at": "2026-05-14T09:55:00+00:00",
                    "last_completed_at": "2026-05-14T09:56:00+00:00",
                    "last_run_id": "run-1",
                    "last_outcome": "success",
                    "last_error": None,
                    "consecutive_failures": 0,
                    "next_fire_at": "2026-05-14T10:05:00+00:00",
                    "created_at": "2026-05-14T09:00:00+00:00",
                }
            ],
            "system_timezone": "Europe/Berlin",
        },
    }
    cron_service.list_jobs.assert_called_once_with()


@pytest.mark.asyncio
async def test_cron_update_happy_path() -> None:
    cron_service = Mock()
    cron_service.update_job.return_value = _cron_job(
        name="Updated status check",
        prompt="Updated prompt",
        status="paused",
    )
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "name": "Updated status check",
                "prompt": "Updated prompt",
                "status": "paused",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["id"] == "job-123"
    assert response["result"]["name"] == "Updated status check"
    assert response["result"]["status"] == "paused"
    cron_service.update_job.assert_called_once_with(
        "job-1",
        name="Updated status check",
        prompt="Updated prompt",
        status="paused",
    )


@pytest.mark.asyncio
async def test_cron_update_schedule_without_repeat_preserves_current_count() -> None:
    cron_service = Mock()
    cron_service.update_job.return_value = _cron_job(cron_expression="0 10 * * *")
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "schedule_type": "cron",
                "cron_expression": "0 10 * * *",
            },
        },
    )

    assert response["ok"] is True
    cron_service.update_job.assert_called_once_with(
        "job-1",
        schedule_type="cron",
        cron_expression="0 10 * * *",
    )


@pytest.mark.asyncio
async def test_cron_update_accepts_null_repeat_for_recurring_job() -> None:
    cron_service = Mock()
    cron_service.update_job.return_value = _cron_job(remaining_runs=None)
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "repeat": None,
            },
        },
    )

    assert response["ok"] is True
    cron_service.update_job.assert_called_once_with("job-1", remaining_runs=None)


@pytest.mark.asyncio
async def test_cron_update_rejects_null_repeat_with_once_schedule() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "schedule_type": "once",
                "run_at": "2026-08-01T09:00:00+00:00",
                "repeat": None,
            },
        },
    )

    assert response["ok"] is False
    assert "params.repeat cannot be null" in response["error"]["message"]
    cron_service.update_job.assert_not_called()


@pytest.mark.asyncio
async def test_cron_update_parses_agent_address_when_agent_id_is_present() -> None:
    cron_service = Mock()
    cron_service.update_job.return_value = _cron_job()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "agent_id": "main",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["target"] == "main"
    cron_service.update_job.assert_called_once_with("job-1", agent_id="main", project_id=None)


@pytest.mark.asyncio
async def test_cron_delete_happy_path() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.delete",
            "params": {"id": "job-1"},
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    cron_service.delete_job.assert_called_once_with("job-1")


@pytest.mark.asyncio
async def test_cron_enable_happy_path() -> None:
    cron_service = Mock()
    cron_service.enable_job.return_value = _cron_job(status="active")
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.enable",
            "params": {"id": "job-1"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["status"] == "active"
    cron_service.enable_job.assert_called_once_with("job-1")


@pytest.mark.asyncio
async def test_cron_disable_happy_path() -> None:
    cron_service = Mock()
    cron_service.disable_job.return_value = _cron_job(status="paused")
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.disable",
            "params": {"id": "job-1"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["status"] == "paused"
    cron_service.disable_job.assert_called_once_with("job-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "cron.create",
            {
                "name": "Status check",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        ),
        (
            "cron.create",
            {
                "agent_id": "main",
                "name": "Status check",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
                "timezone": "Europe/Berlin",
            },
        ),
        (
            "cron.create",
            {
                "agent_id": "main",
                "name": "Status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        ),
        ("cron.list", {"extra": True}),
        ("cron.update", {"prompt": "missing id"}),
        ("cron.delete", {}),
        ("cron.enable", {}),
        ("cron.disable", {}),
    ],
)
async def test_cron_methods_reject_invalid_params(method: str, params: dict[str, Any]) -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_cron_create_wraps_expected_domain_errors() -> None:
    cron_service = Mock()
    cron_service.create_job.side_effect = CronServiceError("bad schedule")
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "main",
                "name": "Status check",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response == {
        "ok": False,
        "error": {"code": "domain_error", "message": "bad schedule"},
    }


@pytest.mark.asyncio
async def test_cron_create_rejects_unknown_agent() -> None:
    cron_service = Mock()
    cron_service.create_job.side_effect = CronJobValidationError(
        "Cron target does not exist: missing"
    )
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "missing",
                "name": "Status check",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response == {
        "ok": False,
        "error": {"code": "domain_error", "message": "Cron target does not exist: missing"},
    }
    cron_service.create_job.assert_called_once()


@pytest.mark.asyncio
async def test_cron_create_rejects_unknown_project_target() -> None:
    cron_service = Mock()
    cron_service.create_job.side_effect = CronJobValidationError(
        "Cron target does not exist: ghost@vbot"
    )
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "ghost@vbot",
                "name": "Status check",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response == {
        "ok": False,
        "error": {
            "code": "domain_error",
            "message": "Cron target does not exist: ghost@vbot",
        },
    }
    cron_service.create_job.assert_called_once()
