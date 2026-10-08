"""Tests for cron execution."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import core.automation.cron as cron_module
from core.automation import _cron_claims as cron_claims
from core.automation import _cron_timing as cron_timing
from core.automation.cron import (
    CronJobInPastError,
    CronStorageError,
)
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.runs import RunKind
from core.sessions import SessionNotFoundError
from tests.core.automation.cron_test_support import (
    make_service,
)

_ASYNC_COORDINATION_TIMEOUT_SECONDS = 10.0


@pytest.mark.asyncio
async def test_start_creates_tasks_for_active_jobs_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    paused = await service.create_job(
        agent_id="agent-one",
        prompt="Paused cron",
        schedule_type="cron",
        cron_expression="* * * * *",
        status="paused",
    )
    active_cron = await service.create_job(
        agent_id="agent-two",
        prompt="Cron active",
        schedule_type="cron",
        cron_expression="* * * * *",
    )

    async def hold_cron_task(_job: cron_module.CronJob, _schedule_type: str) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(service, "_run_recurring_job", hold_cron_task)

    # Act
    service.start()
    await asyncio.sleep(0)

    # Assert
    assert active_cron.id in service._job_tasks
    assert paused.id not in service._job_tasks
    trigger_service.trigger_run.assert_not_called()

    service.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cron_service_aclose_awaits_cancelled_job_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Cron active",
        schedule_type="cron",
        cron_expression="* * * * *",
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hold_cron_task(_job: cron_module.CronJob, _schedule_type: str) -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(service, "_run_recurring_job", hold_cron_task)

    service.start()
    await asyncio.wait_for(started.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)

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
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Cron active",
        schedule_type="cron",
        cron_expression="* * * * *",
    )

    attempts = 0
    restarted = asyncio.Event()

    async def fail_scheduler_task(_job: object, _schedule_type: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("scheduler invariant failed")
        restarted.set()
        await asyncio.Future()

    monkeypatch.setattr(service, "_run_recurring_job", fail_scheduler_task)

    with caplog.at_level(logging.ERROR, logger="vbot.automation.cron"):
        service.start()
        await asyncio.wait_for(restarted.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)

    recovered = service.get_job(job.id)
    persisted = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
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
async def test_run_once_job_fires_in_its_project_and_marks_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        project_id="vbot",
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
        project_id="vbot",
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
    job = await service.create_job(
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
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )

    def broken_write(_jobs: list[object]) -> None:
        raise CronStorageError("disk full")

    monkeypatch.setattr(service, "_write_jobs", broken_write)

    with caplog.at_level(logging.ERROR, logger="vbot.automation.cron"):
        succeeded = await asyncio.wait_for(service._trigger_job_run(job), timeout=5)

    assert succeeded is True
    assert service.get_job(job.id).last_outcome == "success"
    give_up_records = [
        record for record in caplog.records if "could not be persisted" in record.getMessage()
    ]
    assert len(give_up_records) == 1


def _admitted_run_fails(trigger_service: SimpleNamespace) -> None:
    run = SimpleNamespace(id="run-one", wait=AsyncMock(side_effect=RuntimeError("boom")))
    trigger_service.trigger_run.return_value = run


def _pinned_session_missing(trigger_service: SimpleNamespace) -> None:
    trigger_service.trigger_run.side_effect = SessionNotFoundError(
        "session does not exist: ses_gone"
    )


def _target_agent_missing(trigger_service: SimpleNamespace) -> None:
    trigger_service.trigger_run.side_effect = ResolutionAgentNotFoundError("agent-one")


def _target_project_missing(trigger_service: SimpleNamespace) -> None:
    trigger_service.trigger_run.side_effect = ResolutionProjectNotFoundError("vbot")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "error_names", "remaining_runs"),
    [
        # The Run is admitted and then fails - execution failures count.
        pytest.param(_admitted_run_fails, "boom", 3, id="admitted-run-fails"),
        # The pinned Session was deleted or moved away: no Run can start until the
        # job is edited, so the fire counts although nothing was admitted.
        pytest.param(
            _pinned_session_missing,
            "Session does not exist for cron target agent-one@vbot: ses_gone",
            5,
            id="pinned-session-missing",
        ),
        # The target Agent or Project was removed: the same lasting condition.
        pytest.param(
            _target_agent_missing,
            "Cron target does not exist: agent-one@vbot",
            5,
            id="target-agent-missing",
        ),
        pytest.param(
            _target_project_missing,
            "Cron target does not exist: agent-one@vbot",
            5,
            id="target-project-missing",
        ),
    ],
)
async def test_recurring_job_stops_after_consecutive_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    arrange: Any,
    error_names: str,
    remaining_runs: int,
) -> None:
    service, trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "MAX_CONSECUTIVE_CRON_FAILURES", 2)
    arrange(trigger_service)
    job = await service.create_job(
        agent_id="agent-one",
        project_id="vbot",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        session_id="ses_gone",
        remaining_runs=5,
    )

    with caplog.at_level(logging.WARNING, logger="vbot.automation.cron"):
        assert await service._trigger_job_run(job) is False
        assert service.get_job(job.id).status == "active"
        assert await service._trigger_job_run(job) is False

    updated = service.get_job(job.id)
    assert updated.status == "failed"
    assert updated.last_outcome == "failed"
    assert updated.last_error is not None and error_names in updated.last_error
    assert updated.consecutive_failures == 2
    # Only admitted Runs consume a finite run.
    assert updated.remaining_runs == remaining_runs
    # The stop is a health transition an operator must see: one WARNING, not INFO.
    stopped = [
        record
        for record in caplog.records
        if record.name == "vbot.automation.cron"
        and record.levelno == logging.WARNING
        and job.id in record.getMessage()
        and "status=failed" in record.getMessage()
    ]
    assert len(stopped) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "last_error"),
    [
        pytest.param(RuntimeError("queue limit reached"), "queue limit reached", id="capacity"),
        # A target that exists but cannot run can recover without editing the job.
        pytest.param(
            AgentResolutionError("no usable Model"),
            "Cron target agent-one cannot run: no usable Model",
            id="target-cannot-run",
        ),
    ],
)
async def test_pre_admission_trigger_failures_neither_stop_nor_consume_a_recurring_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, last_error: str
) -> None:
    """A fire that never admitted a Run is not a job execution failure.

    Regression: trigger errors (full Queue, shutdown window) used to advance
    ``consecutive_failures``, so five capacity rejections silently killed a
    recurring job that never ran at all. The error must stay visible without
    burning the fatal budget.
    """
    service, trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "MAX_CONSECUTIVE_CRON_FAILURES", 2)
    trigger_service.trigger_run.side_effect = error
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        remaining_runs=2,
    )

    for _ in range(5):
        assert await service._trigger_job_run(job) is False

        updated = service.get_job(job.id)
        assert updated.status == "active"
        assert updated.remaining_runs == 2
        assert updated.consecutive_failures == 0
        assert updated.last_outcome == "failed"
        assert updated.last_error == last_error


@pytest.mark.asyncio
async def test_run_once_job_retries_trigger_failure_without_completing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    )
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", AsyncMock(return_value=True))
    sleep_delays: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        sleep_delays.append(delay_seconds)

    monkeypatch.setattr(cron_timing, "_sleep", record_sleep)
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
    job = await service.create_job(
        agent_id="agent-gone",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    )
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", AsyncMock(return_value=True))
    sleep_delays: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        sleep_delays.append(delay_seconds)

    monkeypatch.setattr(cron_timing, "_sleep", record_sleep)
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


@pytest.mark.asyncio
async def test_failed_once_job_can_be_re_enabled(tmp_path: Path) -> None:
    # Arrange: a once job abandoned as failed (distinct from a completed fire).
    service, _trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    await service._abandon_once_job(job.id, cron_module._ONCE_MAX_FIRE_ATTEMPTS)
    assert service.get_job(job.id).status == "failed"

    # Act: unlike a completed job, a failed job can be re-enabled to retry.
    re_enabled = await service.enable_job(job.id)

    # Assert
    assert re_enabled.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "failed"])
async def test_once_job_with_elapsed_time_is_not_rearmed(
    tmp_path: Path, status: cron_module.CronJobStatus
) -> None:
    service, trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        status="paused",
    )
    service._jobs[job.id].status = status
    service._jobs[job.id].run_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    with pytest.raises(CronJobInPastError):
        await service.enable_job(job.id)
    assert service.get_job(job.id).status == status
    # Choosing a future time together with the status change arms it normally.
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert (await service.update_job(job.id, status="active", run_at=future)).status == "active"
    trigger_service.trigger_run.assert_not_called()


@pytest.mark.asyncio
async def test_once_job_cannot_be_created_or_moved_into_the_past(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path, tz="Europe/Berlin")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    with pytest.raises(CronJobInPastError):
        await service.create_job(
            agent_id="agent-one", prompt="p", schedule_type="once", run_at=past
        )
    assert service.list_jobs() == []
    job = await service.create_job(
        agent_id="agent-one",
        prompt="p",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )
    with pytest.raises(CronJobInPastError):
        await service.update_job(
            job.id, schedule_type="once", run_at=past, cron_expression=None, remaining_runs=1
        )
    assert service.get_job(job.id).schedule_type == "cron"


@pytest.mark.asyncio
async def test_run_once_job_retries_completed_save_without_refiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", AsyncMock())
    monkeypatch.setattr(cron_timing, "_sleep", AsyncMock())
    save_attempts = 0

    original_write_jobs = service._write_jobs

    def fail_first_save_after_fire(jobs: list[object]) -> None:
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise CronStorageError("disk full")
        original_write_jobs(jobs)

    monkeypatch.setattr(service, "_write_jobs", fail_first_save_after_fire)

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


@pytest.mark.asyncio
async def test_start_completes_claimed_once_job_without_refiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)
    job = await service.create_job(
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
    persisted = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert persisted["jobs"][0]["status"] == "completed"
    assert not cron_claims.path_for(restarted_service._once_fire_claims_dir, job.id).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_bytes", [b"{", b"\xff"])
async def test_start_holds_only_the_once_job_with_an_unreadable_fire_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    claim_bytes: bytes,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    recurring = await service.create_job(
        agent_id="agent-one",
        prompt="Recurring prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )
    once = await service.create_job(
        agent_id="agent-one",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    jobs_path = tmp_path / "cron" / "jobs.json"
    persisted = json.loads(jobs_path.read_text(encoding="utf-8"))
    for item in persisted["jobs"]:
        if item["id"] == once.id:  # Its time passed while vBot was offline.
            item["run_at"] = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    jobs_path.write_text(json.dumps(persisted), encoding="utf-8")
    claim_path = cron_claims.path_for(service._once_fire_claims_dir, once.id)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_bytes(claim_bytes)
    restarted_service, restarted_trigger_service = make_service(tmp_path)
    start_job_task = Mock()
    monkeypatch.setattr(restarted_service, "_start_job_task", start_job_task)

    with caplog.at_level(logging.WARNING, logger="vbot.automation.cron"):
        restarted_service.start()

    # The possibly fired once job is held, never fired and never marked missed.
    assert [call.args[0].id for call in start_job_task.call_args_list] == [recurring.id]
    assert restarted_service.get_job(once.id).status == "active"
    assert any(once.id in record.getMessage() for record in caplog.records)
    assert claim_path.read_bytes() == claim_bytes
    restarted_trigger_service.trigger_run.assert_not_called()
    # Storage stays writable; one bad claim file does not disable Cron.
    assert (await restarted_service.update_job(recurring.id, prompt="Still editable")).prompt == (
        "Still editable"
    )


@pytest.mark.asyncio
async def test_start_keeps_cron_available_when_reconciliation_save_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    recurring = await service.create_job(
        agent_id="agent-one",
        prompt="Recurring prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )
    claimed = await service.create_job(
        agent_id="agent-one",
        prompt="Claimed once",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    cron_claims.write(service._once_fire_claims_dir, claimed, datetime.now(UTC).isoformat())
    restarted_service, restarted_trigger_service = make_service(tmp_path)
    start_job_task = Mock()
    monkeypatch.setattr(restarted_service, "_start_job_task", start_job_task)
    real_save = restarted_service._save_jobs
    monkeypatch.setattr(
        restarted_service, "_save_jobs", Mock(side_effect=CronStorageError("disk busy"))
    )

    restarted_service.start()

    assert [call.args[0].id for call in start_job_task.call_args_list] == [recurring.id]
    assert restarted_service.get_job(claimed.id).status == "completed"
    # The claim survives until the reconciled state is durable.
    assert cron_claims.path_for(restarted_service._once_fire_claims_dir, claimed.id).exists()
    restarted_trigger_service.trigger_run.assert_not_called()
    monkeypatch.setattr(restarted_service, "_save_jobs", real_save)
    await restarted_service.update_job(recurring.id, prompt="Saved later")
    persisted = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert {job["id"]: job["status"] for job in persisted}[claimed.id] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
async def test_removed_queued_fire_settles_without_killing_scheduler(
    tmp_path, monkeypatch, schedule_type
):
    from core.runs import RunCancelledError

    service, trigger = make_service(tmp_path)
    kwargs = (
        {"run_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
        if schedule_type == "once"
        else {"cron_expression": "* * * * *"}
    )
    job = await service.create_job(
        agent_id="agent", prompt="work", schedule_type=schedule_type, **kwargs
    )
    trigger.trigger_run.side_effect = RunCancelledError("removed")
    if schedule_type == "once":
        monkeypatch.setattr(cron_timing, "_sleep_until_utc", AsyncMock(return_value=True))
        await service._run_once_job(job)
        assert service.get_job(job.id).status == "failed"
        assert not list(service._once_fire_claims_dir.glob("*.json"))
    else:
        assert await service._trigger_job_run(job) is False
        assert service.get_job(job.id).status == "active"
        assert await service._trigger_job_run(job) is False
        assert trigger.trigger_run.await_count == 2


@pytest.mark.asyncio
async def test_rescheduling_keeps_live_run_waiter_and_global_slot(tmp_path, monkeypatch):
    service, trigger = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent", prompt="work", schedule_type="cron", cron_expression="* * * * *"
    )
    running = asyncio.Event()
    release = asyncio.Event()

    async def wait():
        running.set()
        await release.wait()

    trigger.trigger_run.return_value = SimpleNamespace(id="run", wait=wait)
    sleeps = 0
    resumed = asyncio.Event()

    async def sleep_until(*args, **kwargs):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            resumed.set()
            await asyncio.Future()
        return True

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", sleep_until)
    service.start()
    try:
        await asyncio.wait_for(running.wait(), _ASYNC_COORDINATION_TIMEOUT_SECONDS)
        task = service._job_tasks[job.id]
        available = service._run_slots._value
        await service.update_job(job.id, cron_expression="*/2 * * * *")
        await asyncio.sleep(0)
        assert service._job_tasks[job.id] is task
        assert not task.cancelling()
        assert service._run_slots._value == available
        release.set()
        await asyncio.wait_for(resumed.wait(), _ASYNC_COORDINATION_TIMEOUT_SECONDS)
        assert service._run_slots._value == available + 1
        assert trigger.trigger_run.await_count == 1
    finally:
        await service.aclose()


class _BlockedJobWrites:
    """Holds every jobs.json write on the writer thread until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        write = cron_module.write_json_document

        def blocked_write(*args: Any, **kwargs: Any) -> None:
            self.entered.set()
            self.release.wait(timeout=5)
            write(*args, **kwargs)

        monkeypatch.setattr(cron_module, "write_json_document", blocked_write)


