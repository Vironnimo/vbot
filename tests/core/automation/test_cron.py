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


def make_service(
    tmp_path: Path,
    *,
    agent_resolver: Any = None,
    sessions: Any = None,
) -> tuple[CronService, SimpleNamespace]:
    trigger_service = SimpleNamespace(trigger_run=AsyncMock())
    service = CronService(
        cast(Any, trigger_service),
        tmp_path,
        agent_resolver=agent_resolver,
        sessions=sessions,
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
            prompt="private cron prompt",
            schedule_type="once",
            run_at=run_at,
        )
        listed = service.list_jobs()
        loaded = service.get_job(created.id)
        updated = service.update_job(created.id, prompt="private updated prompt")
        paused = service.disable_job(created.id)
        enabled = service.enable_job(created.id)
        service.delete_job(created.id)

    # Assert
    assert [job.id for job in listed] == [created.id]
    assert loaded.prompt == "private cron prompt"
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
    assert any("fields=prompt" in message for message in messages)
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

    with pytest.raises(CronJobValidationError, match="immutable history"):
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

    with pytest.raises(CronJobValidationError, match="At most 1"):
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
    assert "Skipping invalid Cron job" in caplog.text
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

    with pytest.raises(CronStorageError, match="Invalid JSON"):
        service.create_job(
            agent_id="agent-one",
            prompt="Must not overwrite",
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )

    assert "scheduling is disabled" in caplog.text
    assert jobs_path.read_text(encoding="utf-8") == "{"


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


def test_cron_expression_rejects_seconds_field(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)

    with pytest.raises(CronJobValidationError, match="exactly 5 fields"):
        service.create_job(
            agent_id="agent-one",
            prompt="Too frequent",
            schedule_type="cron",
            cron_expression="* * * * * *",
        )


def test_once_timestamp_is_normalized_to_explicit_utc(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)

    created = service.create_job(
        agent_id="agent-one",
        prompt="Run at local wall time",
        schedule_type="once",
        run_at="2026-07-18T16:00",
        timezone="Europe/Berlin",
    )

    assert created.run_at == "2026-07-18T14:00:00+00:00"
    assert service.next_fire_at(created) == "2026-07-18T14:00:00+00:00"


def test_system_timezone_uses_iana_zone_with_dst_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "get_localzone", lambda: ZoneInfo("Europe/Berlin"))
    monkeypatch.setattr(cron_module, "get_localzone_name", lambda: "Europe/Berlin")

    created = service.create_job(
        agent_id="agent-one",
        prompt="Use system zone",
        schedule_type="once",
        run_at="2026-12-18T16:00",
    )

    assert service.system_timezone_name() == "Europe/Berlin"
    assert created.run_at == "2026-12-18T15:00:00+00:00"


def test_create_validates_target_and_owned_session(tmp_path: Path) -> None:
    resolver = SimpleNamespace(resolve_agent=Mock(return_value=SimpleNamespace(id="agent-one")))
    sessions = SimpleNamespace(exists=Mock(return_value=False))
    service, _trigger_service = make_service(tmp_path, agent_resolver=resolver, sessions=sessions)

    with pytest.raises(CronJobValidationError, match="Session does not exist"):
        service.create_job(
            agent_id="agent-one",
            prompt="Reuse context",
            schedule_type="cron",
            cron_expression="0 9 * * *",
            session_id="wrong-session",
            project_id="vbot",
        )

    resolver.resolve_agent.assert_called_once_with("vbot", "agent-one")
    sessions.exists.assert_called_once_with("agent-one", "wrong-session", "vbot")


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
        "agent-one", "Once prompt", None, project_id=None
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
async def test_recurring_job_stops_after_consecutive_run_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trigger_service = make_service(tmp_path)
    monkeypatch.setattr(cron_module, "MAX_CONSECUTIVE_CRON_FAILURES", 2)
    trigger_service.trigger_run.side_effect = RuntimeError("provider unavailable")
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
    assert updated.last_error == "provider unavailable"
    assert updated.consecutive_failures == 2


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
        "agent-one", "Once prompt", None, project_id=None
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
        _agent_id: str,
        _prompt: str,
        _session_id: str | None = None,
        *,
        project_id: str | None = None,
    ) -> None:
        service._jobs[job.id].status = "paused"

    trigger_service.trigger_run.side_effect = trigger_and_pause

    # Act
    await service._run_cron_job(job)

    # Assert
    trigger_service.trigger_run.assert_awaited_once_with(
        "agent-one", "Cron prompt", None, project_id=None
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
        _agent_id: str,
        _prompt: str,
        _session_id: str | None = None,
        *,
        project_id: str | None = None,
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
        "builder", "Once prompt", None, project_id="vbot"
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
