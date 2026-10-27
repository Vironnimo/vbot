"""Tests for cron recurring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import pytest

import core.automation.cron as cron_module
from core.automation import _cron_timing as cron_timing
from core.runs import RunKind, RunStatus
from tests.core.automation.cron_test_support import (
    make_service,
    scripted_runs,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_steps_back", [False, True], ids=["steady", "clock-steps-back"])
async def test_run_cron_job_fires_and_continues_after_trigger_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_steps_back: bool,
) -> None:
    # Arrange
    clock = [datetime(2030, 10, 5, 9, 59, 30, tzinfo=UTC)]
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: clock[0])
    service, trigger_service = make_service(tmp_path, tz="UTC")
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Cron prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )
    fired_at: list[datetime] = []

    async def trigger_then_fail_then_pause(
        _agent_id: str,
        _prompt: str,
        _session_id: str | None = None,
        *,
        project_id: str | None = None,
        run_kind: RunKind,
        contributes_to_agent_activity: bool,
        **_delivery: object,
    ) -> None:
        assert run_kind is RunKind.CRON
        assert contributes_to_agent_activity is False
        fired_at.append(clock[0])
        if trigger_service.trigger_run.await_count == 1:
            if clock_steps_back:
                # A time sync steps the wall clock back past the fire just made.
                clock[0] -= timedelta(seconds=40)
            raise RuntimeError("boom")
        service._jobs[job.id].status = "paused"

    async def sleep_until(target: datetime, **_kwargs: Any) -> bool:
        clock[0] = max(clock[0], target)
        return True

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", sleep_until)
    trigger_service.trigger_run.side_effect = trigger_then_fail_then_pause

    # Act
    await service._scheduler._run_recurring_job(job, "cron")

    # Assert: the failed fire is not repeated, the next due time fires.
    assert fired_at == [
        datetime(2030, 10, 5, 10, 0, tzinfo=UTC),
        datetime(2030, 10, 5, 10, 1, tzinfo=UTC),
    ]
    trigger_service.trigger_run.assert_awaited_with(
        "agent-one",
        "Cron prompt",
        None,
        project_id=None,
        working_project_id=None,
        run_kind=RunKind.CRON,
        schedule_id=job.id,
        notification_policy=ANY,
        contributes_to_agent_activity=False,
        context_note=ANY,
        completion_activity=ANY,
    )
    updated = service.get_job(job.id)
    assert updated.status == "paused"
    assert updated.last_fired_at == "2030-10-05T10:01:00+00:00"


@pytest.mark.asyncio
async def test_cron_fires_both_sides_of_a_non_hour_spring_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime(2026, 10, 3, 15, 20, tzinfo=UTC)]
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: clock[0])
    service, trigger = make_service(tmp_path, tz="Australia/Lord_Howe")
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Reminder",
        schedule_type="cron",
        cron_expression="15,30 2 * * *",
    )
    scripted_runs(trigger, (RunStatus.COMPLETED, "one"), (RunStatus.COMPLETED, "two"))
    scripted_trigger = trigger.trigger_run.side_effect
    fired_at: list[datetime] = []

    async def record_fire(*args: Any, **kwargs: Any) -> Any:
        fired_at.append(clock[0])
        if len(fired_at) == 2:
            service._jobs[job.id].status = "paused"
        return await scripted_trigger(*args, **kwargs)

    async def sleep_until(target: datetime, **_kwargs: Any) -> bool:
        clock[0] = target
        return True

    trigger.trigger_run.side_effect = record_fire
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", sleep_until)
    await service._scheduler._run_recurring_job(job, "cron")

    assert fired_at == [
        datetime(2026, 10, 3, 15, 30, tzinfo=UTC),
        datetime(2026, 10, 3, 15, 45, tzinfo=UTC),
    ]


@pytest.mark.asyncio
async def test_crud_status_or_schedule_changes_restart_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)
    job = await service.create_job(
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

    monkeypatch.setattr(service._scheduler, "_start_job_task", record_start)
    monkeypatch.setattr(service._scheduler, "_cancel_job_task", record_cancel)

    # Act
    await service.update_job(job.id, cron_expression="*/5 * * * *")
    await service.disable_job(job.id)
    await service.enable_job(job.id)

    # Assert
    assert cancelled_jobs == [job.id, job.id, job.id]
    assert started_jobs == [job.id, job.id]