@pytest.mark.asyncio
async def test_fire_saves_off_the_loop_and_an_edit_lands_after_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trigger_service = make_service(tmp_path)
    trigger_service.trigger_run.return_value = SimpleNamespace(
        id="run-one", wait=AsyncMock(return_value=None)
    )
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )
    writes = _BlockedJobWrites(monkeypatch)

    firing = asyncio.create_task(service._trigger_job_run(job))
    try:
        assert await asyncio.to_thread(writes.entered.wait, 5)
        assert not firing.done()
        # The edit applies at once; its save queues behind the fire's older snapshot
        # without holding the Event Loop.
        editing = asyncio.create_task(service.update_job(job.id, name="Renamed"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert service.get_job(job.id).name == "Renamed"
        assert not editing.done()
    finally:
        writes.release.set()

    await asyncio.wait_for(editing, timeout=5)
    assert await asyncio.wait_for(firing, timeout=5) is True
    stored = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert [(item["name"], item["last_outcome"]) for item in stored["jobs"]] == [
        ("Renamed", "success")
    ]


@pytest.mark.asyncio
async def test_a_cancelled_edit_still_finishes_its_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )
    writes = _BlockedJobWrites(monkeypatch)
    changes: list[str] = []
    service.add_changed_callback(lambda: changes.append("cron"))

    editing = asyncio.create_task(service.update_job(job.id, name="Renamed"))
    try:
        assert await asyncio.to_thread(writes.entered.wait, 5)
        editing.cancel()
        await asyncio.sleep(0)
        assert not editing.done()
    finally:
        writes.release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(editing, timeout=5)
    stored = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in stored["jobs"]] == ["Renamed"]
    assert service.get_job(job.id).name == "Renamed"
    assert changes == ["cron"]


