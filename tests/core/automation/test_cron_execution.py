"""Tests for cron execution."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.automation.cron as cron_module
from core.automation import _cron_claims as cron_claims
from core.automation import _cron_timing as cron_timing
from core.automation.cron import (
    CronStorageError,
)
from core.runs import RunKind
from tests.core.automation.cron_test_support import (
    make_service,
)


@pytest.mark.asyncio
async def test_start_creates_active_tasks_and_records_missed_once_jobs(
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
    assert service.get_job(missed.id).status == "missed"
    assert service.get_job(missed.id).last_outcome == "missed"
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
async def test_unexpected_scheduler_task_failure_restarts_active_recurring_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Cron active",
        schedule_type="cron",
        cron_expression="* * * * *",
    )

    attempts = 0
    restarted = asyncio.Event()

    async def fail_scheduler_task(_job: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("scheduler invariant failed")
        restarted.set()
        await asyncio.Future()

    monkeypatch.setattr(service, "_run_cron_job", fail_scheduler_task)

    with caplog.at_level(logging.ERROR, logger="vbot.automation.cron"):
        service.start()
        await asyncio.wait_for(restarted.wait(), timeout=1)

    recovered = service.get_job(job.id)
    persisted = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert recovered.status == "active"
    assert recovered.last_outcome == "failed"
    assert recovered.last_error == "scheduler invariant failed"
    assert recovered.consecutive_failures == 1
    assert attempts == 2
    assert job.id in service._job_tasks
    assert not service._job_tasks[job.id].done()
    assert persisted[0]["status"] == "active"
    assert any("Cron job task failed" in record.getMessage() for record in caplog.records)

    await service.aclose()


@pytest.mark.asyncio
async def test_run_once_job_fires_and_marks_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", AsyncMock())

    # Act
    with caplog.at_level(logging.INFO, logger="vbot.automation.cron"):
        await service._run_once_job(job)

    # Assert
    trigger_service.trigger_run.assert_awaited_once_with(
        "agent-one",
        "Once prompt",
        None,
        project_id=None,
        run_kind=RunKind.CRON,
        contributes_to_agent_activity=False,
    )
    fired_line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Cron job fired")
    )
    assert f"job={job.id}" in fired_line
    assert "agent=agent-one" in fired_line
    updated = service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    assert updated.last_fired_at.endswith("+00:00")
    assert not cron_claims.path_for(service._once_fire_claims_dir, job.id).exists()


@pytest.mark.asyncio
async def test_trigger_waits_for_run_and_records_execution_health(tmp_path: Path) -> None:
    service, trigger_service = make_service(tmp_path)
    run = SimpleNamespace(id="run-one", wait=AsyncMock(return_value=None))
    trigger_service.trigger_run.return_value = run
    job = service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )

    succeeded = await service._trigger_job_run(job)

    assert succeeded is True
    run.wait.assert_awaited_once_with()
    updated = service.get_job(job.id)
    assert updated.last_attempt_at is not None
    assert updated.last_fired_at is not None
    assert updated.last_completed_at is not None
    assert updated.last_run_id == "run-one"
    assert updated.last_outcome == "success"
    assert updated.last_error is None
    assert updated.consecutive_failures == 0


@pytest.mark.asyncio
async def test_persistent_save_failure_does_not_hang_the_firing_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stuck jobs file must not spin the fire task forever.

    Regression: the post-fire save used to retry unbounded, so an unwritable
    jobs file hung the job task between the fire and the Run wait, leaving the
    fired state unpersisted for as long as the storage fault lasted.
    """
    service, trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "_POST_FIRE_SAVE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cron_module, "_POST_FIRE_SAVE_RETRY_SECONDS", 0.0)
    run = SimpleNamespace(id="run-one", wait=AsyncMock(return_value=None))
    trigger_service.trigger_run.return_value = run
    job = service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )

    def broken_save() -> None:
        raise CronStorageError("disk full")

    monkeypatch.setattr(service, "_save_jobs", broken_save)

    with caplog.at_level(logging.ERROR, logger="vbot.automation.cron"):
        succeeded = await asyncio.wait_for(service._trigger_job_run(job), timeout=5)

    assert succeeded is True
    assert service.get_job(job.id).last_outcome == "success"
    give_up_records = [
        record for record in caplog.records if "could not be persisted" in record.getMessage()
    ]
    assert len(give_up_records) == 1


@pytest.mark.asyncio
async def test_recurring_job_stops_after_consecutive_run_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "MAX_CONSECUTIVE_CRON_FAILURES", 2)
    # The Run is admitted and then fails - execution failures count.
    run = SimpleNamespace(id="run-one", wait=AsyncMock(side_effect=RuntimeError("boom")))
    trigger_service.trigger_run.return_value = run
    job = service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )

    assert await service._trigger_job_run(job) is False
    assert service.get_job(job.id).status == "active"
    assert await service._trigger_job_run(job) is False

    updated = service.get_job(job.id)
    assert updated.status == "failed"
    assert updated.last_outcome == "failed"
    assert updated.last_error == "boom"
    assert updated.consecutive_failures == 2


