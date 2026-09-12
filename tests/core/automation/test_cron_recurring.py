"""Tests for cron recurring."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.automation.cron as cron_module
from core.runs import RunKind
from tests.core.automation.cron_test_support import (
    make_service,
)


@pytest.mark.asyncio
async def test_run_cron_job_fires_and_updates_last_fired_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Cron prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )

    class ImmediateCronIter:
        @staticmethod
        def is_valid(_expression: str) -> bool:
            return True

        def __init__(self, _expression: str, base_time: datetime) -> None:
            self._next_fire = base_time

        def get_next(self, _return_type: Any) -> datetime:
            return self._next_fire

    monkeypatch.setattr("core.automation._cron_schedule.croniter", ImmediateCronIter)

    async def trigger_and_pause(
        _agent_id: str,
        _prompt: str,
        _session_id: str | None = None,
        *,
        project_id: str | None = None,
        run_kind: RunKind,
        contributes_to_agent_activity: bool,
    ) -> None:
        assert run_kind is RunKind.CRON
        assert contributes_to_agent_activity is False
        service._jobs[job.id].status = "paused"

    trigger_service.trigger_run.side_effect = trigger_and_pause

    # Act
    await service._run_cron_job(job)

    # Assert
    trigger_service.trigger_run.assert_awaited_once_with(
        "agent-one",
        "Cron prompt",
        None,
        project_id=None,
        run_kind=RunKind.CRON,
        contributes_to_agent_activity=False,
    )
    updated = service.get_job(job.id)
    assert updated.last_fired_at is not None
    assert updated.last_fired_at.endswith("+00:00")


@pytest.mark.asyncio
async def test_run_cron_job_continues_after_trigger_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Cron prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )

    class ImmediateCronIter:
        @staticmethod
        def is_valid(_expression: str) -> bool:
            return True

        def __init__(self, _expression: str, base_time: datetime) -> None:
            self._next_fire = base_time

        def get_next(self, _return_type: Any) -> datetime:
            return self._next_fire

    async def trigger_then_fail_then_pause(
        _agent_id: str,
        _prompt: str,
        _session_id: str | None = None,
        *,
        project_id: str | None = None,
        run_kind: RunKind,
        contributes_to_agent_activity: bool,
    ) -> None:
        assert run_kind is RunKind.CRON
        assert contributes_to_agent_activity is False
        if trigger_service.trigger_run.await_count == 1:
            raise RuntimeError("boom")
        service._jobs[job.id].status = "paused"

    monkeypatch.setattr("core.automation._cron_schedule.croniter", ImmediateCronIter)
    monkeypatch.setattr(cron_module.asyncio, "sleep", AsyncMock())
    trigger_service.trigger_run.side_effect = trigger_then_fail_then_pause

    # Act
    await service._run_cron_job(job)

    # Assert
    assert trigger_service.trigger_run.await_count == 2
    updated = service.get_job(job.id)
    assert updated.status == "paused"
    assert updated.last_fired_at is not None
    assert updated.last_fired_at.endswith("+00:00")


def test_crud_status_or_schedule_changes_restart_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Cron prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )
    service._started = True

    started_jobs: list[str] = []
    cancelled_jobs: list[str] = []

    def record_start(job_to_start: cron_module.CronJob) -> None:
        started_jobs.append(job_to_start.id)

    def record_cancel(job_id: str) -> None:
        cancelled_jobs.append(job_id)

    monkeypatch.setattr(service, "_start_job_task", record_start)
    monkeypatch.setattr(service, "_cancel_job_task", record_cancel)

    # Act
    service.update_job(job.id, cron_expression="*/5 * * * *")
    service.disable_job(job.id)
    service.enable_job(job.id)

    # Assert
    assert cancelled_jobs == [job.id, job.id, job.id]
    assert started_jobs == [job.id, job.id]
