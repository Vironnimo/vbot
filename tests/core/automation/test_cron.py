"""Tests for cron."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.automation.cron as cron_module
from core.automation import _cron_timing as cron_timing
from core.automation.cron import (
    CronJobNotFoundError,
    CronJobStatus,
    CronJobValidationError,
    CronStorageError,
)
from core.runs import RunKind
from core.sessions import SessionAddress
from tests.core.automation.cron_test_support import (
    make_service,
)


@pytest.mark.parametrize("expression", ["0 0 30 2 *", "0 0 31 4 *"])
def test_impossible_cron_is_rejected_before_mutation(tmp_path, expression):
    service, _ = make_service(tmp_path, tz="Europe/Berlin")
    with pytest.raises(CronJobValidationError):
        service.parse_schedule(expression)
    with pytest.raises(CronJobValidationError):
        service.create_job(
            agent_id="main", prompt="test", schedule_type="cron", cron_expression=expression
        )
    assert service.list_jobs() == []
    assert json.loads((tmp_path / "cron" / "jobs.json").read_text()) == []
    job = service.create_job(
        agent_id="main", prompt="test", schedule_type="cron", cron_expression="0 0 29 2 *"
    )
    path = tmp_path / "cron" / "jobs.json"
    before = path.read_bytes()
    with pytest.raises(CronJobValidationError):
        service.update_job(job.id, cron_expression=expression)
    assert path.read_bytes() == before
    assert service.get_job(job.id) == job
    assert service.next_fire_at(job) is not None


def test_impossible_stored_cron_is_skipped_without_breaking_siblings(tmp_path):
    service, _ = make_service(tmp_path, tz="Europe/Berlin")
    job = service.create_job(
        agent_id="main", prompt="test", schedule_type="cron", cron_expression="0 0 * * *"
    )
    path = tmp_path / "cron" / "jobs.json"
    stored = json.loads(path.read_text())
    invalid = {**stored[0], "id": "broken", "cron_expression": "0 0 30 2 *"}
    path.write_text(json.dumps([*stored, invalid]))
    reloaded, _ = make_service(tmp_path, tz="Europe/Berlin")
    assert [item.id for item in reloaded.list_jobs()] == [job.id]
    assert reloaded.next_fire_at(reloaded.get_job(job.id)) is not None
    assert reloaded.project_occurrences(
        datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC)
    )
    reloaded.update_job(job.id, name="changed")
    assert invalid in json.loads(path.read_text())


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
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", AsyncMock())

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
    await cron_timing._sleep_until_utc(datetime.now(UTC) - timedelta(seconds=1))

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
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: next(clock))
    naps: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        naps.append(delay_seconds)

    monkeypatch.setattr(cron_module.asyncio, "sleep", record_sleep)

    # Act
    await cron_timing._sleep_until_utc(target)

    # Assert
    assert naps == [cron_timing._WALL_CLOCK_RECHECK_SECONDS]


def test_short_ids_skip_collisions_after_reload(tmp_path, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    service, _ = make_service(tmp_path)
    first = service.create_job(
        agent_id="agent", prompt="first", schedule_type="interval", interval_seconds=60
    )
    reloaded, _ = make_service(tmp_path)
    second = reloaded.create_job(
        agent_id="agent", prompt="second", schedule_type="interval", interval_seconds=60
    )
    assert first.id == "cron_000000000001"
    assert second.id == "cron_000000000002"
    assert reloaded.get_job(first.id).prompt == "first"
