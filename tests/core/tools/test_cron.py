"""Tests for the cron management tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import Mock

import pytest

import core.tools.cron as cron_tool_module
from core.automation.cron import (
    CronJob,
    CronJobNotFoundError,
    CronJobValidationError,
    ParsedSchedule,
)
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.tools.cron import CRON_TOOL_NAME, CRON_TOOL_PARAMETERS, register_cron_tool
from core.tools.tools import ToolContext, ToolRegistry, tool_failure

ScheduleType = Literal["cron", "interval", "once"]
CronStatus = Literal["active", "paused", "completed", "failed", "missed"]


def _context(tmp_path: Path, *, project_id: str | None = None) -> ToolContext:
    return ToolContext(
        agent_id="agent-one",
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=CRON_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        project_id=project_id,
    )


async def _dispatch(
    registry: ToolRegistry,
    tmp_path: Path,
    arguments: dict[str, object],
    *,
    project_id: str | None = None,
) -> dict[str, object]:
    try:
        return await registry.dispatch(
            _context(tmp_path, project_id=project_id), arguments, [CRON_TOOL_NAME]
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)


def _make_job(
    *,
    job_id: str,
    name: str = "Run task",
    prompt: str = "Run task",
    schedule_type: ScheduleType = "cron",
    cron_expression: str | None = "*/5 * * * *",
    interval_seconds: int | None = None,
    interval_anchor_at: str | None = None,
    run_at: str | None = None,
    remaining_runs: int | None = None,
    session_id: str | None = None,
    status: CronStatus = "active",
    last_fired_at: str | None = None,
) -> CronJob:
    return CronJob(
        id=job_id,
        agent_id="agent-one",
        name=name,
        prompt=prompt,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        interval_seconds=interval_seconds,
        interval_anchor_at=interval_anchor_at,
        run_at=run_at,
        remaining_runs=remaining_runs,
        session_id=session_id,
        status=status,
        last_fired_at=last_fired_at,
        created_at="2026-05-14T12:00:00+00:00",
    )


def _cron_service_mock() -> Mock:
    service = Mock()
    service.system_timezone_name.return_value = "UTC"
    service.parse_schedule.side_effect = lambda schedule: (
        ParsedSchedule(
            schedule_type="interval",
            interval_seconds=7200,
            interval_anchor_at="2026-05-14T12:00:00+00:00",
        )
        if schedule == "every 2h"
        else ParsedSchedule(schedule_type="once", run_at="2026-05-14T12:30:00+00:00")
        if schedule == "in 30m"
        else ParsedSchedule(schedule_type="once", run_at=schedule)
        if "T" in schedule
        else ParsedSchedule(schedule_type="cron", cron_expression=schedule)
    )
    service.format_schedule.side_effect = lambda job: (
        job.cron_expression
        if job.schedule_type == "cron"
        else f"every {job.interval_seconds // 3600}h"
        if job.schedule_type == "interval"
        else job.run_at
    )
    service.next_fire_at.side_effect = lambda job: (
        "2026-05-14T12:05:00+00:00"
        if job.status == "active" and job.schedule_type == "cron"
        else job.run_at
        if job.status == "active" and job.schedule_type == "once"
        else None
    )
    return service


def test_schema_exposes_flat_action_contract() -> None:
    assert CRON_TOOL_PARAMETERS["type"] == "object"
    assert "oneOf" not in CRON_TOOL_PARAMETERS
    assert "additionalProperties" not in CRON_TOOL_PARAMETERS
    properties = cast(dict[str, Any], CRON_TOOL_PARAMETERS["properties"])
    assert set(properties) == {
        "action",
        "id",
        "target",
        "name",
        "prompt",
        "schedule",
        "repeat",
    }
    assert properties["action"]["enum"] == [
        "create",
        "list",
        "update",
        "delete",
        "enable",
        "disable",
    ]
    assert CRON_TOOL_PARAMETERS["required"] == ["action"]
    assert properties["repeat"]["type"] == ["integer", "null"]
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )


def test_nested_create_operation_is_repaired(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.create_job.return_value = _make_job(job_id="job-create")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "request": {
                    "operation": "create",
                    "prompt": "Run this later",
                    "schedule": "every 2h",
                }
            },
        )
    )

    assert result["ok"] is True
    cron_service.create_job.assert_called_once()


def test_create_action_returns_success(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.create_job.return_value = _make_job(job_id="job-create")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "target": "agent-one",
                "name": "Later task",
                "prompt": "Run this later",
                "schedule": "every 2h",
                "repeat": 3,
            },
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-create"
    assert job["next_fire_at"] is not None
    cron_service.create_job.assert_called_once_with(
        agent_id="agent-one",
        name="Later task",
        prompt="Run this later",
        schedule_type="interval",
        cron_expression=None,
        interval_seconds=7200,
        interval_anchor_at="2026-05-14T12:00:00+00:00",
        run_at=None,
        remaining_runs=3,
        session_id=None,
        project_id=None,
    )


def test_create_defaults_to_current_agent_and_project(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.create_job.return_value = _make_job(job_id="job-current")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "name": "Continue project",
                "prompt": "Continue project work",
                "schedule": "0 9 * * *",
            },
            project_id="vbot",
        )
    )

    assert result["ok"] is True
    cron_service.create_job.assert_called_once_with(
        agent_id="agent-one",
        name="Continue project",
        prompt="Continue project work",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        interval_seconds=None,
        interval_anchor_at=None,
        run_at=None,
        remaining_runs=None,
        session_id=None,
        project_id="vbot",
    )


def test_create_derives_name_when_omitted(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.create_job.return_value = _make_job(
        job_id="job-derived",
        name="Run this later",
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "prompt": "Run this later",
                "schedule": "*/5 * * * *",
            },
        )
    )

    assert result["ok"] is True
    assert cron_service.create_job.call_args.kwargs["name"] is None


def test_create_rejects_repeat_above_one_for_one_time_schedule(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "prompt": "Run this later",
                "schedule": "in 30m",
                "repeat": 2,
            },
        )
    )

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert "repeat must be 1 for a one-time schedule" in error["message"]
    cron_service.create_job.assert_not_called()


def test_create_rejects_null_repeat_for_one_time_schedule(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "prompt": "Run this later",
                "schedule": "in 30m",
                "repeat": None,
            },
        )
    )

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert "repeat cannot be null for a one-time schedule" in error["message"]
    cron_service.create_job.assert_not_called()


def test_list_action_returns_success_and_next_fire_at(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.list_jobs.return_value = [
        _make_job(job_id="job-cron", schedule_type="cron", status="active"),
        _make_job(
            job_id="job-once",
            schedule_type="once",
            cron_expression=None,
            run_at="2026-05-15T12:00:00+00:00",
            status="active",
        ),
        _make_job(job_id="job-paused", schedule_type="cron", status="paused"),
    ]
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, {"action": "list"}))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    jobs = cast(list[dict[str, Any]], data["jobs"])
    assert [job["id"] for job in jobs] == ["job-cron", "job-once", "job-paused"]
    assert jobs[0]["name"] == "Run task"
    assert jobs[0]["next_fire_at"] is not None
    assert jobs[1]["next_fire_at"] == "2026-05-15T12:00:00+00:00"
    assert jobs[2]["next_fire_at"] is None
    cron_service.list_jobs.assert_called_once_with()
    display = registry.display_for_call(CRON_TOOL_NAME, {"action": "list"}, result=result)
    assert display["facts"] == [{"kind": "count", "value": 3, "unit": "results", "at_least": False}]


def test_list_action_uses_canonical_service_projection(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.list_jobs.return_value = [
        _make_job(job_id="job-cron", schedule_type="cron", status="active")
    ]
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, {"action": "list"}))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    jobs = cast(list[dict[str, Any]], data["jobs"])
    assert jobs[0]["next_fire_at"] is not None


def test_update_action_returns_success(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.update_job.return_value = _make_job(
        job_id="job-update",
        name="Updated task",
        prompt="Updated prompt",
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "update",
                "id": "job-update",
                "name": "Updated task",
                "prompt": "Updated prompt",
            },
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-update"
    assert job["name"] == "Updated task"
    assert job["prompt"] == "Updated prompt"
    cron_service.update_job.assert_called_once_with(
        "job-update",
        name="Updated task",
        prompt="Updated prompt",
    )


def test_update_schedule_without_repeat_preserves_the_current_count(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.update_job.return_value = _make_job(
        job_id="job-update",
        schedule_type="interval",
        cron_expression=None,
        interval_seconds=7200,
        interval_anchor_at="2026-05-14T12:00:00+00:00",
        remaining_runs=3,
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "update",
                "id": "job-update",
                "schedule": "every 2h",
            },
        )
    )

    assert result["ok"] is True
    cron_service.update_job.assert_called_once_with(
        "job-update",
        schedule_type="interval",
        cron_expression=None,
        interval_seconds=7200,
        interval_anchor_at="2026-05-14T12:00:00+00:00",
        run_at=None,
    )


def test_update_null_repeat_makes_a_recurring_job_unlimited(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.update_job.return_value = _make_job(
        job_id="job-update",
        remaining_runs=None,
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "update",
                "id": "job-update",
                "repeat": None,
            },
        )
    )

    assert result["ok"] is True
    cron_service.update_job.assert_called_once_with("job-update", remaining_runs=None)


def test_update_rejects_null_repeat_with_a_one_time_schedule(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "update",
                "id": "job-update",
                "schedule": "in 30m",
                "repeat": None,
            },
        )
    )

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert "repeat cannot be null for a one-time schedule" in error["message"]
    cron_service.update_job.assert_not_called()


def test_delete_action_returns_success(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {"action": "delete", "id": "job-delete"},
        )
    )

    assert result["ok"] is True
    assert result["data"] == {"id": "job-delete", "deleted": True}
    cron_service.delete_job.assert_called_once_with("job-delete")


def test_enable_action_returns_success(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.enable_job.return_value = _make_job(
        job_id="job-enable",
        status="active",
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {"action": "enable", "id": "job-enable"},
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-enable"
    assert job["status"] == "active"
    cron_service.enable_job.assert_called_once_with("job-enable")


def test_past_one_time_schedule_is_rejected_with_future_time_guidance(tmp_path: Path) -> None:
    from tests.core.automation.cron_test_support import make_service

    cron_service, trigger_service = make_service(tmp_path, tz="Europe/Berlin")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)
    past = "2020-01-01T09:00:00"

    created = asyncio.run(
        _dispatch(registry, tmp_path, {"action": "create", "prompt": "Remind me", "schedule": past})
    )
    job_id = cast(
        dict[str, Any],
        asyncio.run(
            _dispatch(
                registry,
                tmp_path,
                {"action": "create", "prompt": "Remind me", "schedule": "in 30m"},
            )
        )["data"],
    )["job"]["id"]
    updated = asyncio.run(
        _dispatch(registry, tmp_path, {"action": "update", "id": job_id, "schedule": past})
    )

    for result in (created, updated):
        assert result["ok"] is False
        error = cast(dict[str, Any], result["error"])
        assert error["code"] == "invalid_arguments"
        assert error["retryable"] is False
    assert [job.id for job in cron_service.list_jobs()] == [job_id]
    trigger_service.trigger_run.assert_not_called()


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize(
    ("resolver_error", "code", "reason", "recommendation"),
    [
        (
            ResolutionAgentNotFoundError("agent 'ghost' is not on project 'vbot' team"),
            "agent_not_found",
            None,
            cron_tool_module._TARGET_ADDRESS_RECOMMENDATION,
        ),
        (
            ResolutionProjectNotFoundError("Project not found: vbot"),
            "project_not_found",
            None,
            cron_tool_module._TARGET_ADDRESS_RECOMMENDATION,
        ),
        (
            AgentResolutionError("agent 'ghost' has no usable model"),
            "agent_unavailable",
            "agent 'ghost' has no usable model",
            cron_tool_module._TARGET_UNAVAILABLE_RECOMMENDATION,
        ),
    ],
)
def test_unresolvable_target_gets_target_guidance_instead_of_schedule_examples(
    tmp_path: Path,
    action: str,
    resolver_error: AgentResolutionError,
    code: str,
    reason: str | None,
    recommendation: str,
) -> None:
    from tests.core.automation.cron_test_support import make_service

    resolver = Mock()
    cron_service, trigger_service = make_service(tmp_path, agent_resolver=resolver)
    job = cron_service.create_job(
        agent_id="agent-one", prompt="Ping", schedule_type="interval", interval_seconds=7200
    )
    resolver.resolve_agent.reset_mock()
    resolver.resolve_agent.side_effect = resolver_error
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)
    arguments: dict[str, object] = (
        {"action": "create", "prompt": "Ping", "schedule": "every 2h", "target": "ghost@vbot"}
        if action == "create"
        else {"action": "update", "id": job.id, "target": "ghost@vbot"}
    )

    # Dispatch directly: the Tool itself must turn the failure into a result.
    result = asyncio.run(registry.dispatch(_context(tmp_path), arguments, [CRON_TOOL_NAME]))

    assert result["ok"] is False
    error = cast(dict[str, Any], result["error"])
    assert error["code"] == code
    assert error["retryable"] is False
    assert error["message"].endswith(recommendation)
    assert cron_tool_module._ACTION_RECOMMENDATIONS[action] not in error["message"]
    if reason is not None:
        assert reason in error["message"]
    resolver.resolve_agent.assert_called_once_with("vbot", "ghost")
    assert [(stored.id, stored.project_id) for stored in cron_service.list_jobs()] == [
        (job.id, None)
    ]
    trigger_service.trigger_run.assert_not_called()


def test_malformed_target_gets_target_guidance(tmp_path: Path) -> None:
    from tests.core.automation.cron_test_support import make_service

    cron_service, _trigger_service = make_service(tmp_path)
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        registry.dispatch(
            _context(tmp_path),
            {"action": "create", "prompt": "Ping", "schedule": "every 2h", "target": "ghost@"},
            [CRON_TOOL_NAME],
        )
    )

    error = cast(dict[str, Any], result["error"])
    # A malformed address names no target, so it is an argument error.
    assert error["code"] == "invalid_arguments"
    assert error["retryable"] is False
    assert error["message"].endswith(cron_tool_module._TARGET_ADDRESS_RECOMMENDATION)
    assert cron_service.list_jobs() == []


def test_schedule_format_failure_keeps_schedule_examples(tmp_path: Path) -> None:
    from tests.core.automation.cron_test_support import make_service

    cron_service, _trigger_service = make_service(tmp_path)
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        registry.dispatch(
            _context(tmp_path),
            {"action": "create", "prompt": "Ping", "schedule": "whenever"},
            [CRON_TOOL_NAME],
        )
    )

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert error["message"].endswith(cron_tool_module._ACTION_RECOMMENDATIONS["create"])


def test_disable_action_returns_success(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.disable_job.return_value = _make_job(
        job_id="job-disable",
        status="paused",
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {"action": "disable", "id": "job-disable"},
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-disable"
    assert job["status"] == "paused"
    cron_service.disable_job.assert_called_once_with("job-disable")


def test_invalid_operation_returns_contract_failure(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, {"action": "invalid"}))

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert error["retryable"] is False
    assert '"action" must be one of "create", "list"' in error["message"]
    assert 'received "invalid"' in error["message"]


def test_multiple_top_level_operation_objects_are_rejected(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {"list": {}, "delete": {"id": "job-delete"}},
        )
    )

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert error["retryable"] is False


def test_update_requires_a_change_beyond_id(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {"action": "update", "id": "job-update"},
        )
    )

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert error["retryable"] is False
    assert "update requires at least one field to change" in error["message"]
    cron_service.update_job.assert_not_called()


@pytest.mark.parametrize("removed_field", ["status", "session_id", "timezone"])
def test_removed_agent_fields_are_rejected(
    tmp_path: Path,
    removed_field: str,
) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)
    arguments: dict[str, object] = {
        "name": "Later task",
        "prompt": "Run this later",
        "schedule": "*/5 * * * *",
        removed_field: "active",
    }

    result = asyncio.run(_dispatch(registry, tmp_path, {"action": "create", **arguments}))

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert error["retryable"] is False
    assert f'"{removed_field}" is not a parameter' in error["message"]
    cron_service.create_job.assert_not_called()


def test_recognizable_agent_id_alias_selects_target(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.create_job.return_value = _make_job(job_id="job-legacy")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "agent_id": "builder@vbot",
                "name": "Later task",
                "prompt": "Run this later",
                "schedule": "*/5 * * * *",
            },
        )
    )

    assert result["ok"] is True
    assert cron_service.create_job.call_args.kwargs["agent_id"] == "builder"
    assert cron_service.create_job.call_args.kwargs["project_id"] == "vbot"


def test_stringified_operation_payload_is_repaired(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.list_jobs.return_value = []
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, {"list": "{}"}))

    assert result["ok"] is True
    cron_service.list_jobs.assert_called_once()


def test_nested_create_request_is_repaired(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.create_job.return_value = _make_job(job_id="job-envelope")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "request": {
                    "operation": "create",
                    "name": "Later task",
                    "prompt": "Run this later",
                    "schedule": "*/5 * * * *",
                }
            },
        )
    )

    assert result["ok"] is True
    cron_service.create_job.assert_called_once()


def test_create_invalid_cron_expression_returns_failure(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.parse_schedule.side_effect = CronJobValidationError(
        "schedule is not a valid five-field cron expression"
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "target": "agent-one",
                "name": "Later task",
                "prompt": "Run this later",
                "schedule": "not a valid cron expression",
            },
        )
    )

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert error["retryable"] is False
    assert "schedule is not a valid five-field cron expression" in error["message"]
    assert '"schedule":"0 9 * * *"' in error["message"]
    cron_service.create_job.assert_not_called()


@pytest.mark.parametrize(
    ("action", "method_name", "arguments"),
    [
        (
            "update",
            "update_job",
            {"action": "update", "id": "missing", "prompt": "Updated"},
        ),
        ("delete", "delete_job", {"action": "delete", "id": "missing"}),
        ("enable", "enable_job", {"action": "enable", "id": "missing"}),
        ("disable", "disable_job", {"action": "disable", "id": "missing"}),
    ],
)
def test_unknown_id_failures_return_job_not_found(
    tmp_path: Path,
    action: str,
    method_name: str,
    arguments: dict[str, object],
) -> None:
    cron_service = _cron_service_mock()
    getattr(cron_service, method_name).side_effect = CronJobNotFoundError(
        "Cron job not found: missing"
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, arguments))

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "job_not_found"
    assert error["retryable"] is False
    assert "Cron job not found: missing" in error["message"]
    assert '{"action":"list"}' in error["message"]
    getattr(cron_service, method_name).assert_called_once()


def test_conflicting_cron_target_aliases_do_not_create_job(tmp_path: Path) -> None:
    service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, service)
    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "target": "first",
                "agent_id": "second",
                "prompt": "Check",
                "schedule": "every 2h",
            },
        )
    )
    assert not result["ok"]
    assert "different Agents" in cast(dict[str, Any], result["error"])["message"]
    service.create_job.assert_not_called()
