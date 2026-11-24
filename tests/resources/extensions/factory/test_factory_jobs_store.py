"""Contracts for Simplified Software Factory Jobs, Runs, and Trigger Receipts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.database import open_offline_database
from core.extensions.databases import extension_database_spec
from resources.extensions.factory._jobs_schema import SCHEMA_SQL as JOBS_SCHEMA_SQL
from resources.extensions.factory.store import (
    DATABASE_NAME,
    MIGRATIONS,
    SCHEMA_SQL,
    FactoryError,
    FactoryStore,
)

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


async def _team(store: FactoryStore, project: str = "project") -> tuple[Json, Json, Json]:
    factory = await store.native.create_factory(
        project, leader_agent_id="leader", leader_agent_proof="inc-1"
    )
    leader = await store.native.get_member(factory["leader_member_id"])
    worker = await store.native.add_member(
        factory["id"], "worker", agent_proof="inc-2", role="Implementer"
    )
    return factory, leader, worker


async def test_job_crud_validation_and_revision_conflicts(factory_store: FactoryStore) -> None:
    factory, _, worker = await _team(factory_store)
    jobs = factory_store.jobs

    with pytest.raises(FactoryError) as exc:
        await jobs.create_job(factory["id"], name="   ", prompt="Do work")
    assert exc.value.code == "invalid_job"

    with pytest.raises(FactoryError) as exc:
        await jobs.create_job(factory["id"], name="x" * 201, prompt="Do work")
    assert exc.value.code == "invalid_job"

    with pytest.raises(FactoryError) as exc:
        await jobs.create_job(factory["id"], name="Nightly", prompt="   ")
    assert exc.value.code == "invalid_job"

    with pytest.raises(FactoryError) as exc:
        await jobs.create_job(
            factory["id"],
            name="Nightly",
            prompt="Run tests",
            agent_member_id="fmb_missing",
        )
    assert exc.value.code == "member_unavailable"

    created = await jobs.create_job(
        factory["id"],
        name="  Nightly Suite  ",
        prompt="  Run full test suite  ",
        description="Runs every night",
        agent_member_id=worker["id"],
        triggers=[
            {"type": "schedule", "title": "Midnight", "schedule": "0 0 * * *", "enabled": True},
            {
                "id": "trg_custom",
                "type": "webhook",
                "title": "Hook",
                "schedule": "",
                "enabled": False,
            },
        ],
    )
    assert created["id"].startswith("fjob_")
    assert created["name"] == "Nightly Suite"
    assert created["prompt"] == "Run full test suite"
    assert created["description"] == "Runs every night"
    assert created["enabled"] is True
    assert created["agent_member_id"] == worker["id"]
    assert created["revision"] == 1
    assert len(created["triggers"]) == 2
    assert created["triggers"][0]["id"].startswith("trg_")
    assert created["triggers"][1]["id"] == "trg_custom"

    fetched = await jobs.get_job(created["id"])
    assert fetched == created

    listed = await jobs.list_jobs(factory["id"])
    assert listed == [created]

    all_jobs = await jobs.list_all_jobs()
    assert len(all_jobs) == 1
    assert all_jobs[0]["id"] == created["id"]
    assert all_jobs[0]["factory_state"] == "active"

    updated = await jobs.update_job(
        created["id"],
        name="Updated Nightly",
        enabled=False,
        expected_revision=created["revision"],
    )
    assert updated["revision"] == 2
    assert updated["name"] == "Updated Nightly"
    assert updated["enabled"] is False
    assert updated["prompt"] == "Run full test suite"

    with pytest.raises(FactoryError) as conflict:
        await jobs.update_job(
            created["id"],
            name="Stale",
            expected_revision=created["revision"],
        )
    assert conflict.value.code == "revision_conflict"

    await jobs.delete_job(created["id"])
    assert await jobs.list_jobs(factory["id"]) == []
    with pytest.raises(FactoryError) as missing:
        await jobs.get_job(created["id"])
    assert missing.value.code == "not_found"


async def test_run_member_resolution_and_fallbacks(factory_store: FactoryStore) -> None:
    factory, leader, worker = await _team(factory_store, "proj-a")
    other_factory, _, other_worker = await _team(factory_store, "proj-b")
    jobs = factory_store.jobs

    job_default = await jobs.create_job(factory["id"], name="Leader Job", prompt="Triage")
    job_assigned = await jobs.create_job(
        factory["id"],
        name="Worker Job",
        prompt="Implement",
        agent_member_id=worker["id"],
    )

    # 1. Leader fallback when job has no agent_member_id
    run_leader = await jobs.create_run(factory["id"], job_default["id"], origin="manual")
    assert run_leader["member_id"] == leader["id"]
    assert run_leader["group_id"] == f"fjob.{run_leader['id']}"
    assert run_leader["participant_id"] == "worker"
    assert run_leader["state"] == "queued"

    # 2. Job's agent_member_id used when no explicit override
    run_worker = await jobs.create_run(factory["id"], job_assigned["id"], origin="leader")
    assert run_worker["member_id"] == worker["id"]

    # 3. Explicit member_id override wins over job's agent_member_id
    run_override = await jobs.create_run(
        factory["id"],
        job_assigned["id"],
        origin="public_tool",
        member_id=leader["id"],
        input_note="Urgent override",
    )
    assert run_override["member_id"] == leader["id"]
    assert run_override["input_note"] == "Urgent override"

    # Cross-factory or retired member fails with member_unavailable
    with pytest.raises(FactoryError) as cross:
        await jobs.create_run(
            factory["id"],
            job_default["id"],
            origin="manual",
            member_id=other_worker["id"],
        )
    assert cross.value.code == "member_unavailable"

    await factory_store.native.update_member(worker["id"], {"state": "retired"})
    with pytest.raises(FactoryError) as retired:
        await jobs.create_run(factory["id"], job_assigned["id"], origin="manual")
    assert retired.value.code == "member_unavailable"

    # Archived factory rejects new jobs and runs
    await factory_store.native.update_factory(other_factory["id"], {"state": "archived"})
    with pytest.raises(FactoryError) as archived_job:
        await jobs.create_job(other_factory["id"], name="Blocked", prompt="Nope")
    assert archived_job.value.code == "factory_archived"
    with pytest.raises(FactoryError) as archived_run:
        await jobs.create_run(other_factory["id"], "", origin="manual", prompt="Nope")
    assert archived_run.value.code == "factory_archived"


async def test_run_transitions_queries_and_revisions(factory_store: FactoryStore) -> None:
    factory, _, worker = await _team(factory_store)
    jobs = factory_store.jobs
    job = await jobs.create_job(
        factory["id"], name="Build", prompt="Build app", agent_member_id=worker["id"]
    )
    run = await jobs.create_run(factory["id"], job["id"], origin="manual")
    assert run["id"].startswith("jrun_")
    assert run["finished_at"] is None

    running = await jobs.transition_run(
        run["id"],
        "running",
        session_address={"session_id": "ses_1", "group_id": run["group_id"]},
        expected_revision=run["revision"],
    )
    assert running["state"] == "running"
    assert running["revision"] == 2
    assert running["session_address"] == {"session_id": "ses_1", "group_id": run["group_id"]}
    assert running["finished_at"] is None

    attention = await jobs.transition_run(
        run["id"],
        "needs_attention",
        question="Approve deploy?",
        expected_revision=running["revision"],
    )
    assert attention["state"] == "needs_attention"
    assert attention["question"] == "Approve deploy?"
    assert attention["finished_at"] is None

    assert [item["id"] for item in await jobs.list_active_runs()] == [run["id"]]
    assert await jobs.get_run_by_group(run["group_id"]) == attention

    done = await jobs.transition_run(
        run["id"],
        "done",
        summary="Build succeeded",
        expected_revision=attention["revision"],
    )
    assert done["state"] == "done"
    assert done["summary"] == "Build succeeded"
    assert done["finished_at"] is not None
    assert await jobs.list_active_runs() == []

    with pytest.raises(FactoryError) as conflict:
        await jobs.update_run(run["id"], reason="stale", expected_revision=attention["revision"])
    assert conflict.value.code == "revision_conflict"

    assert [item["id"] for item in await jobs.list_runs(factory["id"], state="done")] == [run["id"]]
    assert await jobs.list_runs(factory["id"], state="queued") == []
    assert [item["id"] for item in await jobs.list_runs(factory["id"], job_id=job["id"])] == [
        run["id"]
    ]


async def test_atomic_trigger_receipt_deduplication(factory_store: FactoryStore) -> None:
    factory, _, worker = await _team(factory_store)
    jobs = factory_store.jobs
    job = await jobs.create_job(
        factory["id"],
        name="Webhook Job",
        prompt="Handle event",
        agent_member_id=worker["id"],
        triggers=[
            {"id": "trg_1", "type": "webhook", "title": "GH", "schedule": "", "enabled": True}
        ],
    )

    receipt = "wh:delivery-123"
    assert await jobs.has_trigger_receipt(receipt) is False

    first = await jobs.create_triggered_run(
        factory["id"],
        job["id"],
        origin="webhook",
        trigger_id="trg_1",
        receipt=receipt,
        trigger_event={"event": "push", "ref": "refs/heads/main"},
        input_note="Triggered by push",
    )
    assert first is not None
    assert first["origin"] == "webhook"
    assert first["trigger_id"] == "trg_1"
    assert first["trigger_event"] == {"event": "push", "ref": "refs/heads/main"}
    assert await jobs.has_trigger_receipt(receipt) is True

    duplicate = await jobs.create_triggered_run(
        factory["id"],
        job["id"],
        origin="webhook",
        trigger_id="trg_1",
        receipt=receipt,
    )
    assert duplicate is None
    assert len(await jobs.list_runs(factory["id"], job_id=job["id"])) == 1


async def test_jobs_schema_coexists_additively_with_legacy_tables(tmp_path: Path) -> None:
    before = extension_database_spec(
        tmp_path,
        "factory",
        DATABASE_NAME,
        SCHEMA_SQL.replace(JOBS_SCHEMA_SQL, ""),
        migrations=MIGRATIONS,
    )
    database = open_offline_database(before)
    try:
        old = FactoryStore(database)
        card = await old.create_card("project", "Legacy Card", "Keep me", origin="operator")
        factory = await old.native.create_factory(
            "project", leader_agent_id="leader", leader_agent_proof="inc-1"
        )
    finally:
        database.close()

    after = extension_database_spec(
        tmp_path, "factory", DATABASE_NAME, SCHEMA_SQL, migrations=MIGRATIONS
    )
    database = open_offline_database(after)
    try:
        store = FactoryStore(database)
        assert await store.get_card(card["id"]) == card
        assert (await store.native.get_factory(factory["id"]))["id"] == factory["id"]

        job = await store.jobs.create_job(factory["id"], name="New Job", prompt="Work")
        run = await store.jobs.create_run(factory["id"], job["id"], origin="manual")
        assert run["member_id"] == factory["leader_member_id"]
    finally:
        database.close()
