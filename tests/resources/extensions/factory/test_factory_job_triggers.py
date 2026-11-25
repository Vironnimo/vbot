"""Factory Job triggers: validation, host sync, enrichment, upcoming, secrets and fires."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from resources.extensions.factory._job_triggers import (
    FactoryJobTriggers,
    condense,
    trigger_key,
)
from resources.extensions.factory.store import FactoryError
from tests.resources.extensions.factory.factory_test_support import FakeTriggers

type Json = dict[str, Any]


class _FakeJobsStore:
    def __init__(self, jobs: list[Json] | None = None) -> None:
        self.jobs: dict[str, Json] = {job["id"]: job for job in (jobs or [])}
        self.receipts: set[str] = set()
        self.runs: list[Json] = []
        self._next_run = 1

    async def list_all_jobs(self) -> list[Json]:
        return list(self.jobs.values())

    async def get_job(self, job_id: str) -> Json:
        if job_id not in self.jobs:
            raise FactoryError("job_not_found", f"Job {job_id!r} not found.")
        return self.jobs[job_id]

    async def create_triggered_run(
        self,
        factory_id: str,
        job_id: str,
        *,
        origin: str,
        trigger_id: str,
        receipt: str,
        trigger_event: Json | None = None,
        input_note: str = "",
    ) -> Json | None:
        if receipt and receipt in self.receipts:
            return None
        if receipt:
            self.receipts.add(receipt)
        run = {
            "id": f"jrun_{self._next_run}",
            "factory_id": factory_id,
            "job_id": job_id,
            "origin": origin,
            "trigger_id": trigger_id,
            "receipt": receipt,
            "trigger_event": trigger_event,
            "input_note": input_note,
            "revision": 1,
        }
        self._next_run += 1
        self.runs.append(run)
        return run


class _FakeRecords:
    def __init__(self, jobs: _FakeJobsStore) -> None:
        self.jobs = jobs


class _FakeHost:
    def __init__(self) -> None:
        self.triggers: FakeTriggers | None = FakeTriggers()
        self.published: list[tuple[str, list[str], int]] = []

    def publish_change(self, resource: str, ids: list[str], revision: int) -> None:
        self.published.append((resource, ids, revision))


class _FakeService:
    def __init__(self, jobs: list[Json] | None = None) -> None:
        self.host: _FakeHost | None = _FakeHost()
        self.lock = asyncio.Lock()
        self.jobs_store = _FakeJobsStore(jobs)
        self._records = _FakeRecords(self.jobs_store)
        self.schedule_requests = 0

    def records(self) -> _FakeRecords:
        return self._records

    def request_schedule(self) -> None:
        self.schedule_requests += 1


def _job(
    job_id: str = "job1",
    *,
    factory_id: str = "fac1",
    factory_state: str = "active",
    name: str = "Nightly",
    enabled: bool = True,
    agent_member_id: str = "mem1",
    triggers: list[Json] | None = None,
) -> Json:
    return {
        "id": job_id,
        "factory_id": factory_id,
        "factory_state": factory_state,
        "name": name,
        "enabled": enabled,
        "agent_member_id": agent_member_id,
        "prompt": "Run checks.",
        "description": "Check repo",
        "triggers": triggers
        if triggers is not None
        else [
            {
                "id": "sched1",
                "type": "schedule",
                "title": "Daily",
                "schedule": "0 9 * * *",
                "enabled": True,
            },
            {
                "id": "hook1",
                "type": "webhook",
                "title": "Push",
                "schedule": "",
                "enabled": True,
            },
        ],
    }


def test_trigger_key_format() -> None:
    assert trigger_key("fac1", "job1", "trg1") == "job:fac1:job1:trg1"


def test_check_validates_schedules_and_requires_host_triggers() -> None:
    service = _FakeService()
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]
    jt.check(_job())

    bad = _job(
        triggers=[
            {
                "id": "t1",
                "type": "schedule",
                "title": "Bad",
                "schedule": "bad cron",
                "enabled": True,
            }
        ]
    )
    with pytest.raises(FactoryError) as invalid:
        jt.check(bad)
    assert invalid.value.code == "invalid_schedule"

    assert service.host is not None
    service.host.triggers = None
    with pytest.raises(FactoryError) as unavailable:
        jt.check(_job())
    assert unavailable.value.code == "triggers_unavailable"


@pytest.mark.asyncio
async def test_sync_creates_updates_deletes_and_cleans_legacy_triggers() -> None:
    active = _job("job1")
    paused_job = _job(
        "job2",
        enabled=False,
        triggers=[
            {
                "id": "t2",
                "type": "schedule",
                "title": "Hourly",
                "schedule": "0 * * * *",
                "enabled": True,
            }
        ],
    )
    archived = _job(
        "job3",
        factory_state="archived",
        triggers=[
            {
                "id": "t3",
                "type": "schedule",
                "title": "Arch",
                "schedule": "0 1 * * *",
                "enabled": True,
            }
        ],
    )
    service = _FakeService([active, paused_job, archived])
    assert service.host is not None and service.host.triggers is not None
    host_trgs = service.host.triggers
    # Seed a legacy Workflow trigger and an obsolete job trigger.
    await host_trgs.create("legacy_wf:trg", name="Legacy", schedule="0 8 * * *", enabled=True)
    await host_trgs.create("job:fac1:deleted:t1", name="Old", schedule="0 8 * * *", enabled=True)

    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]
    await jt.sync()

    assert set(host_trgs.items) == {
        "job:fac1:job1:sched1",
        "job:fac1:job1:hook1",
        "job:fac1:job2:t2",
        "job:fac1:job3:t3",
    }
    assert host_trgs.items["job:fac1:job1:sched1"]["enabled"] is True
    assert host_trgs.items["job:fac1:job1:hook1"]["schedule"] == "webhook"
    assert host_trgs.items["job:fac1:job2:t2"]["enabled"] is False
    assert host_trgs.items["job:fac1:job3:t3"]["enabled"] is False

    # Webhook URL/id preserved when paused; changed type recreates it.
    hook_id = host_trgs.items["job:fac1:job1:hook1"]["id"]
    active["enabled"] = False
    await jt.sync()
    assert host_trgs.items["job:fac1:job1:hook1"]["id"] == hook_id
    assert host_trgs.items["job:fac1:job1:hook1"]["enabled"] is False

    active["enabled"] = True
    active["triggers"][1]["type"] = "schedule"
    active["triggers"][1]["schedule"] = "0 12 * * *"
    await jt.sync()
    assert host_trgs.items["job:fac1:job1:hook1"]["id"] != hook_id
    assert host_trgs.items["job:fac1:job1:hook1"]["schedule"] == "0 12 * * *"


@pytest.mark.asyncio
async def test_sync_preserves_completed_and_missed_oneshots_unless_schedule_changes() -> None:
    job = _job(
        triggers=[
            {
                "id": "once",
                "type": "schedule",
                "title": "Once",
                "schedule": "in 1h",
                "enabled": True,
            }
        ]
    )
    service = _FakeService([job])
    assert service.host is not None and service.host.triggers is not None
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]
    await jt.sync()
    item = service.host.triggers.items["job:fac1:job1:once"]
    item.update(status="completed", enabled=False)
    old_id = item["id"]

    await jt.sync()
    assert item["id"] == old_id and item["status"] == "completed" and item["enabled"] is False

    job["triggers"][0]["schedule"] = "every 1h"
    await jt.sync()
    new_item = service.host.triggers.items["job:fac1:job1:once"]
    assert new_item["id"] != old_id and new_item["status"] == "active"


@pytest.mark.asyncio
async def test_enrich_job_and_secret_read_rotate() -> None:
    job = _job()
    service = _FakeService([job])
    assert service.host is not None and service.host.triggers is not None
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]
    await jt.sync()
    service.host.triggers.items["job:fac1:job1:sched1"].update(
        next_fire_at="2026-11-25T09:00:00Z",
        last_fired_at="2026-11-24T09:00:00Z",
        last_error="boom",
    )

    enriched = jt.enrich_job(job)
    assert "status" not in job["triggers"][0]
    sched, hook = enriched["triggers"]
    assert sched["status"] == "active"
    assert sched["next_fire"] == "2026-11-25T09:00:00Z"
    assert sched["last_fire"] == "2026-11-24T09:00:00Z"
    assert sched["last_error"] == "boom"
    assert sched["webhook_url"] is None and sched["webhook_path"] is None

    hook_id = service.host.triggers.items["job:fac1:job1:hook1"]["id"]
    assert hook["webhook_url"] == f"http://vbot/hooks/{hook_id}"
    assert hook["webhook_path"] == hook_id

    assert await jt.secret("fac1", "job1", "hook1") == {"secret": f"secret-{hook_id}"}
    assert await jt.secret("fac1", "job1", "hook1", rotate=True) == {
        "secret": f"secret-{hook_id}-rotated"
    }
    with pytest.raises(FactoryError) as err:
        await jt.secret("fac1", "job1", "sched1")
    assert err.value.code == "trigger_not_found"


@pytest.mark.asyncio
async def test_upcoming_sorts_by_next_fire_and_filters_factory() -> None:
    j1 = _job(
        "j1",
        name="Later",
        agent_member_id="m1",
        triggers=[
            {
                "id": "t1",
                "type": "schedule",
                "title": "T1",
                "schedule": "0 10 * * *",
                "enabled": True,
            },
            {"id": "t2", "type": "webhook", "title": "Hook", "schedule": "", "enabled": True},
        ],
    )
    j2 = _job(
        "j2",
        name="Sooner",
        agent_member_id="m2",
        triggers=[
            {
                "id": "t1",
                "type": "schedule",
                "title": "T2",
                "schedule": "0 8 * * *",
                "enabled": True,
            },
        ],
    )
    other_factory = _job(
        "j3",
        factory_id="fac2",
        triggers=[
            {
                "id": "t1",
                "type": "schedule",
                "title": "Other",
                "schedule": "0 7 * * *",
                "enabled": True,
            },
        ],
    )
    service = _FakeService([j1, j2, other_factory])
    assert service.host is not None and service.host.triggers is not None
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]
    await jt.sync()
    service.host.triggers.items["job:fac1:j1:t1"]["next_fire_at"] = "2026-11-25T10:00:00Z"
    service.host.triggers.items["job:fac1:j2:t1"]["next_fire_at"] = "2026-11-25T08:00:00Z"
    service.host.triggers.items["job:fac2:j3:t1"]["next_fire_at"] = "2026-11-25T07:00:00Z"

    upcoming = await jt.upcoming("fac1", limit=10)
    assert upcoming == [
        {
            "job_id": "j2",
            "job_name": "Sooner",
            "trigger_id": "t1",
            "trigger_title": "T2",
            "schedule": "0 8 * * *",
            "next_fire": "2026-11-25T08:00:00Z",
            "agent_member_id": "m2",
        },
        {
            "job_id": "j1",
            "job_name": "Later",
            "trigger_id": "t1",
            "trigger_title": "T1",
            "schedule": "0 10 * * *",
            "next_fire": "2026-11-25T10:00:00Z",
            "agent_member_id": "m1",
        },
    ]


@pytest.mark.asyncio
async def test_schedule_fire_creates_run_with_missed_count_and_deduplicates() -> None:
    job = _job()
    service = _FakeService([job])
    assert service.host is not None
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]

    fire_event = {
        "key": "job:fac1:job1:sched1",
        "trigger_id": "cron-1",
        "fired_at": "2026-11-25T09:00:05Z",
        "due_at": "2026-11-25T09:00:00Z",
        "earlier_missed": 2,
        "requests": [],
    }
    await jt.fire(fire_event)
    assert len(service.jobs_store.runs) == 1
    run = service.jobs_store.runs[0]
    assert run["origin"] == "schedule"
    assert run["trigger_id"] == "sched1"
    assert run["receipt"] == "cron-1:due:2026-11-25T09:00:00Z"
    assert run["trigger_event"] == {
        "factory_id": "fac1",
        "job_id": "job1",
        "trigger_id": "sched1",
        "type": "schedule",
        "fired_at": "2026-11-25T09:00:05Z",
        "due_at": "2026-11-25T09:00:00Z",
        "missed": 2,
    }
    assert service.schedule_requests == 1
    assert service.host.published == [("job_runs", [run["id"]], 1)]

    # Retried fire with same receipt is deduplicated.
    await jt.fire(fire_event)
    assert len(service.jobs_store.runs) == 1
    assert service.schedule_requests == 1


@pytest.mark.asyncio
async def test_webhook_fire_condenses_limits_to_20_and_deduplicates() -> None:
    job = _job()
    service = _FakeService([job])
    assert service.host is not None
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]

    requests = [
        {
            "seq": idx,
            "received_at": f"2026-11-25T09:00:{idx:02d}Z",
            "headers": {"x-github-event": "push", "x-hub-signature-256": "secret"},
            "body": {
                "ref": "refs/heads/main",
                "repository": {"full_name": "octo/app"},
                "commits": [{"id": f"c{i}", "message": f"m{i}", "url": "u"} for i in range(25)],
            },
        }
        for idx in range(23)
    ]
    fire_event = {
        "key": "job:fac1:job1:hook1",
        "trigger_id": "cron-hook",
        "fired_at": "2026-11-25T09:01:00Z",
        "requests": requests,
        "dropped": 2,
    }
    await jt.fire(fire_event)
    assert len(service.jobs_store.runs) == 20
    first = service.jobs_store.runs[0]
    assert first["receipt"] == "cron-hook:request:3"
    assert first["trigger_event"]["skipped_requests"] == 5
    req = first["trigger_event"]["request"]
    assert req["event"] == "push"
    assert len(req["commits"]) == 20 and req["more_commits"] == 5
    assert "x-hub-signature-256" not in str(req)
    assert service.schedule_requests == 1
    assert len(service.host.published) == 20

    # Retrying the same batch creates no duplicates.
    await jt.fire(fire_event)
    assert len(service.jobs_store.runs) == 20


def test_condense_non_dict_and_unknown_dict_body() -> None:
    assert condense({"x-custom-event": "ping"}, "hello") == {"event": "ping", "body": "hello"}
    assert condense({}, {"foo": "bar"}) == {"body": '{"foo": "bar"}'}


@pytest.mark.asyncio
async def test_webhook_fire_origin_fallback_receipt_and_paused_factory() -> None:
    job = _job(
        triggers=[
            {
                "id": "hook1",
                "type": "webhook",
                "title": "Hook",
                "schedule": "",
                "enabled": True,
            }
        ]
    )
    service = _FakeService([job])
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]

    # Missing trigger_id / seq still produces a non-empty deterministic receipt and webhook origin.
    await jt.fire(
        {
            "key": "job:fac1:job1:hook1",
            "fired_at": "2026-11-25T10:00:00Z",
            "requests": [{"headers": {}, "body": {"ok": True}}],
        }
    )
    assert len(service.jobs_store.runs) == 1
    assert service.jobs_store.runs[0]["origin"] == "webhook"
    assert service.jobs_store.runs[0]["receipt"]

    # Paused/archived Factory from native store is skipped on fire.
    class _PausedNative:
        async def get_factory(self, _factory_id: str) -> Json:
            return {"id": "fac1", "state": "paused"}

    service.records().native = _PausedNative()  # type: ignore[attr-defined]
    await jt.fire(
        {
            "key": "job:fac1:job1:hook1",
            "fired_at": "2026-11-25T10:01:00Z",
            "requests": [{"seq": 99, "headers": {}, "body": {"ok": True}}],
        }
    )
    assert len(service.jobs_store.runs) == 1


@pytest.mark.asyncio
async def test_sync_recreates_on_schedule_to_webhook_transition() -> None:
    job = _job(
        triggers=[
            {
                "id": "t1",
                "type": "schedule",
                "title": "Cron",
                "schedule": "0 * * * *",
                "enabled": True,
            }
        ]
    )
    service = _FakeService([job])
    jt = FactoryJobTriggers(service)  # type: ignore[arg-type]
    await jt.sync()
    first_id = service.host.triggers.list()[0]["id"]  # type: ignore[union-attr]

    job["triggers"] = [
        {"id": "t1", "type": "webhook", "title": "Hook", "schedule": "", "enabled": True}
    ]
    await jt.sync()
    items = service.host.triggers.list()  # type: ignore[union-attr]
    assert len(items) == 1
    assert items[0]["id"] != first_id
    assert items[0]["schedule_type"] == "webhook"
