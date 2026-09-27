"""Tests for cron."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import core.automation.cron as cron_module
from core.automation import _cron_timing as cron_timing
from core.automation.cron import (
    CronJobNotFoundError,
    CronJobStatus,
    CronJobValidationError,
    CronStorageError,
    CronTargetAgentNotFoundError,
    CronTargetProjectNotFoundError,
    CronTargetUnavailableError,
)
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.sessions import SessionAddress
from tests.core.automation.cron_test_support import (
    make_service,
)


def test_impossible_cron_is_rejected_before_mutation(tmp_path):
    expression = "0 0 30 2 *"
    service, _ = make_service(tmp_path, tz="Europe/Berlin")
    with pytest.raises(CronJobValidationError):
        service.parse_schedule(expression)
    with pytest.raises(CronJobValidationError):
        service.create_job(
            agent_id="main", prompt="test", schedule_type="cron", cron_expression=expression
        )
    assert service.list_jobs() == []
    assert json.loads((tmp_path / "cron" / "jobs.json").read_text())["jobs"] == []
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
    invalid = {**stored["jobs"][0], "id": "broken", "cron_expression": "0 0 30 2 *"}
    stored["jobs"].append(invalid)
    path.write_text(json.dumps(stored))
    reloaded, _ = make_service(tmp_path, tz="Europe/Berlin")
    assert [item.id for item in reloaded.list_jobs()] == [job.id]
    assert reloaded.next_fire_at(reloaded.get_job(job.id)) is not None
    assert reloaded.project_occurrences(
        datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC)
    )
    reloaded.update_job(job.id, name="changed")
    assert invalid in json.loads(path.read_text())["jobs"]


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


_CRON = {"schedule_type": "cron", "cron_expression": "0 9 * * *"}
_IN_AN_HOUR = "in-an-hour"


@pytest.mark.parametrize(
    ("created", "changes", "outcome"),
    [
        pytest.param(
            {**_CRON, "remaining_runs": 3},
            {"schedule_type": "interval", "interval_seconds": 120},
            ("interval", 3),
            id="kept-when-omitted-on-schedule-change",
        ),
        pytest.param(
            {**_CRON, "remaining_runs": 3},
            {"remaining_runs": None},
            ("cron", None),
            id="explicit-null-makes-recurring-unlimited",
        ),
        pytest.param(
            {**_CRON, "remaining_runs": None},
            {"schedule_type": "once", "run_at": _IN_AN_HOUR},
            CronJobValidationError,
            id="switch-to-once-from-unlimited-needs-explicit-one",
        ),
        pytest.param(
            {**_CRON, "remaining_runs": 2},
            {"schedule_type": "once", "run_at": _IN_AN_HOUR},
            CronJobValidationError,
            id="switch-to-once-from-several-needs-explicit-one",
        ),
        pytest.param(
            {**_CRON, "remaining_runs": 4},
            {"schedule_type": "once", "run_at": _IN_AN_HOUR, "remaining_runs": 1},
            ("once", 1),
            id="switch-to-once-with-explicit-one",
        ),
        pytest.param(
            {**_CRON, "remaining_runs": 1},
            {"schedule_type": "once", "run_at": _IN_AN_HOUR},
            ("once", 1),
            id="switch-to-once-keeps-a-compatible-one",
        ),
        pytest.param(
            {"schedule_type": "once", "run_at": _IN_AN_HOUR},
            {"remaining_runs": None},
            CronJobValidationError,
            id="once-rejects-explicit-null",
        ),
    ],
)
def test_update_applies_the_remaining_runs_rules(
    tmp_path: Path,
    created: dict[str, Any],
    changes: dict[str, Any],
    outcome: tuple[str, int | None] | type[Exception],
) -> None:
    in_an_hour = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def resolved(fields: dict[str, Any]) -> dict[str, Any]:
        values = {**fields}
        if values.get("run_at") == _IN_AN_HOUR:
            values["run_at"] = in_an_hour
        if values.get("schedule_type") == "interval":
            values["interval_anchor_at"] = datetime.now(UTC).isoformat()
        return values

    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(agent_id="agent-one", prompt="Runs", **resolved(created))

    if isinstance(outcome, tuple):
        updated = service.update_job(job.id, **resolved(changes))
        assert (updated.schedule_type, updated.remaining_runs) == outcome
    else:
        with pytest.raises(outcome):
            service.update_job(job.id, **resolved(changes))
        assert service.get_job(job.id) == job


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
    assert json.loads(jobs_path.read_text(encoding="utf-8")) == {"format_version": 1, "jobs": []}


def test_terminal_job_status_cannot_be_changed_through_update(tmp_path: Path) -> None:
    terminal_status: CronJobStatus = "completed"
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
        "name": "Still runs",
        "prompt": "Still runs",
        "schedule_type": "cron",
        "cron_expression": "0 9 * * *",
    }
    invalid_job = {"id": "broken", "schedule_type": "daily"}
    unnamed_job = {**valid_job, "id": "unnamed"}
    del unnamed_job["name"]
    jobs_path.write_text(
        json.dumps({"format_version": 1, "jobs": [valid_job, invalid_job, unnamed_job]}),
        encoding="utf-8",
    )
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
    persisted = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
    assert invalid_job in persisted
    assert unnamed_job in persisted
    assert len(persisted) == 4


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param("{", "Invalid JSON", id="malformed"),
        pytest.param(
            json.dumps({"format_version": 2, "jobs": []}),
            "written by a newer vBot",
            id="newer-format",
        ),
    ],
)
def test_unreadable_jobs_file_disables_cron_without_overwriting_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str, message: str
) -> None:
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(content, encoding="utf-8")
    service, _trigger_service = make_service(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert service.list_jobs() == []
    with pytest.raises(CronStorageError, match=message):
        service.create_job(
            agent_id="agent-one",
            prompt="Must not overwrite",
            schedule_type="cron",
            cron_expression="0 9 * * *",
        )

    assert caplog.records
    assert jobs_path.read_text(encoding="utf-8") == content


def test_unknown_fields_are_kept_when_jobs_are_saved(tmp_path: Path) -> None:
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "future_setting": {"kept": True},
                "jobs": [
                    {
                        "id": "job-one",
                        "agent_id": "agent-one",
                        "name": "Morning",
                        "prompt": "Morning schedule",
                        "schedule_type": "cron",
                        "cron_expression": "0 9 * * *",
                        "timezone": "Europe/Paris",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service, _trigger_service = make_service(tmp_path)

    loaded = service.list_jobs()
    service.update_job("job-one", prompt="Changed schedule")

    assert [job.id for job in loaded] == ["job-one"]
    assert not hasattr(loaded[0], "timezone")
    persisted = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert persisted["format_version"] == 1
    assert persisted["future_setting"] == {"kept": True}
    assert persisted["jobs"][0]["prompt"] == "Changed schedule"
    assert persisted["jobs"][0]["timezone"] == "Europe/Paris"


def test_save_refuses_a_jobs_file_that_stopped_loading(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    job = service.create_job(
        agent_id="agent-one",
        prompt="Loaded before the file broke",
        schedule_type="cron",
        cron_expression="0 9 * * *",
    )
    jobs_path = tmp_path / "cron" / "jobs.json"
    jobs_path.write_text("[]", encoding="utf-8")

    with pytest.raises(CronStorageError, match="Refusing to overwrite Cron jobs"):
        service.update_job(job.id, prompt="Must not overwrite")

    assert jobs_path.read_text(encoding="utf-8") == "[]"


def test_create_derives_name_when_none_is_given(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)

    created = service.create_job(
        agent_id="agent-one",
        prompt="  Review   the weekly reports  ",
        schedule_type="cron",
        cron_expression="0 9 * * 1",
    )

    assert created.name == "Review the weekly reports"
    persisted = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert persisted["jobs"][0]["name"] == "Review the weekly reports"


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


@pytest.mark.parametrize(
    ("run_at", "expected"),
    [
        pytest.param("2099-07-18T16:00", "2099-07-18T14:00:00+00:00", id="summer-time"),
        pytest.param("2099-12-18T16:00", "2099-12-18T15:00:00+00:00", id="winter-time"),
    ],
)
def test_once_local_wall_time_is_stored_as_explicit_utc(
    tmp_path: Path, run_at: str, expected: str
) -> None:
    service, _trigger_service = make_service(tmp_path, tz="Europe/Berlin")

    created = service.create_job(
        agent_id="agent-one", prompt="Run at local wall time", schedule_type="once", run_at=run_at
    )

    assert service.system_timezone_name() == "Europe/Berlin"
    assert created.run_at == expected
    assert service.next_fire_at(created) == expected


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


@pytest.mark.parametrize(
    ("expression", "reference", "expected"),
    [
        ("30 2 * * *", "2026-10-25T01:15:00+00:00", "2026-10-26T01:30:00+00:00"),
        ("*/30 * * * *", "2026-10-25T01:15:00+00:00", "2026-10-25T02:00:00+00:00"),
        ("30 2 * * *", "2026-10-25T00:15:00+00:00", "2026-10-25T00:30:00+00:00"),
    ],
)
def test_next_cron_fire_never_replays_elapsed_overlap(
    tmp_path: Path, expression: str, reference: str, expected: str
) -> None:
    service, _ = make_service(tmp_path, tz="Europe/Berlin")
    job = service.create_job(
        agent_id="main", prompt="test", schedule_type="cron", cron_expression=expression
    )
    assert service.next_fire_at(job, reference_time=datetime.fromisoformat(reference)) == expected


def test_next_fire_keeps_local_wall_clock_across_dst_transitions(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path, tz="Europe/Berlin")
    job = service.create_job(
        agent_id="agent-one",
        prompt="Midnight run",
        schedule_type="cron",
        cron_expression="0 0 * * *",
    )
    berlin = ZoneInfo("Europe/Berlin")

    # Spring forward: 2026-03-29 is 23 hours long, so the next local midnight is
    # 2026-03-29T22:00Z — not 23:00 local that evening, which a fixed-offset step
    # after midnight would report.
    assert (
        service.next_fire_at(job, reference_time=datetime(2026, 3, 29, tzinfo=berlin))
        == "2026-03-29T22:00:00+00:00"
    )
    # Fall back: 2026-10-25 is 25 hours long.
    assert (
        service.next_fire_at(job, reference_time=datetime(2026, 10, 25, tzinfo=berlin))
        == "2026-10-25T23:00:00+00:00"
    )


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


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    ("resolver_error", "expected_error", "resolver_base"),
    [
        (
            ResolutionAgentNotFoundError("agent 'ghost' is not on project 'vbot' team"),
            CronTargetAgentNotFoundError,
            ResolutionAgentNotFoundError,
        ),
        (
            ResolutionProjectNotFoundError("Project not found: vbot"),
            CronTargetProjectNotFoundError,
            ResolutionProjectNotFoundError,
        ),
    ],
)
def test_missing_target_is_a_cron_validation_and_resolver_not_found_error(
    tmp_path: Path,
    operation: str,
    resolver_error: AgentResolutionError,
    expected_error: type[CronJobValidationError],
    resolver_base: type[AgentResolutionError],
) -> None:
    # Cron's own catchers see a validation error; RPC accessors see the precise
    # resolver not-found error and report agent_not_found / project_not_found.
    resolver = SimpleNamespace(resolve_agent=Mock(return_value=SimpleNamespace(id="ghost")))
    service, _trigger_service = make_service(tmp_path, agent_resolver=resolver)
    job = service.create_job(
        agent_id="ghost", prompt="Ping", schedule_type="cron", cron_expression="0 9 * * *"
    )
    resolver.resolve_agent.side_effect = resolver_error

    with pytest.raises(expected_error) as raised:
        if operation == "create":
            service.create_job(
                agent_id="ghost",
                prompt="Ping",
                schedule_type="cron",
                cron_expression="0 9 * * *",
                project_id="vbot",
            )
        else:
            service.update_job(job.id, project_id="vbot")

    assert isinstance(raised.value, CronJobValidationError)
    assert isinstance(raised.value, resolver_base)
    assert raised.value.__cause__ is resolver_error
    assert [stored.id for stored in service.list_jobs()] == [job.id]
    assert service.list_jobs()[0].project_id is None


@pytest.mark.parametrize("operation", ["create", "update"])
def test_target_that_cannot_run_keeps_the_resolver_reason(tmp_path: Path, operation: str) -> None:
    # An existing target without a usable Model must not be reported as missing.
    reason = "agent 'stranded' has no usable model"
    resolver = SimpleNamespace(resolve_agent=Mock(return_value=SimpleNamespace(id="stranded")))
    service, _trigger_service = make_service(tmp_path, agent_resolver=resolver)
    job = service.create_job(
        agent_id="stranded", prompt="Ping", schedule_type="cron", cron_expression="0 9 * * *"
    )
    resolver.resolve_agent.side_effect = AgentResolutionError(reason)

    with pytest.raises(CronJobValidationError) as raised:
        if operation == "create":
            service.create_job(
                agent_id="stranded",
                prompt="Ping",
                schedule_type="cron",
                cron_expression="0 9 * * *",
                project_id="vbot",
            )
        else:
            service.update_job(job.id, project_id="vbot")

    assert isinstance(raised.value, CronTargetUnavailableError)
    assert not isinstance(
        raised.value, (ResolutionAgentNotFoundError, ResolutionProjectNotFoundError)
    )
    assert reason in str(raised.value)
    assert "stranded@vbot" in str(raised.value)


def test_project_id_defaults_to_none_and_round_trips(tmp_path: Path) -> None:
    service, _trigger_service = make_service(tmp_path)
    fields: dict[str, Any] = {"agent_id": "builder", "prompt": "Prompt", "schedule_type": "cron"}
    bare = service.create_job(**fields, cron_expression="* * * * *")
    blank = service.create_job(**fields, cron_expression="* * * * *", project_id="   ")
    scoped = service.create_job(**fields, cron_expression="* * * * *", project_id="vbot")

    # A blank project id means no project.
    assert [job.project_id for job in (bare, blank, scoped)] == [None, None, "vbot"]
    reloaded_service, _ = make_service(tmp_path)
    reloaded = {job.id: job.project_id for job in reloaded_service.list_jobs()}
    assert reloaded == {bare.id: None, blank.id: None, scoped.id: "vbot"}


@pytest.mark.asyncio
async def test_sleep_until_utc_returns_immediately_for_past_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    naps: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        naps.append(delay_seconds)

    monkeypatch.setattr(cron_timing, "_sleep", record_sleep)

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
    ["2026-07-28", "every 5s", "* * * * * *", "30m"],
    ids=["date-without-time", "seconds-interval", "six-field-cron", "duration-without-in"],
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

    monkeypatch.setattr(cron_timing, "_sleep", record_sleep)

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
