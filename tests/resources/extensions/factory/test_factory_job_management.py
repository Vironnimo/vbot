"""Contracts for Simplified Software Factory Job and Dashboard management operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from resources.extensions.factory._job_management import (
    JOB_OPERATION_DESCRIPTIONS,
    JOB_OPERATION_SCHEMAS,
    JOB_SECRET_OPERATIONS,
    FactoryJobManagement,
)
from resources.extensions.factory._job_triggers import FactoryJobTriggers
from resources.extensions.factory.store import FactoryError, FactoryStore
from tests.resources.extensions.factory.factory_test_support import FakeHost

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


class _FakeJobExecution:
    def __init__(self, store: FactoryStore) -> None:
        self.store = store
        self.replied: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def reply_run(self, run_id: str, text: str) -> Json:
        self.replied.append((run_id, text))
        return await self.store.jobs.transition_run(run_id, "running", question="", reason="")

    async def cancel_run(self, run_id: str) -> Json:
        self.cancelled.append(run_id)
        return await self.store.jobs.transition_run(run_id, "cancelled")


class _FakeService:
    def __init__(self, store: FactoryStore, tmp_path: Path) -> None:
        self.store = store
        self.host: FakeHost = FakeHost(tmp_path)
        self.lock = asyncio.Lock()
        self._closing = False
        self.schedule_requests = 0
        self.job_triggers = FactoryJobTriggers(self)  # type: ignore[arg-type]
        self.job_execution = _FakeJobExecution(store)

    def records(self) -> FactoryStore:
        return self.store

    def request_schedule(self) -> None:
        self.schedule_requests += 1


async def _setup(
    factory_store: FactoryStore, tmp_path: Path
) -> tuple[_FakeService, FactoryJobManagement, Json, Json, Json]:
    service = _FakeService(factory_store, tmp_path)
    management = FactoryJobManagement(service)  # type: ignore[arg-type]
    factory = await factory_store.native.create_factory(
        "project", leader_agent_id="leader", leader_agent_proof="inc-1"
    )
    leader = await factory_store.native.get_member(factory["leader_member_id"])
    worker = await factory_store.native.add_member(
        factory["id"], "worker", agent_proof="inc-2", role="Implementer"
    )
    return service, management, factory, leader, worker


async def test_exports_and_unknown_operation(factory_store: FactoryStore, tmp_path: Path) -> None:
    assert frozenset({"job.secret"}) == JOB_SECRET_OPERATIONS
    assert set(JOB_OPERATION_SCHEMAS) == set(JOB_OPERATION_DESCRIPTIONS)
    assert set(JOB_OPERATION_SCHEMAS) == {
        "job.list",
        "job.get",
        "job.create",
        "job.update",
        "job.delete",
        "job.trigger",
        "job.secret",
        "job_run.list",
        "job_run.get",
        "job_run.reply",
        "job_run.cancel",
        "dashboard.get",
    }
    _, management, _, _, _ = await _setup(factory_store, tmp_path)
    assert await management.operation("task.list", {"factory_id": "fac_1"}) is None
    assert await management.operation("unknown.op", {}) is None


async def test_job_crud_triggers_sync_and_secrets(
    factory_store: FactoryStore, tmp_path: Path
) -> None:
    service, management, factory, _, worker = await _setup(factory_store, tmp_path)
    assert service.host.triggers is not None

    with pytest.raises(FactoryError) as bad_sched:
        await management.operation(
            "job.create",
            {
                "factory_id": factory["id"],
                "name": "Bad Job",
                "prompt": "Run checks",
                "triggers": [{"type": "schedule", "title": "Bad", "schedule": "bad-cron"}],
            },
        )
    assert bad_sched.value.code == "invalid_schedule"
    assert await factory_store.jobs.list_jobs(factory["id"]) == []

    created_res = await management.operation(
        "job.create",
        {
            "factory_id": factory["id"],
            "name": "Nightly",
            "prompt": "Run pytest",
            "description": "Nightly test run",
            "agent_member_id": worker["id"],
            "enabled": True,
            "triggers": [
                {
                    "id": "sched_1",
                    "type": "schedule",
                    "title": "Daily",
                    "schedule": "0 9 * * *",
                    "enabled": True,
                },
                {
                    "id": "hook_1",
                    "type": "webhook",
                    "title": "GitHub",
                    "schedule": "",
                    "enabled": True,
                },
            ],
        },
    )
    assert created_res is not None
    job = created_res["job"]
    assert job["name"] == "Nightly"
    assert job["revision"] == 1
    assert len(job["triggers"]) == 2
    assert job["triggers"][0]["status"] == "active"
    assert job["triggers"][1]["webhook_url"].startswith("http://vbot/hooks/")
    assert service.host.published[-1] == ("jobs", [job["id"]], 1)
    assert len(service.host.triggers.list()) == 2

    listed_enriched = await management.operation("job.list", {"factory_id": factory["id"]})
    assert listed_enriched is not None
    assert listed_enriched["jobs"][0]["triggers"][1]["webhook_url"] is not None

    listed_raw = await management.operation(
        "job.list", {"factory_id": factory["id"], "include_triggers_status": False}
    )
    assert listed_raw is not None
    assert "status" not in listed_raw["jobs"][0]["triggers"][0]

    fetched = await management.operation("job.get", {"job_id": job["id"]})
    assert fetched is not None
    assert fetched["job"]["id"] == job["id"]
    assert fetched["job"]["triggers"][0]["status"] == "active"

    secret_res = await management.operation(
        "job.secret",
        {"factory_id": factory["id"], "job_id": job["id"], "trigger_id": "hook_1"},
    )
    assert secret_res is not None
    first_secret = secret_res["secret"]
    assert first_secret.startswith("secret-")

    rotated_res = await management.operation(
        "job.secret",
        {
            "factory_id": factory["id"],
            "job_id": job["id"],
            "trigger_id": "hook_1",
            "rotate": True,
        },
    )
    assert rotated_res is not None
    assert rotated_res["secret"] == f"{first_secret}-rotated"

    with pytest.raises(FactoryError) as no_secret:
        await management.operation(
            "job.secret",
            {"factory_id": factory["id"], "job_id": job["id"], "trigger_id": "sched_1"},
        )
    assert no_secret.value.code == "trigger_not_found"

    updated_res = await management.operation(
        "job.update",
        {
            "job_id": job["id"],
            "name": "Nightly v2",
            "enabled": False,
            "triggers": [
                {
                    "id": "sched_1",
                    "type": "schedule",
                    "title": "Daily",
                    "schedule": "0 10 * * *",
                    "enabled": True,
                }
            ],
            "expected_revision": 1,
        },
    )
    assert updated_res is not None
    updated = updated_res["job"]
    assert updated["name"] == "Nightly v2"
    assert updated["enabled"] is False
    assert updated["revision"] == 2
    assert len(updated["triggers"]) == 1
    assert len(service.host.triggers.list()) == 1
    assert service.host.published[-1] == ("jobs", [job["id"]], 2)

    with pytest.raises(FactoryError) as conflict:
        await management.operation(
            "job.update",
            {"job_id": job["id"], "name": "Stale", "expected_revision": 1},
        )
    assert conflict.value.code == "revision_conflict"

    deleted_res = await management.operation("job.delete", {"job_id": job["id"]})
    assert deleted_res == {"deleted": True, "job_id": job["id"]}
    assert service.host.triggers.list() == []
    assert service.host.published[-1] == ("jobs", [job["id"]], 0)


async def test_job_create_refuses_triggers_when_host_triggers_unavailable(
    factory_store: FactoryStore, tmp_path: Path
) -> None:
    service, management, factory, _, _ = await _setup(factory_store, tmp_path)
    service.host.triggers = None

    with pytest.raises(FactoryError) as exc:
        await management.operation(
            "job.create",
            {
                "factory_id": factory["id"],
                "name": "Hook Job",
                "prompt": "Handle event",
                "triggers": [{"type": "webhook", "title": "Hook"}],
            },
        )
    assert exc.value.code == "triggers_unavailable"


async def test_job_trigger_and_run_operations(factory_store: FactoryStore, tmp_path: Path) -> None:
    service, management, factory, leader, worker = await _setup(factory_store, tmp_path)
    created_res = await management.operation(
        "job.create",
        {
            "factory_id": factory["id"],
            "name": "Deploy Check",
            "prompt": "Check deployment",
            "agent_member_id": worker["id"],
        },
    )
    assert created_res is not None
    job = created_res["job"]

    trig_res = await management.operation(
        "job.trigger",
        {
            "job_id": job["id"],
            "member_id": leader["id"],
            "input_note": "Focus on staging",
        },
    )
    assert trig_res is not None
    run = trig_res["run"]
    assert run["origin"] == "manual"
    assert run["state"] == "queued"
    assert run["member_id"] == leader["id"]
    assert run["input_note"] == "Focus on staging"
    assert service.schedule_requests == 1
    assert service.host.published[-1] == ("job_runs", [run["id"]], run["revision"])

    got_run = await management.operation("job_run.get", {"run_id": run["id"]})
    assert got_run is not None
    assert got_run["run"]["id"] == run["id"]

    listed = await management.operation(
        "job_run.list",
        {"factory_id": factory["id"], "job_id": job["id"], "state": "queued", "limit": 10},
    )
    assert listed is not None
    assert [item["id"] for item in listed["runs"]] == [run["id"]]

    await factory_store.jobs.transition_run(run["id"], "needs_attention", question="Continue?")
    replied = await management.operation(
        "job_run.reply", {"run_id": run["id"], "text": "Yes, proceed"}
    )
    assert replied is not None
    assert replied["run"]["state"] == "running"
    assert service.job_execution.replied == [(run["id"], "Yes, proceed")]

    cancelled = await management.operation("job_run.cancel", {"run_id": run["id"]})
    assert cancelled is not None
    assert cancelled["run"]["state"] == "cancelled"
    assert service.job_execution.cancelled == [run["id"]]


async def test_dashboard_get_aggregates_state(factory_store: FactoryStore, tmp_path: Path) -> None:
    service, management, factory, leader, worker = await _setup(factory_store, tmp_path)
    assert service.host.triggers is not None

    job_res = await management.operation(
        "job.create",
        {
            "factory_id": factory["id"],
            "name": "Monitor",
            "prompt": "Check metrics",
            "agent_member_id": worker["id"],
            "triggers": [
                {
                    "id": "sched_1",
                    "type": "schedule",
                    "title": "Hourly",
                    "schedule": "0 * * * *",
                    "enabled": True,
                }
            ],
        },
    )
    assert job_res is not None
    job = job_res["job"]

    # Set next_fire_at on the fake host trigger so upcoming() returns it.
    for item in service.host.triggers.items.values():
        item["next_fire_at"] = "2026-11-25T01:00:00Z"

    r_queued = await factory_store.jobs.create_run(factory["id"], job["id"], origin="manual")
    r_running = await factory_store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await factory_store.jobs.transition_run(r_running["id"], "running")
    r_attn = await factory_store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await factory_store.jobs.transition_run(r_attn["id"], "needs_attention", question="Which env?")
    r_failed = await factory_store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await factory_store.jobs.transition_run(r_failed["id"], "failed", reason="Boom")
    r_done = await factory_store.jobs.create_run(factory["id"], job["id"], origin="manual")
    await factory_store.jobs.transition_run(r_done["id"], "done", summary="All good")

    dash = await management.operation(
        "dashboard.get",
        {"factory_id": factory["id"], "recent_limit": 3, "upcoming_limit": 5},
    )
    assert dash is not None
    assert dash["factory"]["id"] == factory["id"]
    assert {m["id"] for m in dash["members"]} == {leader["id"], worker["id"]}
    assert len(dash["jobs"]) == 1
    assert dash["jobs"][0]["triggers"][0]["next_fire"] == "2026-11-25T01:00:00Z"
    assert {r["id"] for r in dash["needs_attention"]} == {r_attn["id"], r_failed["id"]}
    assert {r["id"] for r in dash["running"]} == {r_running["id"], r_queued["id"]}
    assert len(dash["upcoming"]) == 1
    assert dash["upcoming"][0]["job_id"] == job["id"]
    assert len(dash["recent_runs"]) == 3


@pytest.mark.asyncio
async def test_dashboard_get_enriches_member_profiles_and_member_get_registered(
    tmp_path: Path, factory_store: FactoryStore
) -> None:
    from types import SimpleNamespace

    from resources.extensions.factory._native_values import OPERATION_SCHEMAS
    from tests.resources.extensions.factory.factory_test_support import build_factory

    assert "member.get" in OPERATION_SCHEMAS

    harness = await build_factory(tmp_path, factory_store)

    async def get_identity(agent_id: str) -> Json:
        return {
            "agent_id": agent_id,
            "incarnation": "p1",
            "name": "Leader",
            "model": "test/worker",
            "placement": "project",
            "created_by_owner": False,
        }

    async def get_profiles(agent_ids: list[str]) -> dict[str, Json]:
        return {agent_id: await get_identity(agent_id) for agent_id in agent_ids}

    harness.host.identity_agents = SimpleNamespace(  # type: ignore[attr-defined]
        get=get_identity,
        profiles=get_profiles,
    )
    factory = await factory_store.native.create_factory(
        "project", leader_agent_id="leader", name="Dash Team", leader_agent_proof="p1"
    )
    harness.service.job_triggers = FactoryJobTriggers(harness.service)  # type: ignore[attr-defined]
    mgmt = FactoryJobManagement(harness.service)
    dash = await mgmt.operation("dashboard.get", {"factory_id": factory["id"]})
    assert dash is not None
    assert "leader_available" in dash["factory"]
    assert isinstance(dash["factory"]["leader_available"], bool)
    assert len(dash["members"]) == 1
    assert "profile_available" in dash["members"][0]
