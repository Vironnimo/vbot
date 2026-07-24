"""Tests for the cron management tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import Mock

import pytest

from core.automation.cron import CronJob, CronJobNotFoundError, CronJobValidationError
from core.tools.cron import CRON_TOOL_NAME, CRON_TOOL_PARAMETERS, register_cron_tool
from core.tools.tools import ToolContext, ToolRegistry, tool_failure

ScheduleType = Literal["cron", "once"]
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
        app_root=tmp_path,
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
    return await registry.dispatch(
        _context(tmp_path, project_id=project_id), arguments, [CRON_TOOL_NAME]
    )


def _make_job(
    *,
    job_id: str,
    prompt: str = "Run task",
    schedule_type: ScheduleType = "cron",
    cron_expression: str | None = "*/5 * * * *",
    run_at: str | None = None,
    session_id: str | None = None,
    status: CronStatus = "active",
    last_fired_at: str | None = None,
) -> CronJob:
    return CronJob(
        id=job_id,
        agent_id="agent-one",
        prompt=prompt,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        run_at=run_at,
        session_id=session_id,
        status=status,
        last_fired_at=last_fired_at,
        created_at="2026-05-14T12:00:00+00:00",
    )


def _cron_service_mock() -> Mock:
    service = Mock()
    service.system_timezone_name.return_value = "UTC"
    service.next_fire_at.side_effect = lambda job: (
        "2026-05-14T12:05:00+00:00"
        if job.status == "active" and job.schedule_type == "cron"
        else job.run_at
        if job.status == "active" and job.schedule_type == "once"
        else None
    )
    return service


def test_schema_separates_operations_and_omits_agent_inapplicable_fields() -> None:
    properties = cast(dict[str, dict[str, Any]], CRON_TOOL_PARAMETERS["properties"])

    assert set(properties) == {"create", "list", "update", "delete", "enable", "disable"}
    assert CRON_TOOL_PARAMETERS["minProperties"] == 1
    assert CRON_TOOL_PARAMETERS["maxProperties"] == 1
    assert properties["create"]["required"] == ["prompt", "schedule_type"]
    assert properties["update"]["required"] == ["id"]

    create_fields = cast(dict[str, Any], properties["create"]["properties"])
    update_fields = cast(dict[str, Any], properties["update"]["properties"])
    for removed_field in ("status", "session_id", "timezone", "agent_id"):
        assert removed_field not in create_fields
        assert removed_field not in update_fields
    assert "target" in create_fields
    assert "target" in update_fields


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
                "create": {
                    "target": "agent-one",
                    "prompt": "Run this later",
                    "schedule_type": "cron",
                    "cron_expression": "*/5 * * * *",
                }
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
        prompt="Run this later",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        run_at=None,
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
                "create": {
                    "prompt": "Continue project work",
                    "schedule_type": "cron",
                    "cron_expression": "0 9 * * *",
                }
            },
            project_id="vbot",
        )
    )

    assert result["ok"] is True
    cron_service.create_job.assert_called_once_with(
        agent_id="agent-one",
        prompt="Continue project work",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        run_at=None,
        session_id=None,
        project_id="vbot",
    )


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

    result = asyncio.run(_dispatch(registry, tmp_path, {"list": {}}))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    jobs = cast(list[dict[str, Any]], data["jobs"])
    assert [job["id"] for job in jobs] == ["job-cron", "job-once", "job-paused"]
    assert jobs[0]["next_fire_at"] is not None
    assert jobs[1]["next_fire_at"] == "2026-05-15T12:00:00+00:00"
    assert jobs[2]["next_fire_at"] is None
    cron_service.list_jobs.assert_called_once_with()


def test_list_action_uses_canonical_service_projection(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.list_jobs.return_value = [
        _make_job(job_id="job-cron", schedule_type="cron", status="active")
    ]
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, {"list": {}}))

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    jobs = cast(list[dict[str, Any]], data["jobs"])
    assert jobs[0]["next_fire_at"] is not None


def test_update_action_returns_success(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.update_job.return_value = _make_job(job_id="job-update", prompt="Updated prompt")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "update": {
                    "id": "job-update",
                    "prompt": "Updated prompt",
                }
            },
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-update"
    assert job["prompt"] == "Updated prompt"
    cron_service.update_job.assert_called_once_with("job-update", prompt="Updated prompt")


def test_delete_action_returns_success(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {"delete": {"id": "job-delete"}},
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
            {"enable": {"id": "job-enable"}},
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-enable"
    assert job["status"] == "active"
    cron_service.enable_job.assert_called_once_with("job-enable")


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
            {"disable": {"id": "job-disable"}},
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-disable"
    assert job["status"] == "paused"
    cron_service.disable_job.assert_called_once_with("job-disable")


def test_invalid_action_returns_failure(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, {"action": "invalid"}))

    assert result == tool_failure(
        "invalid_arguments",
        "action must be one of: create, delete, disable, enable, list, update",
        retryable=False,
    )


def test_multiple_operations_return_non_retryable_failure(tmp_path: Path) -> None:
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

    assert result == tool_failure(
        "invalid_arguments",
        "Exactly one operation is required: create, delete, disable, enable, list, or update",
        retryable=False,
    )


def test_update_requires_a_change_beyond_id(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {"update": {"id": "job-update"}},
        )
    )

    assert result == tool_failure(
        "invalid_arguments",
        "update requires at least one field to change",
        retryable=False,
    )
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
        "prompt": "Run this later",
        "schedule_type": "cron",
        "cron_expression": "*/5 * * * *",
        removed_field: "active",
    }

    result = asyncio.run(_dispatch(registry, tmp_path, {"create": arguments}))

    assert result == tool_failure(
        "invalid_arguments",
        f"Unknown argument(s) for action 'create': {removed_field}",
        retryable=False,
    )
    cron_service.create_job.assert_not_called()


def test_legacy_flat_shape_maps_agent_id_to_target(tmp_path: Path) -> None:
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
                "prompt": "Run this later",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        )
    )

    assert result["ok"] is True
    cron_service.create_job.assert_called_once_with(
        agent_id="builder",
        prompt="Run this later",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        run_at=None,
        session_id=None,
        project_id="vbot",
    )


def test_create_invalid_cron_expression_returns_failure(tmp_path: Path) -> None:
    cron_service = _cron_service_mock()
    cron_service.create_job.side_effect = CronJobValidationError("cron_expression is invalid")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "create": {
                    "target": "agent-one",
                    "prompt": "Run this later",
                    "schedule_type": "cron",
                    "cron_expression": "not-a-cron-expression",
                }
            },
        )
    )

    assert result == tool_failure(
        "invalid_arguments", "cron_expression is invalid", retryable=False
    )
    cron_service.create_job.assert_called_once()


@pytest.mark.parametrize(
    ("action", "method_name", "arguments"),
    [
        ("update", "update_job", {"update": {"id": "missing", "prompt": "Updated"}}),
        ("delete", "delete_job", {"delete": {"id": "missing"}}),
        ("enable", "enable_job", {"enable": {"id": "missing"}}),
        ("disable", "disable_job", {"disable": {"id": "missing"}}),
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

    assert result == tool_failure("job_not_found", "Cron job not found: missing", retryable=False)
    getattr(cron_service, method_name).assert_called_once()
