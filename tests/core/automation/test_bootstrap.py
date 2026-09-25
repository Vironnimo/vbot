"""Tests for persistent startup-triggered Bootstrap Runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.automation import bootstrap as bootstrap_module
from core.automation.bootstrap import (
    BootstrapJobValidationError,
    BootstrapService,
    BootstrapStorageError,
    validate_bootstrap_jobs_data,
)
from core.chat import ChatMessage
from core.runs import RunStatus
from core.sessions import ChatSession, ChatSessionManager
from tests.core.sessions.history_fixtures import complete_run

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class StubRun:
    def __init__(
        self,
        run_id: str,
        session_id: str,
        *,
        error: Exception | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.id = run_id
        self.session_id = session_id
        self.status = RunStatus.RUNNING
        self._error = error
        self._release = release

    async def wait(self) -> None:
        if self._release is not None:
            await self._release.wait()
        if self._error is not None:
            self.status = RunStatus.FAILED
            raise self._error
        self.status = RunStatus.COMPLETED


class StubTriggerService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.runs: list[StubRun] = []

    async def trigger_run(self, agent_id: str, prompt: str, **kwargs: Any) -> StubRun:
        self.calls.append({"agent_id": agent_id, "prompt": prompt, **kwargs})
        run = (
            self.runs.pop(0)
            if self.runs
            else StubRun(
                f"run-{len(self.calls)}", kwargs.get("session_id") or f"session-{len(self.calls)}"
            )
        )
        return run


def make_service(trigger: StubTriggerService, data_root: Path, startup_id: str) -> BootstrapService:
    return BootstrapService(cast(Any, trigger), data_root, startup_id=startup_id)


@pytest.mark.asyncio
async def test_once_job_arms_for_next_startup_and_completes(tmp_path: Path) -> None:
    first_trigger = StubTriggerService()
    first = make_service(first_trigger, tmp_path, "startup-one")
    created = first.create_job(
        agent_id="main",
        name="Verify update",
        prompt="Check server status",
        mode="once",
        session_id="session-one",
    )

    first.activate()
    await first.wait_until_idle()
    assert first_trigger.calls == []

    second_trigger = StubTriggerService()
    second = make_service(second_trigger, tmp_path, "startup-two")
    second.activate()
    await second.wait_until_idle()

    assert second_trigger.calls == [
        {
            "agent_id": "main",
            "prompt": "Check server status",
            "session_id": "session-one",
            "internal": True,
            "project_id": None,
            "run_kind": "system",
            "contributes_to_agent_activity": False,
            "resume_process_restart": True,
        }
    ]
    completed = second.get_job(created.id)
    assert completed.status == "completed"
    assert completed.last_outcome == "success"
    assert completed.last_run_id == "run-1"


@pytest.mark.asyncio
async def test_always_job_runs_once_per_startup(tmp_path: Path) -> None:
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    created = creator.create_job(agent_id="main", prompt="Health check", mode="always")

    trigger = StubTriggerService()
    service = make_service(trigger, tmp_path, "boot-a")
    service.activate()
    await service.wait_until_idle()
    service.activate()
    await service.wait_until_idle()

    assert len(trigger.calls) == 1
    assert service.get_job(created.id).status == "active"

    next_trigger = StubTriggerService()
    next_service = make_service(next_trigger, tmp_path, "boot-b")
    next_service.activate()
    await next_service.wait_until_idle()
    assert len(next_trigger.calls) == 1


@pytest.mark.asyncio
async def test_shutdown_cancellation_leaves_once_job_retryable(tmp_path: Path) -> None:
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    created = creator.create_job(agent_id="main", prompt="Verify", mode="once")
    release = asyncio.Event()
    trigger = StubTriggerService()
    trigger.runs.append(StubRun("run-blocked", "new-session", release=release))
    service = make_service(trigger, tmp_path, "boot-a")

    service.activate()
    while not trigger.calls:
        await asyncio.sleep(0)
    await service.aclose()

    interrupted = service.get_job(created.id)
    assert interrupted.status == "active"
    assert interrupted.last_outcome is None

    retry_trigger = StubTriggerService()
    retry = make_service(retry_trigger, tmp_path, "boot-b")
    retry.activate()
    await retry.wait_until_idle()
    assert len(retry_trigger.calls) == 1
    assert retry.get_job(created.id).status == "completed"


@pytest.mark.asyncio
async def test_same_session_jobs_run_sequentially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_ids = iter(("boot_000000000002", "boot_000000000001"))
    monkeypatch.setattr(bootstrap_module, "new_id", lambda *_args, **_kwargs: next(job_ids))
    monkeypatch.setattr(
        bootstrap_module,
        "_utc_now_iso",
        lambda: "2026-08-07T12:00:00+00:00",
    )
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    for prompt in ("first", "second"):
        creator.create_job(agent_id="main", prompt=prompt, mode="once", session_id="session-one")
    first_release = asyncio.Event()
    trigger = StubTriggerService()
    trigger.runs.extend(
        [
            StubRun("run-one", "session-one", release=first_release),
            StubRun("run-two", "session-one"),
        ]
    )
    service = make_service(trigger, tmp_path, "boot")

    service.activate()
    while len(trigger.calls) < 1:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(trigger.calls) == 1
    first_release.set()
    await service.wait_until_idle()
    assert [call["prompt"] for call in trigger.calls] == ["first", "second"]


@pytest.mark.asyncio
async def test_different_session_jobs_can_run_concurrently(tmp_path: Path) -> None:
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    creator.create_job(agent_id="main", prompt="first", mode="once", session_id="session-one")
    creator.create_job(agent_id="main", prompt="second", mode="once", session_id="session-two")
    release = asyncio.Event()
    trigger = StubTriggerService()
    trigger.runs.extend(
        [
            StubRun("run-one", "session-one", release=release),
            StubRun("run-two", "session-two", release=release),
        ]
    )
    service = make_service(trigger, tmp_path, "boot")

    service.activate()
    while len(trigger.calls) < 2:
        await asyncio.sleep(0)
    assert {call["session_id"] for call in trigger.calls} == {"session-one", "session-two"}
    release.set()
    await service.wait_until_idle()


@pytest.mark.asyncio
async def test_running_job_cannot_be_mutated(tmp_path: Path) -> None:
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    created = creator.create_job(agent_id="main", prompt="Verify", mode="once")
    release = asyncio.Event()
    trigger = StubTriggerService()
    trigger.runs.append(StubRun("run-one", "session-one", release=release))
    service = make_service(trigger, tmp_path, "boot")
    service.activate()
    while not trigger.calls:
        await asyncio.sleep(0)

    with pytest.raises(BootstrapJobValidationError):
        service.delete_job(created.id)

    release.set()
    await service.wait_until_idle()


@pytest.mark.asyncio
async def test_failed_once_job_can_be_rearmed_for_a_later_startup(tmp_path: Path) -> None:
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    created = creator.create_job(agent_id="main", prompt="Verify", mode="once")
    trigger = StubTriggerService()
    trigger.runs.append(StubRun("run-failed", "session-one", error=RuntimeError("boom")))
    failed_service = make_service(trigger, tmp_path, "boot-a")
    failed_service.activate()
    await failed_service.wait_until_idle()

    assert failed_service.get_job(created.id).status == "failed"
    rearmed = failed_service.enable_job(created.id)
    assert rearmed.status == "active"
    assert rearmed.last_run_id is None
    assert rearmed.last_session_id is None
    failed_service.activate()
    await failed_service.wait_until_idle()
    assert len(trigger.calls) == 1

    retry_trigger = StubTriggerService()
    retry = make_service(retry_trigger, tmp_path, "boot-b")
    retry.activate()
    await retry.wait_until_idle()
    assert len(retry_trigger.calls) == 1
    assert retry.get_job(created.id).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "expected_job_status", "expected_outcome"),
    [
        ("completed", "completed", "success"),
        ("interrupted", "failed", "failed"),
    ],
)
async def test_restart_reconciles_terminal_run_before_retry(
    tmp_path: Path,
    run_status: str,
    expected_job_status: str,
    expected_outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    created = creator.create_job(agent_id="main", prompt="Verify", mode="once")
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("main", session_id="bootstrap-session")
    session = session.start_run("run-before-crash")
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id="run-before-crash",
            status=run_status,
            iteration_count=1,
            timing={
                "started_at": "2026-08-02T12:00:00+00:00",
                "completed_at": "2026-08-02T12:00:01+00:00",
                "duration_ms": 1000,
            },
        ),
    )
    jobs_path = tmp_path / "bootstrap" / "jobs.json"
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    stored = payload["jobs"][0]
    stored["last_run_id"] = "run-before-crash"
    stored["last_session_id"] = "bootstrap-session"
    stored["last_started_startup_id"] = "crashed-startup"
    jobs_path.write_text(json.dumps(payload), encoding="utf-8")

    def fail_full_load(self: ChatSession) -> list[ChatMessage]:
        raise AssertionError("Bootstrap reconciliation must query the target Run summary")

    monkeypatch.setattr(ChatSession, "load", fail_full_load)
    trigger = StubTriggerService()
    service = BootstrapService(
        cast(Any, trigger),
        tmp_path,
        startup_id="recovery-startup",
        sessions=sessions,
    )

    service.activate()
    await service.wait_until_idle()

    assert trigger.calls == []
    reconciled = service.get_job(created.id)
    assert reconciled.status == expected_job_status
    assert reconciled.last_outcome == expected_outcome


def test_validation_reports_invalid_mode() -> None:
    diagnostics = validate_bootstrap_jobs_data(
        {
            "format_version": 1,
            "jobs": [
                {
                    "id": "job",
                    "agent_id": "main",
                    "name": "Check",
                    "prompt": "Check",
                    "mode": "sometimes",
                    "status": "active",
                    "created_at": "2026-08-02T00:00:00+00:00",
                    "armed_after_startup_id": "startup",
                }
            ],
        }
    )

    assert any(diagnostic.path == "$.jobs[0].mode" for diagnostic in diagnostics)


def test_validation_requires_the_versioned_document() -> None:
    assert [diagnostic.path for diagnostic in validate_bootstrap_jobs_data([])] == ["$"]
    diagnostics = validate_bootstrap_jobs_data({"jobs": []})
    assert [diagnostic.path for diagnostic in diagnostics] == ["$.format_version"]


def test_save_keeps_invalid_entries_and_unknown_fields(tmp_path: Path) -> None:
    creator = make_service(StubTriggerService(), tmp_path, "creator")
    created = creator.create_job(agent_id="main", prompt="Verify", mode="always")
    jobs_path = tmp_path / "bootstrap" / "jobs.json"
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    payload["future_setting"] = 1
    payload["jobs"][0]["future_field"] = "kept"
    invalid = {"id": "broken", "mode": "sometimes"}
    payload["jobs"].append(invalid)
    jobs_path.write_text(json.dumps(payload), encoding="utf-8")

    service = make_service(StubTriggerService(), tmp_path, "later")
    assert [job.id for job in service.list_jobs()] == [created.id]
    service.update_job(created.id, prompt="Changed")

    rewritten = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert rewritten["format_version"] == 1
    assert rewritten["future_setting"] == 1
    assert rewritten["jobs"][0]["prompt"] == "Changed"
    assert rewritten["jobs"][0]["future_field"] == "kept"
    assert rewritten["jobs"][1] == invalid


def test_jobs_written_by_a_newer_vbot_are_never_overwritten(tmp_path: Path) -> None:
    jobs_path = tmp_path / "bootstrap" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    original = json.dumps({"format_version": 2, "jobs": []})
    jobs_path.write_text(original, encoding="utf-8")
    service = make_service(StubTriggerService(), tmp_path, "startup")

    assert service.list_jobs() == []
    with pytest.raises(BootstrapStorageError, match="written by a newer vBot"):
        service.create_job(agent_id="main", prompt="Must not overwrite", mode="once")

    assert jobs_path.read_text(encoding="utf-8") == original
