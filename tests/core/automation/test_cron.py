"""Tests for cron scheduling and persistence in CronService."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import core.automation.cron as cron_module
from core.automation.cron import (
    CronJobNotFoundError,
    CronJobStatus,
    CronJobValidationError,
    CronService,
    CronStorageError,
)
from core.runs import RunKind
from core.sessions import SessionAddress


def make_service(
    tmp_path: Path,
    *,
    agent_resolver: Any = None,
    sessions: Any = None,
    tz: str | ZoneInfo | None = None,
) -> tuple[CronService, SimpleNamespace]:
    trigger_service = SimpleNamespace(trigger_run=AsyncMock())
    service = CronService(
        cast(Any, trigger_service),
        tmp_path,
        agent_resolver=agent_resolver,
        sessions=sessions,
        tz=tz,
    )
    return service, trigger_service


def test_cron_service_crud_operations(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)
    run_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    # Act
    with caplog.at_level(logging.INFO, logger="vbot.automation.cron"):
        created = service.create_job(
            agent_id="agent-one",
            name="Private status check",
            prompt="private cron prompt",
            schedule_type="once",
            run_at=run_at,
        )
        listed = service.list_jobs()
        loaded = service.get_job(created.id)
        updated = service.update_job(
            created.id,
            name="Updated status check",
            prompt="private updated prompt",
        )
        paused = service.disable_job(created.id)
        enabled = service.enable_job(created.id)
        service.delete_job(created.id)

    # Assert
    assert [job.id for job in listed] == [created.id]
    assert loaded.name == "Private status check"
    assert loaded.prompt == "private cron prompt"
    assert updated.name == "Updated status check"
    assert updated.prompt == "private updated prompt"
    assert paused.status == "paused"
    assert enabled.status == "active"
    assert service.list_jobs() == []
    with pytest.raises(CronJobNotFoundError, match=created.id):
        service.get_job(created.id)
    messages = [
        record.getMessage() for record in caplog.records if record.name == "vbot.automation.cron"
    ]
    assert any(message.startswith("Cron job created") for message in messages)
    assert any("fields=name,prompt" in message for message in messages)
    assert any(message.startswith("Cron job disabled") for message in messages)
    assert any(message.startswith("Cron job enabled") for message in messages)
    assert any(message.startswith("Cron job deleted") for message in messages)
    assert "private cron prompt" not in " ".join(messages)
    assert "private updated prompt" not in " ".join(messages)


def test_cron_no_op_update_does_not_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="unchanged",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="vbot.automation.cron"):
        result = service.update_job(job.id, prompt="unchanged")

    assert result.prompt == "unchanged"
    assert not [record for record in caplog.records if record.name == "vbot.automation.cron"]


def test_schedule_update_preserves_remaining_runs_when_omitted(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Run three times",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        remaining_runs=3,
    )

    updated = service.update_job(
        job.id,
        schedule_type="interval",
        interval_seconds=120,
        interval_anchor_at=datetime.now(UTC).isoformat(),
    )

    assert updated.schedule_type == "interval"
    assert updated.remaining_runs == 3


def test_explicit_null_remaining_runs_makes_recurring_job_unlimited(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Run three times",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        remaining_runs=3,
    )

    updated = service.update_job(job.id, remaining_runs=None)

    assert updated.remaining_runs is None


@pytest.mark.parametrize("remaining_runs", [None, 2])
def test_switch_to_once_requires_explicit_repeat_one_when_current_count_is_incompatible(
    tmp_path: Path,
    remaining_runs: int | None,
) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Switch schedule",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        remaining_runs=remaining_runs,
    )

    with pytest.raises(CronJobValidationError):
        service.update_job(
            job.id,
            schedule_type="once",
            run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )

    assert service.get_job(job.id).schedule_type == "cron"
    assert service.get_job(job.id).remaining_runs == remaining_runs


def test_switch_to_once_accepts_explicit_repeat_one(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Switch schedule",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        remaining_runs=4,
    )

    updated = service.update_job(
        job.id,
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        remaining_runs=1,
    )

    assert updated.schedule_type == "once"
    assert updated.remaining_runs == 1


def test_switch_to_once_preserves_compatible_repeat_one_when_omitted(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Switch schedule",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        remaining_runs=1,
    )

    updated = service.update_job(
        job.id,
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )

    assert updated.schedule_type == "once"
    assert updated.remaining_runs == 1


def test_once_update_rejects_explicit_null_repeat(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Run once",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )

    with pytest.raises(CronJobValidationError):
        service.update_job(job.id, remaining_runs=None)

    assert service.get_job(job.id).remaining_runs == 1


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


@pytest.mark.parametrize("terminal_status", ["completed", "missed"])
def test_terminal_job_status_cannot_be_changed_through_update(
    tmp_path: Path, terminal_status: CronJobStatus
) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Historical run",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
    )
    service.update_job(job.id, status=terminal_status)

    with pytest.raises(CronJobValidationError):
        service.update_job(job.id, status="active")

    assert service.get_job(job.id).status == terminal_status


def test_active_job_limit_prevents_unbounded_scheduler_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "MAX_ACTIVE_CRON_JOBS", 1)
    service.create_job(
        agent_id="agent-one",
        prompt="First",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )

    with pytest.raises(CronJobValidationError):
        service.create_job(
            agent_id="agent-two",
            prompt="Second",
            schedule_type="cron",
            cron_expression="0 10 * * *",
        )


def test_invalid_job_is_skipped_and_preserved_when_valid_jobs_change(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    valid_job = {
        "id": "job-one",
        "agent_id": "agent-one",
        "prompt": "Still runs",
        "schedule_type": "cron",
        "cron_expression": "0 9 * * *",
    }
    invalid_job = {"id": "broken", "schedule_type": "daily"}
    jobs_path.write_text(json.dumps([valid_job, invalid_job]), encoding="utf-8")
    service, _trigger_service = make_service(tmp_path)

    with caplog.at_level(logging.WARNING):
        loaded = service.list_jobs()
    service.create_job(
        agent_id="agent-two",
        prompt="New job",
        schedule_type="cron",
        cron_expression="0 10 * * *",
    )

    assert [job.id for job in loaded] == ["job-one"]
    assert loaded[0].status == "active"
    assert loaded[0].created_at
    assert caplog.records
    persisted = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert invalid_job in persisted
    assert len(persisted) == 3


def test_malformed_jobs_file_disables_cron_without_overwriting_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text("{", encoding="utf-8")
    service, _trigger_service = make_service(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert service.list_jobs() == []

    with pytest.raises(CronStorageError):
        service.create_job(
            agent_id="agent-one",
            prompt="Must not overwrite",
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )

    assert caplog.records
    assert jobs_path.read_text(encoding="utf-8") == "{"


def test_legacy_timezone_is_ignored_and_removed_on_next_save(tmp_path: Path) -> None:
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "id": "legacy-job",
                    "agent_id": "agent-one",
                    "prompt": "Legacy schedule",
                    "schedule_type": "cron",
                    "cron_expression": "0 9 * * *",
                    "timezone": "Europe/Paris",
                }
            ]
        ),
        encoding="utf-8",
    )
    service, _trigger_service = make_service(tmp_path)

    loaded = service.list_jobs()
    service.update_job("legacy-job", prompt="Migrated schedule")

    assert [job.id for job in loaded] == ["legacy-job"]
    assert loaded[0].name == "Legacy schedule"
    assert not hasattr(loaded[0], "timezone")
    persisted = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert persisted[0]["name"] == "Legacy schedule"
    assert "timezone" not in persisted[0]


def test_create_derives_name_for_internal_legacy_callers(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)

    created = service.create_job(
        agent_id="agent-one",
        prompt="  Review   the weekly reports  ",
        schedule_type="cron",
        cron_expression="0 9 * * 1",
    )

    assert created.name == "Review the weekly reports"
    persisted = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert persisted[0]["name"] == "Review the weekly reports"


def test_explicit_empty_name_is_rejected(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)

    with pytest.raises(CronJobValidationError):
        service.create_job(
            agent_id="agent-one",
            name=" ",
            prompt="Run task",
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )


def test_cron_expression_rejects_seconds_field(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)

    with pytest.raises(CronJobValidationError):
        service.create_job(
            agent_id="agent-one",
            prompt="Too frequent",
            schedule_type="cron",
            cron_expression="* * * * * *",
        )


def test_once_timestamp_is_normalized_from_server_timezone_to_explicit_utc(
    tmp_path: Path,
) -> None:
    service, _trigger_service = make_service(tmp_path, tz="Europe/Berlin")

    created = service.create_job(
        agent_id="agent-one",
        prompt="Run at local wall time",
        schedule_type="once",
        run_at="2026-07-18T16:00",
    )

    assert created.run_at == "2026-07-18T14:00:00+00:00"
    assert service.next_fire_at(created) == "2026-07-18T14:00:00+00:00"


def test_system_timezone_uses_iana_zone_with_dst_rules(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path, tz="Europe/Berlin")

    created = service.create_job(
        agent_id="agent-one",
        prompt="Use system zone",
        schedule_type="once",
        run_at="2026-12-18T16:00",
    )

    assert service.system_timezone_name() == "Europe/Berlin"
    assert created.run_at == "2026-12-18T15:00:00+00:00"


def test_timezone_change_reprojects_wall_clock_cron(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path, tz="UTC")
    job = service.create_job(
        agent_id="agent-one",
        prompt="Morning run",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )
    reference = datetime(2026, 1, 1, 8, 30, tzinfo=UTC)

    assert service.next_fire_at(job, reference_time=reference) == "2026-01-01T09:00:00+00:00"

    service.set_timezone("Europe/Berlin")

    assert service.system_timezone_name() == "Europe/Berlin"
    assert service.next_fire_at(job, reference_time=reference) == "2026-01-02T08:00:00+00:00"


def test_create_validates_target_and_owned_session(tmp_path: Path) -> None:
    resolver = SimpleNamespace(resolve_agent=Mock(return_value=SimpleNamespace(id="agent-one")))
    sessions = SimpleNamespace(exists=Mock(return_value=False))
    service, _trigger_service = make_service(tmp_path, agent_resolver=resolver, sessions=sessions)

    with pytest.raises(CronJobValidationError):
        service.create_job(
            agent_id="agent-one",
            prompt="Reuse context",
            schedule_type="cron",
            cron_expression="0 9 * * *",
            session_id="wrong-session",
            project_id="vbot",
        )

    resolver.resolve_agent.assert_called_once_with("vbot", "agent-one")
    sessions.exists.assert_called_once_with(
        SessionAddress(project_id="vbot", agent_id="agent-one", session_id="wrong-session")
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
    monkeypatch.setattr(cron_module, "_sleep_until_utc", AsyncMock())

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
    assert not service._once_fire_claim_path(job.id).exists()


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
    assert sleep_delays == [cron_module._ONCE_RETRY_DELAY_SECONDS]
    updated = service.get_job(job.id)
    assert updated.status == "completed"
    assert updated.last_fired_at is not None
    assert updated.last_fired_at.endswith("+00:00")
    assert not service._once_fire_claim_path(job.id).exists()


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
        cron_module._once_retry_delay(attempt)
        for attempt in range(1, cron_module._ONCE_MAX_FIRE_ATTEMPTS)
    ]
    assert backoff_delays == expected_backoff
    updated = service.get_job(job.id)
    assert updated.status == "failed"
    assert updated.last_fired_at is None
    assert not service._once_fire_claim_path(job.id).exists()


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
    monkeypatch.setattr(cron_module, "_sleep_until_utc", AsyncMock())
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
    claim_path = service._once_fire_claim_path(once.id)
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
    claim_path = service._once_fire_claim_path(once.id)
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

    monkeypatch.setattr(cron_module, "croniter", ImmediateCronIter)

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


def test_project_id_defaults_to_none_and_round_trips(tmp_path: Path) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)

    # Act
    bare = service.create_job(
        agent_id="builder",
        prompt="Bare prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
    )
    scoped = service.create_job(
        agent_id="builder",
        prompt="Scoped prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
        project_id="vbot",
    )

    # Assert
    assert bare.project_id is None
    assert scoped.project_id == "vbot"
    # Round-trips through persistence (clone goes through to_dict/from_dict, and a
    # fresh service re-reads the saved jobs.json).
    reloaded_service, _ = make_service(tmp_path)
    reloaded = {job.id: job for job in reloaded_service.list_jobs()}
    assert reloaded[bare.id].project_id is None
    assert reloaded[scoped.id].project_id == "vbot"


def test_blank_project_id_normalizes_to_none(tmp_path: Path) -> None:
    # Arrange
    service, _trigger_service = make_service(tmp_path)

    # Act
    job = service.create_job(
        agent_id="builder",
        prompt="Prompt",
        schedule_type="cron",
        cron_expression="* * * * *",
        project_id="   ",
    )

    # Assert
    assert job.project_id is None


def test_jobs_json_schema_accepts_optional_project_id(tmp_path: Path) -> None:
    # Arrange
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "id": "job-one",
                    "agent_id": "builder",
                    "prompt": "Prompt",
                    "schedule_type": "cron",
                    "cron_expression": "* * * * *",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                    "project_id": "vbot",
                }
            ]
        ),
        encoding="utf-8",
    )
    service, _trigger_service = make_service(tmp_path)

    # Act
    jobs = service.list_jobs()

    # Assert
    assert [job.project_id for job in jobs] == ["vbot"]


@pytest.mark.asyncio
async def test_run_once_job_fires_with_project_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    service, trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="builder",
        prompt="Once prompt",
        schedule_type="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        project_id="vbot",
    )
    monkeypatch.setattr(cron_module, "_sleep_until_utc", AsyncMock())

    # Act
    await service._run_once_job(job)

    # Assert
    trigger_service.trigger_run.assert_awaited_once_with(
        "builder",
        "Once prompt",
        None,
        project_id="vbot",
        run_kind=RunKind.CRON,
        contributes_to_agent_activity=False,
    )
    assert service.get_job(job.id).status == "completed"


@pytest.mark.asyncio
async def test_sleep_until_utc_returns_immediately_for_past_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    naps: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        naps.append(delay_seconds)

    monkeypatch.setattr(cron_module.asyncio, "sleep", record_sleep)

    # Act
    await cron_module._sleep_until_utc(datetime.now(UTC) - timedelta(seconds=1))

    # Assert
    assert naps == []


@pytest.mark.parametrize(
    ("schedule", "expected_type"),
    [
        ("2026-07-28T15:00:00+02:00", "once"),
        ("in 30m", "once"),
        ("every 2h", "interval"),
        ("0 9 * * 1-5", "cron"),
    ],
)
def test_parse_schedule_accepts_only_the_supported_forms(
    tmp_path: Path,
    schedule: str,
    expected_type: str,
) -> None:
    service, _trigger_service = make_service(tmp_path)

    parsed = service.parse_schedule(
        schedule,
        reference_time=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    )

    assert parsed.schedule_type == expected_type
    if schedule == "in 30m":
        assert parsed.run_at == "2026-07-28T12:30:00+00:00"
    if schedule == "every 2h":
        assert parsed.interval_seconds == 7200
        assert parsed.interval_anchor_at == "2026-07-28T12:00:00+00:00"


@pytest.mark.parametrize(
    "schedule",
    [
        "30m",
        "2026-07-28",
        "tomorrow morning",
        "every 5s",
        "* * * * * *",
        "in two hours",
    ],
)
def test_parse_schedule_rejects_ambiguous_or_unsupported_forms(
    tmp_path: Path,
    schedule: str,
) -> None:
    service, _trigger_service = make_service(tmp_path)

    with pytest.raises(CronJobValidationError):
        service.parse_schedule(schedule)


def test_unnamed_job_uses_stable_first_prompt_line(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="\n## Daily status\nInclude blockers and next steps.",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )

    updated = service.update_job(job.id, prompt="A completely different prompt")

    assert job.name == "Daily status"
    assert updated.name == "Daily status"


def test_interval_next_fire_uses_persisted_anchor_and_skips_missed_ticks(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Check status",
        schedule_type="interval",
        interval_seconds=7200,
        interval_anchor_at="2026-07-28T08:00:00+00:00",
    )

    next_fire_at = service.next_fire_at(
        job,
        reference_time=datetime(2026, 7, 28, 13, 15, tzinfo=UTC),
    )

    assert next_fire_at == "2026-07-28T14:00:00+00:00"
    assert service.format_schedule(job) == "every 2h"


@pytest.mark.asyncio
async def test_repeat_is_consumed_when_run_is_admitted_even_if_run_fails(tmp_path: Path) -> None:
    service, trigger_service = make_service(tmp_path)
    run = SimpleNamespace(id="run-one", wait=AsyncMock(side_effect=RuntimeError("run failed")))
    trigger_service.trigger_run.return_value = run
    job = service.create_job(
        agent_id="agent-one",
        prompt="Finite check",
        schedule_type="interval",
        interval_seconds=3600,
        remaining_runs=1,
    )

    assert await service._trigger_job_run(job) is False

    updated = service.get_job(job.id)
    assert updated.remaining_runs == 0
    assert updated.status == "failed"
    assert updated.last_run_id == "run-one"


@pytest.mark.asyncio
async def test_repeat_is_not_consumed_when_run_admission_fails(tmp_path: Path) -> None:
    service, trigger_service = make_service(tmp_path)
    trigger_service.trigger_run.side_effect = RuntimeError("not admitted")
    job = service.create_job(
        agent_id="agent-one",
        prompt="Finite check",
        schedule_type="cron",
        cron_expression="0 9 * * *",
        remaining_runs=2,
    )

    assert await service._trigger_job_run(job) is False

    updated = service.get_job(job.id)
    assert updated.remaining_runs == 2
    assert updated.status == "active"


@pytest.mark.asyncio
async def test_sleep_until_utc_realigns_after_wall_clock_jump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: the wall clock jumps past the target after the first bounded nap
    # (e.g. NTP correcting a freshly booted Pi), so the wait must end at the
    # next recheck instead of sleeping out the original full delay.
    start = datetime.now(UTC)
    target = start + timedelta(minutes=10)
    clock = iter([start, target + timedelta(seconds=1)])
    monkeypatch.setattr(cron_module, "_utc_now", lambda: next(clock))
    naps: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        naps.append(delay_seconds)

    monkeypatch.setattr(cron_module.asyncio, "sleep", record_sleep)

    # Act
    await cron_module._sleep_until_utc(target)

    # Assert
    assert naps == [cron_module._WALL_CLOCK_RECHECK_SECONDS]