@pytest.mark.asyncio
async def test_job_paused_during_the_fire_save_does_not_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trigger_service = make_service(tmp_path)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Health check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )
    writes = _BlockedJobWrites(monkeypatch)

    firing = asyncio.create_task(service._trigger_job_run(job))
    try:
        assert await asyncio.to_thread(writes.entered.wait, 5)
        editing = asyncio.create_task(service.update_job(job.id, status="paused"))
        await asyncio.sleep(0)
        assert not editing.done()
    finally:
        writes.release.set()

    await asyncio.wait_for(editing, timeout=5)
    assert await asyncio.wait_for(firing, timeout=5) is False
    trigger_service.trigger_run.assert_not_awaited()
    assert service.get_job(job.id).status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "change"),
    [
        ("claim", "reschedule"),
        ("claim", "pause"),
        ("claim", "shutdown"),
        ("fire", "reschedule"),
        ("fire", "pause"),
    ],
)
async def test_unadmitted_once_claim_is_withdrawn_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, change: str
) -> None:
    now = datetime(2030, 10, 5, 10, tzinfo=UTC)
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: now)
    service, trigger = make_service(tmp_path, tz="UTC")
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Work at the selected time",
        schedule_type="once",
        run_at=(now + timedelta(minutes=1)).isoformat(),
    )
    sleeps = 0
    allow_replacement = asyncio.Event()

    async def reach_first_fire(target: datetime, **kwargs: Any) -> bool:
        nonlocal sleeps, now
        sleeps += 1
        if sleeps > 1:
            await allow_replacement.wait()
        now = target
        return True

    entered = threading.Event()
    release = threading.Event()
    write = cron_claims.write if phase == "claim" else service._write_jobs

    def hold_written_state(*args: Any, **kwargs: Any) -> None:
        write(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", reach_first_fire)
    if phase == "claim":
        monkeypatch.setattr(cron_claims, "write", hold_written_state)
    else:
        monkeypatch.setattr(service, "_write_jobs", hold_written_state)
    service.start()
    expected_run_at = job.run_at
    try:
        assert await asyncio.to_thread(entered.wait, _ASYNC_COORDINATION_TIMEOUT_SECONDS)
        firing = service._job_tasks[job.id]
        # An edit reaches the job task at once; its save queues behind the claim or fire save.
        release.set()
        if change == "reschedule":
            updated = await service.update_job(job.id, run_at=(now + timedelta(days=1)).isoformat())
            expected_run_at = updated.run_at
        elif change == "pause":
            await service.disable_job(job.id)
        if change != "shutdown":
            await asyncio.wait_for(firing, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
        await asyncio.wait_for(service.aclose(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
        trigger.trigger_run.assert_not_awaited()
        assert not cron_claims.path_for(service._once_fire_claims_dir, job.id).exists()
    finally:
        release.set()
        await service.aclose()

    restarted, restarted_trigger = make_service(tmp_path, tz="UTC")
    restarted.start()
    try:
        restored = restarted.get_job(job.id)
        assert restored.status == ("paused" if change == "pause" else "active")
        assert restored.run_at == expected_run_at
        assert restored.last_outcome is None
        restarted_trigger.trigger_run.assert_not_awaited()
        if change == "reschedule":
            # The withdrawn fire must leave the replacement armed, including after restart.
            restarted_trigger.trigger_run.return_value = SimpleNamespace(
                id="replacement-run", wait=AsyncMock()
            )
            replacement = restarted._job_tasks[job.id]
            allow_replacement.set()
            await asyncio.wait_for(replacement, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
            restarted_trigger.trigger_run.assert_awaited_once()
            completed = restarted.get_job(job.id)
            assert completed.status == "completed"
            assert completed.last_fired_at == expected_run_at
    finally:
        await restarted.aclose()
