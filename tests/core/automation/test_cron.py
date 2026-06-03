"""Tests for cron scheduling and persistence in CronService."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import core.automation.cron as cron_module
from core.automation.cron import (
    CronJobNotFoundError,
    CronJobValidationError,
    CronService,
    CronStorageError,
)


def make_service(tmp_path: Path) -> tuple[CronService, SimpleNamespace]:
    trigger_service = SimpleNamespace(trigger_run=AsyncMock())
    service = CronService(cast(Any, trigger_service), tmp_path)
    return service, trigger_service


def test_cron_service_crud_operations(tmp_path: Path) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)
    run_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    # Act
    created = service.create_job(
        agent_id="agent-one",
        prompt="Run once",
        schedule_type="once",
        run_at=run_at,
    )
    listed = service.list_jobs()
    loaded = service.get_job(created.id)
    updated = service.update_job(created.id, prompt="Run once updated")
    paused = service.disable_job(created.id)
    enabled = service.enable_job(created.id)
    service.delete_job(created.id)

    # Assert
    assert [job.id for job in listed] == [created.id]
    assert loaded.prompt == "Run once"
    assert updated.prompt == "Run once updated"
    assert paused.status == "paused"
    assert enabled.status == "active"
    assert service.list_jobs() == []
    with pytest.raises(CronJobNotFoundError, match=created.id):
        service.get_job(created.id)


def test_jobs_json_is_created_on_demand(tmp_path: Path) -> None:
    # Arrange
    jobs_path = tmp_path / "cron" / "jobs.json"
    service, _trigger_service = make_service(tmp_path)
    assert not jobs_path.exists()

    # Act
    jobs = service.list_jobs()

    # Assert
    assert jobs == []
    assert jobs_path.exists()
    assert json.loads(jobs_path.read_text(encoding="utf-8")) == []


def test_jobs_json_schema_is_validated_on_read(tmp_path: Path) -> None:
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(
        json.dumps([{"id": "job-one", "schedule_type": "daily"}]), encoding="utf-8"
    )
    service, _trigger_service = make_service(tmp_path)

    with pytest.raises(CronStorageError, match=r"\$\[0\]\.schedule_type: must be one of"):
        service.list_jobs()


def test_utc_timezone_is_accepted_when_zoneinfo_database_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)

    def missing_zoneinfo(_timezone_name: str) -> Any:
        raise cron_module.ZoneInfoNotFoundError("timezone data unavailable")

    monkeypatch.setattr(cron_module, "ZoneInfo", missing_zoneinfo)

    # Act
    created = service.create_job(
        agent_id="agent-one",
        prompt="Cron job",
        schedule_type="cron",
        cron_expression="* * * * *",
        timezone="UTC",
    )

    # Assert
    assert created.timezone == "UTC"


def test_non_utc_timezone_still_fails_when_zoneinfo_database_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)

    def missing_zoneinfo(_timezone_name: str) -> Any:
        raise cron_module.ZoneInfoNotFoundError("timezone data unavailable")

    monkeypatch.setattr(cron_module, "ZoneInfo", missing_zoneinfo)

    # Act / Assert
    with pytest.raises(CronJobValidationError, match="Unknown timezone: Europe/Paris"):
        service.create_job(
            agent_id="agent-one",
            prompt="Cron job",
            schedule_type="cron",
            cron_expression="* * * * *",
            timezone="Europe/Paris",
        )


@pytest.mark.asyncio
async def test_start_creates_active_tasks_and_completes_missed_once_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    missed = service.create_job(
        agent_id="agent-one",
        prompt="Missed once",
        schedule_type="once",
        run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    active_cron = service.create_job(
        agent_id="agent-two",
        prompt="Cron active",
        schedule_type="cron",
        cron_expression="* * * * *",
        timezone="UTC",
    )

    async def hold_cron_task(_job: cron_module.CronJob) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(service, "_run_cron_job", hold_cron_task)

    # Act
    service.start()
    await asyncio.sleep(0)

    # Assert
    assert active_cron.id in service._job_tasks
    assert missed.id not in service._job_tasks
    assert service.get_job(missed.id).status == "completed"
    trigger_service.trigger_run.assert_not_called()

    service.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cron_service_aclose_awaits_cancelled_job_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Cron active",
        schedule_type="cron",
        cron_expression="* * * * *",
        timezone="UTC",
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hold_cron_task(_job: cron_module.CronJob) -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(service, "_run_cron_job", hold_cron_task)

    service.start()
    await asyncio.wait_for(started.wait(), timeout=1)

    await service.aclose()

    assert cancelled.is_set()
    assert service._job_tasks == {}
    assert service._started is False
    assert service.get_job(job.id).status == "active"


@pytest.mark.asyncio
async def test_run_once_job_fires_and_marks_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    monkeypatch.setattr(cron_module.asyncio, "sleep", AsyncMock())

    # Act
    await service._run_once_job(job)

    # Assert
    trigger_service.trigger_run.assert_awaited_once_with("agent-one", "Once prompt", None)
    updated = service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    assert updated.last_fired_at.endswith("+00:00")
    assert not service._once_fire_claim_path(job.id).exists()


@pytest.mark.asyncio
async def test_run_once_job_retries_trigger_failure_without_completing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    sleep_delays: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        sleep_delays.append(delay_seconds)

    monkeypatch.setattr(cron_module.asyncio, "sleep", record_sleep)
    trigger_service.trigger_run.side_effect = [RuntimeError("boom"), None]

    # Act
    await service._run_once_job(job)

    # Assert
    assert trigger_service.trigger_run.await_count == 2
    assert sleep_delays[1] == cron_module._ONCE_RETRY_DELAY_SECONDS
    updated = service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    assert updated.last_fired_at.endswith("+00:00")
    assert not service._once_fire_claim_path(job.id).exists()


@pytest.mark.asyncio
async def test_run_once_job_retries_completed_save_without_refiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    monkeypatch.setattr(cron_module.asyncio, "sleep", AsyncMock())
    save_attempts = 0

    original_save_jobs = service._save_jobs

    def fail_first_save_after_fire() -> None:
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise CronStorageError("disk full")
        original_save_jobs()

    monkeypatch.setattr(service, "_save_jobs", fail_first_save_after_fire)

    # Act
    await service._run_once_job(job)

    # Assert
    trigger_service.trigger_run.assert_awaited_once_with("agent-one", "Once prompt", None)
    assert save_attempts == 2
    updated = service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    assert not service._once_fire_claim_path(job.id).exists()


def test_start_completes_claimed_once_job_without_refiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    service._write_once_fire_claim(job, datetime.now(UTC).isoformat())

    restarted_service, restarted_trigger_service = make_service(tmp_path)

    def fail_if_once_task_starts(_job: cron_module.CronJob) -> None:
        raise AssertionError("claimed once job should not start")

    monkeypatch.setattr(restarted_service, "_start_job_task", fail_if_once_task_starts)

    # Act
    restarted_service.start()

    # Assert
    restarted_trigger_service.trigger_run.assert_not_called()
    updated = restarted_service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    persisted_jobs = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert persisted_jobs[0]["status"] == "completed"
    assert not restarted_service._once_fire_claim_path(job.id).exists()


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
        timezone="UTC",
    )

    class ImmediateCronIter:
        @staticmethod
        def is_valid(_expression: str) -> bool:
            return True

        def __init__(self, _expression: str, base_time: datetime) -> None:
            self._next_fire = base_time

        def get_next(self, _return_type: Any) -> datetime:
            return self._next_fire

    monkeypatch.setattr(cron_module, "croniter", ImmediateCronIter)

    async def trigger_and_pause(
        _agent_id: str, _prompt: str, _session_id: str | None = None
    ) -> None:
        service._jobs[job.id].status = "paused"

    trigger_service.trigger_run.side_effect = trigger_and_pause

    # Act
    await service._run_cron_job(job)

    # Assert
    trigger_service.trigger_run.assert_awaited_once_with("agent-one", "Cron prompt", None)
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
        timezone="UTC",
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
        _agent_id: str, _prompt: str, _session_id: str | None = None
    ) -> None:
        if trigger_service.trigger_run.await_count == 1:
            raise RuntimeError("boom")
        service._jobs[job.id].status = "paused"

    monkeypatch.setattr(cron_module, "croniter", ImmediateCronIter)
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
        timezone="UTC",
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
