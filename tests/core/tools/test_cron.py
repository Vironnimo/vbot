"""Tests for the cron management tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import Mock

import pytest

from core.automation.cron import CronJob, CronJobNotFoundError
from core.tools.cron import CRON_TOOL_NAME, register_cron_tool
from core.tools.tools import ToolContext, ToolRegistry, tool_failure

ScheduleType = Literal["cron", "once"]
CronStatus = Literal["active", "paused", "completed"]


def _context(tmp_path: Path) -> ToolContext:
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
    )


async def _dispatch(
    registry: ToolRegistry,
    tmp_path: Path,
    arguments: dict[str, object],
) -> dict[str, object]:
    return await registry.dispatch(_context(tmp_path), arguments, [CRON_TOOL_NAME])


def _make_job(
    *,
    job_id: str,
    prompt: str = "Run task",
    schedule_type: ScheduleType = "cron",
    cron_expression: str | None = "*/5 * * * *",
    run_at: str | None = None,
    timezone: str | None = "UTC",
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
        timezone=timezone,
        session_id=session_id,
        status=status,
        last_fired_at=last_fired_at,
        created_at="2026-05-14T12:00:00+00:00",
    )


def test_create_action_returns_success(tmp_path: Path) -> None:
    cron_service = Mock()
    cron_service.create_job.return_value = _make_job(job_id="job-create")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "agent_id": "agent-one",
                "prompt": "Run this later",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
                "timezone": "UTC",
                "session_id": "session-one",
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
        timezone="UTC",
        session_id="session-one",
    )


def test_list_action_returns_success_and_next_fire_at(tmp_path: Path) -> None:
    cron_service = Mock()
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
    assert jobs[0]["next_fire_at"] is not None
    assert jobs[1]["next_fire_at"] is None
    assert jobs[2]["next_fire_at"] is None
    cron_service.list_jobs.assert_called_once_with()


def test_update_action_returns_success(tmp_path: Path) -> None:
    cron_service = Mock()
    cron_service.update_job.return_value = _make_job(job_id="job-update", prompt="Updated prompt")
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "update",
                "id": "job-update",
                "prompt": "Updated prompt",
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
    cron_service = Mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "delete",
                "id": "job-delete",
            },
        )
    )

    assert result["ok"] is True
    assert result["data"] == {"id": "job-delete", "deleted": True}
    cron_service.delete_job.assert_called_once_with("job-delete")


def test_enable_action_returns_success(tmp_path: Path) -> None:
    cron_service = Mock()
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
            {
                "action": "enable",
                "id": "job-enable",
            },
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-enable"
    assert job["status"] == "active"
    cron_service.enable_job.assert_called_once_with("job-enable")


def test_disable_action_returns_success(tmp_path: Path) -> None:
    cron_service = Mock()
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
            {
                "action": "disable",
                "id": "job-disable",
            },
        )
    )

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    job = cast(dict[str, Any], data["job"])
    assert job["id"] == "job-disable"
    assert job["status"] == "paused"
    cron_service.disable_job.assert_called_once_with("job-disable")


def test_invalid_action_returns_failure(tmp_path: Path) -> None:
    cron_service = Mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, {"action": "invalid"}))

    assert result == tool_failure(
        "invalid_arguments",
        "action must be one of: create, delete, disable, enable, list, update",
    )


def test_create_invalid_cron_expression_returns_failure(tmp_path: Path) -> None:
    cron_service = Mock()
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(
        _dispatch(
            registry,
            tmp_path,
            {
                "action": "create",
                "agent_id": "agent-one",
                "prompt": "Run this later",
                "schedule_type": "cron",
                "cron_expression": "not-a-cron-expression",
            },
        )
    )

    assert result == tool_failure("invalid_arguments", "cron_expression is invalid")
    cron_service.create_job.assert_not_called()


@pytest.mark.parametrize(
    ("action", "method_name", "arguments"),
    [
        ("update", "update_job", {"action": "update", "id": "missing", "prompt": "Updated"}),
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
    cron_service = Mock()
    getattr(cron_service, method_name).side_effect = CronJobNotFoundError(
        "Cron job not found: missing"
    )
    registry = ToolRegistry()
    register_cron_tool(registry, cron_service)

    result = asyncio.run(_dispatch(registry, tmp_path, arguments))

    assert result == tool_failure("job_not_found", "Cron job not found: missing")
    getattr(cron_service, method_name).assert_called_once()
