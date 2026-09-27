"""Automation RPCs: cron jobs and bootstrap jobs.

The RPC edge validates params, splits the ``agent@project`` address once and
projects the service's jobs; the services own scheduling and target validation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.automation import CronService
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from tests.core.automation.cron_test_support import make_service
from tests.server.rpc_test_support import rpc_error, rpc_result

JsonObject = dict[str, Any]
_CRON_PARAMS: JsonObject = {
    "agent_id": "main",
    "name": "Status check",
    "prompt": "Run status check",
    "schedule_type": "cron",
    "cron_expression": "*/5 * * * *",
}
_CREATE_DEFAULTS: JsonObject = {
    "name": None,
    "cron_expression": None,
    "interval_seconds": None,
    "run_at": None,
    "remaining_runs": None,
    "session_id": None,
    "project_id": None,
}


def _cron_state(cron_service: Any | None = None, *, resolver: Any | None = None) -> SimpleNamespace:
    # The cron RPC validates the target through the Agent resolver (the seam every
    # run path uses); the default resolver accepts any target.
    if resolver is None:
        resolver = Mock()
        resolver.resolve_agent.return_value = SimpleNamespace(id="main")
    if cron_service is None:
        cron_service = Mock()
        cron_service.format_schedule.side_effect = CronService.format_schedule
        cron_service.next_fire_at.return_value = None
    return SimpleNamespace(
        runtime=SimpleNamespace(cron_service=cron_service, agent_resolver=resolver)
    )


def _cron_job(**changes: Any) -> SimpleNamespace:
    fields: JsonObject = {
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
    fields: JsonObject = {
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


# ---------------------------------------------------------------------------
# bootstrap.*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_create_and_update_pass_the_parsed_target_and_session() -> None:
    service = Mock()
    service.create_job.return_value = _bootstrap_job(agent_id="builder", project_id="vbot")
    service.update_job.return_value = _bootstrap_job(session_id=None)
    state = SimpleNamespace(runtime=SimpleNamespace(bootstrap_service=service))

    created = await rpc_result(
        state,
        "bootstrap.create",
        agent_id="builder@vbot",
        name="Verify update",
        prompt="Check status and logs",
        mode="once",
        session_id="session-one",
    )
    # A null session clears the pinned Session.
    updated = await rpc_result(state, "bootstrap.update", id="bootstrap-123", session_id=None)

    assert created["target"] == "builder@vbot"
    service.create_job.assert_called_once_with(
        agent_id="builder",
        project_id="vbot",
        name="Verify update",
        prompt="Check status and logs",
        mode="once",
        session_id="session-one",
    )
    assert updated["session_id"] is None
    service.update_job.assert_called_once_with("bootstrap-123", session_id=None)


@pytest.mark.asyncio
async def test_bootstrap_list_projects_each_job() -> None:
    service = Mock()
    service.list_jobs.return_value = [_bootstrap_job(agent_id="builder", project_id="vbot")]
    state = SimpleNamespace(runtime=SimpleNamespace(bootstrap_service=service))

    result = await rpc_result(state, "bootstrap.list")

    assert [(job["id"], job["target"], job["mode"]) for job in result["jobs"]] == [
        ("bootstrap-123", "builder@vbot", "once")
    ]


# ---------------------------------------------------------------------------
# cron.*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "job", "service_call", "result"),
    [
        pytest.param(
            {**_CRON_PARAMS, "session_id": "session-1"},
            _cron_job(),
            {
                "agent_id": "main",
                "name": "Status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
                "session_id": "session-1",
            },
            {"id": "job-123", "name": "Status check", "target": "main", "status": "active"},
            id="cron",
        ),
        # The address is split once at the edge into agent_id + project_id, never an
        # "@" string in agent_id.
        pytest.param(
            {**_CRON_PARAMS, "agent_id": "builder@vbot"},
            _cron_job(agent_id="builder", project_id="vbot", session_id=None),
            {
                "agent_id": "builder",
                "name": "Status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
                "project_id": "vbot",
            },
            {"target": "builder@vbot", "project_id": "vbot"},
            id="project-target",
        ),
        pytest.param(
            {
                "agent_id": "main",
                "prompt": "Check status",
                "schedule_type": "interval",
                "interval_seconds": 7200,
                "repeat": 3,
            },
            _cron_job(
                name="Check status",
                schedule_type="interval",
                cron_expression=None,
                interval_seconds=7200,
                interval_anchor_at="2026-07-28T12:00:00+00:00",
                remaining_runs=3,
            ),
            {
                "agent_id": "main",
                "schedule_type": "interval",
                "interval_seconds": 7200,
                "remaining_runs": 3,
            },
            {"schedule": "every 2h", "remaining_runs": 3},
            id="interval-repeat-unnamed",
        ),
        pytest.param(
            {
                "agent_id": "main",
                "prompt": "Check once",
                "schedule_type": "once",
                "run_at": "2099-01-01T09:00:00+00:00",
            },
            _cron_job(
                name="Check once",
                schedule_type="once",
                cron_expression=None,
                run_at="2099-01-01T09:00:00+00:00",
            ),
            {"agent_id": "main", "schedule_type": "once", "run_at": "2099-01-01T09:00:00+00:00"},
            {"schedule": "2099-01-01T09:00:00+00:00", "run_at": "2099-01-01T09:00:00+00:00"},
            id="once",
        ),
    ],
)
async def test_cron_create_passes_the_normalized_job_to_the_service(
    params: JsonObject, job: SimpleNamespace, service_call: JsonObject, result: JsonObject
) -> None:
    state = _cron_state()
    cron_service = state.runtime.cron_service
    cron_service.create_job.return_value = job

    created = await rpc_result(state, "cron.create", **params)

    assert created.items() >= result.items()
    cron_service.create_job.assert_called_once_with(
        **{**_CREATE_DEFAULTS, "prompt": params["prompt"], **service_call}
    )


@pytest.mark.asyncio
async def test_cron_list_projects_each_job_with_its_next_fire_time() -> None:
    cron_service = Mock()
    cron_service.list_jobs.return_value = [
        _cron_job(
            id="job-1",
            agent_id="builder",
            project_id="vbot",
            name="Report check",
            prompt="Check reports",
            last_fired_at="2026-05-14T09:55:00+00:00",
            last_attempt_at="2026-05-14T09:55:00+00:00",
            last_completed_at="2026-05-14T09:56:00+00:00",
            last_run_id="run-1",
            last_outcome="success",
        )
    ]
    cron_service.system_timezone_name.return_value = "Europe/Berlin"
    cron_service.next_fire_at.return_value = "2026-05-14T10:05:00+00:00"
    cron_service.format_schedule.side_effect = CronService.format_schedule

    result = await rpc_result(_cron_state(cron_service), "cron.list")

    assert result == {
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
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "updates"),
    [
        pytest.param(
            {"name": "Updated status check", "prompt": "Updated prompt", "status": "paused"},
            {"name": "Updated status check", "prompt": "Updated prompt", "status": "paused"},
            id="fields",
        ),
        # A schedule change without repeat keeps the job's remaining count.
        pytest.param(
            {"schedule_type": "cron", "cron_expression": "0 10 * * *"},
            {"schedule_type": "cron", "cron_expression": "0 10 * * *"},
            id="schedule-keeps-count",
        ),
        # A recurring job may clear its repeat count.
        pytest.param({"repeat": None}, {"remaining_runs": None}, id="clear-repeat"),
        # Re-targeting re-parses the address, so a bare target clears the Project.
        pytest.param({"agent_id": "main"}, {"agent_id": "main", "project_id": None}, id="retarget"),
    ],
)
async def test_cron_update_passes_only_the_given_fields(
    params: JsonObject, updates: JsonObject
) -> None:
    state = _cron_state()
    cron_service = state.runtime.cron_service
    cron_service.update_job.return_value = _cron_job(status=updates.get("status", "active"))

    result = await rpc_result(state, "cron.update", id="job-1", **params)

    assert (result["id"], result["target"]) == ("job-123", "main")
    assert result["status"] == updates.get("status", "active")
    cron_service.update_job.assert_called_once_with("job-1", **updates)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "service_method", "job", "result"),
    [
        ("cron.delete", "delete_job", None, {"ok": True}),
        ("cron.enable", "enable_job", _cron_job(status="active"), {"status": "active"}),
        ("cron.disable", "disable_job", _cron_job(status="paused"), {"status": "paused"}),
    ],
)
async def test_cron_job_actions_address_one_job(
    method: str, service_method: str, job: SimpleNamespace | None, result: JsonObject
) -> None:
    state = _cron_state()
    getattr(state.runtime.cron_service, service_method).return_value = job

    response = await rpc_result(state, method, id="job-1")

    assert response.items() >= result.items()
    getattr(state.runtime.cron_service, service_method).assert_called_once_with("job-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("cron.create", {key: value for key, value in _CRON_PARAMS.items() if key != "agent_id"}),
        ("cron.create", {**_CRON_PARAMS, "timezone": "Europe/Berlin"}),
        ("cron.create", {key: value for key, value in _CRON_PARAMS.items() if key != "prompt"}),
        # A one-shot job cannot repeat, so an explicit null repeat is refused.
        (
            "cron.create",
            {
                "agent_id": "main",
                "prompt": "Run once",
                "schedule_type": "once",
                "run_at": "2026-08-01T09:00:00+00:00",
                "repeat": None,
            },
        ),
        (
            "cron.update",
            {
                "id": "job-1",
                "schedule_type": "once",
                "run_at": "2026-08-01T09:00:00+00:00",
                "repeat": None,
            },
        ),
        ("cron.list", {"extra": True}),
        ("bootstrap.list", {"extra": True}),
        ("cron.update", {"prompt": "missing id"}),
        ("cron.delete", {}),
        ("cron.enable", {}),
        ("cron.disable", {}),
        ("bootstrap.create", {"agent_id": "main", "prompt": "Check", "mode": "sometimes"}),
    ],
)
async def test_automation_rpcs_reject_invalid_params_before_the_service(
    method: str, params: JsonObject
) -> None:
    state = _cron_state()
    state.runtime.bootstrap_service = Mock()

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert state.runtime.cron_service.mock_calls == []
    assert state.runtime.bootstrap_service.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "resolver_error", "expected_code"),
    [
        (
            "ghost@vbot",
            ResolutionAgentNotFoundError("agent 'ghost' is not on project 'vbot' team"),
            "agent_not_found",
        ),
        (
            "ghost@vbot",
            ResolutionProjectNotFoundError("Project not found: vbot"),
            "project_not_found",
        ),
        (
            "stranded@vbot",
            AgentResolutionError("agent 'stranded' has no usable model"),
            "domain_error",
        ),
    ],
)
async def test_cron_target_resolution_failure_maps_to_precise_code(
    tmp_path: Path, address: str, resolver_error: AgentResolutionError, expected_code: str
) -> None:
    # A real CronService validates the target through the resolver: a missing
    # Agent or Project keeps its not-found code, a target that exists but cannot
    # run stays a domain error.
    resolver = Mock()
    resolver.resolve_agent.side_effect = resolver_error
    cron_service, _trigger_service = make_service(tmp_path, agent_resolver=resolver)
    state = _cron_state(cron_service, resolver=resolver)

    error = await rpc_error(state, "cron.create", **{**_CRON_PARAMS, "agent_id": address})

    assert error["code"] == expected_code
    resolver.resolve_agent.assert_called_once()
    assert cron_service.list_jobs() == []