@pytest.mark.asyncio
async def test_pre_admission_trigger_failures_do_not_stop_a_recurring_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fire that never admitted a Run is not a job execution failure.

    Regression: trigger errors (full Queue, shutdown window) used to advance
    ``consecutive_failures``, so five capacity rejections silently killed a
    recurring job that never ran at all. The error must stay visible without
    burning the fatal budget.
    """
    service, trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "MAX_CONSECUTIVE_CRON_FAILURES", 2)
    trigger_service.trigger_run.side_effect = RuntimeError("queue limit reached")
    job = service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )

    for _ in range(5):
        assert await service._trigger_job_run(job) is False

        updated = service.get_job(job.id)
        assert updated.status == "active"
        assert updated.consecutive_failures == 0
        assert updated.last_outcome == "failed"
        assert updated.last_error == "queue limit reached"


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
    assert sleep_delays == [cron_timing._ONCE_RETRY_DELAY_SECONDS]
    updated = service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    assert updated.last_fired_at.endswith("+00:00")
    assert not cron_claims.path_for(service._once_fire_claims_dir, job.id).exists()


@pytest.mark.asyncio
async def test_run_once_job_abandons_after_attempt_limit_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-gone",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    sleep_delays: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        sleep_delays.append(delay_seconds)

    monkeypatch.setattr(cron_module.asyncio, "sleep", record_sleep)
    trigger_service.trigger_run.side_effect = RuntimeError("agent deleted")

    # Act
    await service._run_once_job(job)

    # Assert: a permanently failing once job stops after the attempt cap instead
    # of looping forever, backing off exponentially between attempts.
    assert trigger_service.trigger_run.await_count == cron_module._ONCE_MAX_FIRE_ATTEMPTS
    backoff_delays = [delay for delay in sleep_delays if delay > 0]
    expected_backoff = [
        cron_timing._once_retry_delay(attempt)
        for attempt in range(1, cron_module._ONCE_MAX_FIRE_ATTEMPTS)
    ]
    assert backoff_delays == expected_backoff
    updated = service.get_job(job.id)
    assert updated.status == "failed"
    assert updated.last_fired_at is None
    assert not cron_claims.path_for(service._once_fire_claims_dir, job.id).exists()


def test_failed_once_job_can_be_re_enabled(tmp_path: Path) -> None:
    # Arrange: a once job abandoned as failed (distinct from a completed fire).
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    service._abandon_once_job(job.id, cron_module._ONCE_MAX_FIRE_ATTEMPTS)
    assert service.get_job(job.id).status == "failed"

    # Act: unlike a completed job, a failed job can be re-enabled to retry.
    re_enabled = service.enable_job(job.id)

    # Assert
    assert re_enabled.status == "active"


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
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", AsyncMock())
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
    trigger_service.trigger_run.assert_awaited_once_with(
        "agent-one",
        "Once prompt",
        None,
        project_id=None,
        run_kind=RunKind.CRON,
        contributes_to_agent_activity=False,
    )
    assert save_attempts == 4
    updated = service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    assert not cron_claims.path_for(service._once_fire_claims_dir, job.id).exists()


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
    cron_claims.write(service._once_fire_claims_dir, job, datetime.now(UTC).isoformat())

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
    assert not cron_claims.path_for(restarted_service._once_fire_claims_dir, job.id).exists()


def test_start_degrades_cron_when_once_fire_claim_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    recurring = service.create_job(
        agent_id="agent-one",
        prompt="Recurring prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )
    once = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    claim_path = cron_claims.path_for(service._once_fire_claims_dir, once.id)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_text("{", encoding="utf-8")
    restarted_service, _restarted_trigger_service = make_service(tmp_path)
    start_job_task = Mock()
    monkeypatch.setattr(restarted_service, "_start_job_task", start_job_task)

    with caplog.at_level(logging.ERROR, logger="vbot.automation.cron"):
        restarted_service.start()

    assert restarted_service.list_jobs() == []
    with pytest.raises(CronStorageError):
        restarted_service.update_job(recurring.id, prompt="Must not overwrite")
    start_job_task.assert_not_called()
    assert caplog.records
    assert claim_path.read_text(encoding="utf-8") == "{"


def test_start_degrades_cron_when_once_fire_claim_is_not_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    once = service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    claim_path = cron_claims.path_for(service._once_fire_claims_dir, once.id)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_bytes(b"\xff")
    restarted_service, _restarted_trigger_service = make_service(tmp_path)
    start_job_task = Mock()
    monkeypatch.setattr(restarted_service, "_start_job_task", start_job_task)

    with caplog.at_level(logging.ERROR, logger="vbot.automation.cron"):
        restarted_service.start()

    assert restarted_service.list_jobs() == []
    start_job_task.assert_not_called()
    assert caplog.records
    assert claim_path.read_bytes() == b"\xff"
