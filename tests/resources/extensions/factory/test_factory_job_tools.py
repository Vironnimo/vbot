"""Tests for Simplified Factory Jobs agent texts and private session Tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.runs import RunExecutionOwner
from resources.extensions.factory._job_agent_text import (
    JOB_PRIVATE_TOOLS,
    JOB_STATUS_TOOL_NAME,
    LEADER_JOBS_TOOL_NAME,
    build_job_run_instructions,
    build_job_run_message,
    format_leader_jobs_context,
)
from resources.extensions.factory._job_tools import FactoryJobTools
from resources.extensions.factory._job_triggers import FactoryJobTriggers
from resources.extensions.factory.store import FactoryStore
from tests.resources.extensions.factory.factory_test_support import build_factory

type Json = dict[str, Any]


async def _setup_factory_and_tools(tmp_path: Path, factory_store: FactoryStore):
    harness = await build_factory(tmp_path, factory_store)
    harness.service.job_triggers = FactoryJobTriggers(harness.service)  # type: ignore[attr-defined]
    reported: list[str] = []
    harness.service.job_execution = SimpleNamespace(  # type: ignore[attr-defined]
        mark_reported=lambda run_id: reported.append(run_id)
    )
    native = factory_store.native
    factory = await native.create_factory(
        "project",
        name="Alpha Factory",
        remit="Ship clean code.",
        leader_agent_id="leader",
        leader_agent_proof="p1",
    )
    worker = await native.add_member(
        factory["id"], "worker", name="Builder", role="builder", agent_proof="p2"
    )
    topic = await native.create_topic(
        factory["id"],
        "Leader chat",
        participants=[factory["leader_member_id"], worker["id"]],
    )
    leader_exec = await native.create_execution(
        factory["id"],
        "topic",
        topic["id"],
        factory["leader_member_id"],
        1,
        group_id="",
        participant_id="leader",
    )
    leader_group = f"native.{leader_exec['id']}"
    await native.change_execution(leader_exec["id"], {"group_id": leader_group, "state": "running"})
    worker_exec = await native.create_execution(
        factory["id"],
        "topic",
        topic["id"],
        worker["id"],
        1,
        group_id="",
        participant_id="worker",
    )
    worker_group = f"native.{worker_exec['id']}"
    await native.change_execution(worker_exec["id"], {"group_id": worker_group, "state": "running"})
    tools = FactoryJobTools(harness.service)
    return harness, tools, factory, worker, leader_group, worker_group, reported


def _owner(group_id: str, participant_id: str = "worker") -> RunExecutionOwner:
    return RunExecutionOwner(
        extension="factory",
        group_id=group_id,
        participant_id=participant_id,
        generation_id="gen-1",
        epoch="1",
    )


def _job_context(harness: Any, group_id: str, *, turn_ends: list[str] | None = None):
    ctx = harness.context(
        tool_name=JOB_STATUS_TOOL_NAME,
        execution_owner=_owner(group_id),
    )
    if turn_ends is not None:
        ctx = harness.context(
            tool_name=JOB_STATUS_TOOL_NAME,
            execution_owner=_owner(group_id),
            request_turn_end_hook=lambda call_id: turn_ends.append(call_id),
        )
    return ctx


@pytest.mark.asyncio
async def test_factory_job_status_transitions_and_refusals(
    factory_store: FactoryStore, tmp_path: Path
) -> None:
    (
        harness,
        tools,
        factory,
        _worker,
        leader_group,
        _worker_group,
        reported,
    ) = await _setup_factory_and_tools(tmp_path, factory_store)
    jobs_store = factory_store.jobs
    job = await jobs_store.create_job(factory["id"], name="Nightly check", prompt="Run pytest")

    # Non-job binding is refused cleanly.
    non_job = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        harness.context(tool_name=JOB_STATUS_TOOL_NAME),
        {"status": "done"},
    )
    assert non_job["ok"] is False
    assert non_job["error"]["code"] == "job_run_required"

    native_binding = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        _job_context(harness, leader_group),
        {"status": "done"},
    )
    assert native_binding["ok"] is False
    assert native_binding["error"]["code"] == "job_run_required"

    # needs_attention requires question.
    run1 = await jobs_store.create_run(factory["id"], job["id"], origin="manual")
    await jobs_store.transition_run(run1["id"], "running")
    missing_q = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        _job_context(harness, run1["group_id"]),
        {"status": "needs_attention"},
    )
    assert missing_q["ok"] is False
    assert missing_q["error"]["code"] == "invalid_arguments"

    turn_ends: list[str] = []
    ctx1 = _job_context(harness, run1["group_id"], turn_ends=turn_ends)
    attn = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        ctx1,
        {
            "status": "needs_attention",
            "question": "Approve deployment to staging?",
            "summary": "Tests passed; awaiting approval.",
        },
    )
    assert attn["ok"] is True
    assert attn["data"]["run"]["state"] == "needs_attention"
    assert attn["data"]["run"]["question"] == "Approve deployment to staging?"
    assert attn["data"]["run"]["summary"] == "Tests passed; awaiting approval."
    assert ctx1._turn_end_requested is True
    assert run1["id"] in reported
    assert any(n["key"] == run1["id"] for n in harness.host.notifications)

    # failed requires reason.
    run2 = await jobs_store.create_run(factory["id"], job["id"], origin="manual")
    await jobs_store.transition_run(run2["id"], "running")
    missing_r = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        _job_context(harness, run2["group_id"]),
        {"status": "failed"},
    )
    assert missing_r["ok"] is False
    assert missing_r["error"]["code"] == "invalid_arguments"

    ctx2 = _job_context(harness, run2["group_id"], turn_ends=turn_ends)
    failed = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        ctx2,
        {"status": "failed", "reason": "Database migration failed."},
    )
    assert failed["ok"] is True
    assert failed["data"]["run"]["state"] == "failed"
    assert failed["data"]["run"]["reason"] == "Database migration failed."
    assert ctx2._turn_end_requested is True

    # done with summary.
    run3 = await jobs_store.create_run(factory["id"], job["id"], origin="manual")
    await jobs_store.transition_run(run3["id"], "running")
    ctx3 = _job_context(harness, run3["group_id"], turn_ends=turn_ends)
    done = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        ctx3,
        {"status": "done", "summary": "All 42 tests passed."},
    )
    assert done["ok"] is True
    assert done["data"]["run"]["state"] == "done"
    assert done["data"]["run"]["summary"] == "All 42 tests passed."
    assert ctx3._turn_end_requested is True

    # Already finished run is refused.
    again = await tools.handle(
        JOB_STATUS_TOOL_NAME,
        _job_context(harness, run3["group_id"]),
        {"status": "done"},
    )
    assert again["ok"] is False
    assert again["error"]["code"] == "job_run_finished"


@pytest.mark.asyncio
async def test_factory_jobs_crud_trigger_and_non_leader_refusal(
    factory_store: FactoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        harness,
        tools,
        _factory,
        worker,
        leader_group,
        worker_group,
        _reported,
    ) = await _setup_factory_and_tools(tmp_path, factory_store)
    schedule_calls = 0

    def _request_schedule() -> None:
        nonlocal schedule_calls
        schedule_calls += 1

    monkeypatch.setattr(harness.service, "request_schedule", _request_schedule)

    leader_ctx = harness.context(
        tool_name=LEADER_JOBS_TOOL_NAME,
        execution_owner=_owner(leader_group, "leader"),
    )
    worker_ctx = harness.context(
        tool_name=LEADER_JOBS_TOOL_NAME,
        execution_owner=_owner(worker_group, "worker"),
    )

    # Non-leader and unbound callers are refused.
    refused_worker = await tools.handle(LEADER_JOBS_TOOL_NAME, worker_ctx, {"action": "list"})
    assert refused_worker["ok"] is False
    assert refused_worker["error"]["code"] == "leader_required"

    refused_unbound = await tools.handle(
        LEADER_JOBS_TOOL_NAME, harness.context(tool_name=LEADER_JOBS_TOOL_NAME), {"action": "list"}
    )
    assert refused_unbound["ok"] is False
    assert refused_unbound["error"]["code"] == "leader_required"

    # Invalid schedule trigger is rejected without persisting.
    bad_create = await tools.handle(
        LEADER_JOBS_TOOL_NAME,
        leader_ctx,
        {
            "action": "create",
            "name": "Bad cron",
            "prompt": "Check",
            "triggers": [{"type": "schedule", "schedule": "bad-cron", "enabled": True}],
        },
    )
    assert bad_create["ok"] is False
    assert bad_create["error"]["code"] == "invalid_schedule"
    assert await factory_store.jobs.list_jobs(_factory["id"]) == []

    # Create job with valid schedule trigger.
    created = await tools.handle(
        LEADER_JOBS_TOOL_NAME,
        leader_ctx,
        {
            "action": "create",
            "name": "Nightly Audit",
            "prompt": "Audit dependencies.",
            "description": "When asked to audit deps",
            "triggers": [{"type": "schedule", "schedule": "0 9 * * *", "title": "Daily"}],
        },
    )
    assert created["ok"] is True
    job = created["data"]["job"]
    assert job["name"] == "Nightly Audit"
    assert len(harness.host.triggers.list()) == 1

    # List and get by id & name.
    listed = await tools.handle(LEADER_JOBS_TOOL_NAME, leader_ctx, {"action": "list"})
    assert listed["ok"] is True
    assert len(listed["data"]["jobs"]) == 1

    by_id = await tools.handle(
        LEADER_JOBS_TOOL_NAME, leader_ctx, {"action": "get", "job_id": job["id"]}
    )
    assert by_id["ok"] is True
    assert by_id["data"]["job"]["id"] == job["id"]

    by_name = await tools.handle(
        LEADER_JOBS_TOOL_NAME, leader_ctx, {"action": "get", "name": "Nightly Audit"}
    )
    assert by_name["ok"] is True
    assert by_name["data"]["job"]["id"] == job["id"]

    # Update job.
    updated = await tools.handle(
        LEADER_JOBS_TOOL_NAME,
        leader_ctx,
        {"action": "update", "job_id": job["id"], "description": "Updated description"},
    )
    assert updated["ok"] is True
    assert updated["data"]["job"]["description"] == "Updated description"

    # Trigger by id with default member, and by name with member role override.
    trig1 = await tools.handle(
        LEADER_JOBS_TOOL_NAME,
        leader_ctx,
        {"action": "trigger", "job_id": job["id"], "input_note": "Focus on auth."},
    )
    assert trig1["ok"] is True
    assert trig1["data"]["run"]["origin"] == "leader"
    assert trig1["data"]["run"]["input_note"] == "Focus on auth."
    assert trig1["data"]["run"]["member_id"] == _factory["leader_member_id"]

    trig2 = await tools.handle(
        LEADER_JOBS_TOOL_NAME,
        leader_ctx,
        {"action": "trigger", "name": "Nightly Audit", "member_id": "builder"},
    )
    assert trig2["ok"] is True
    assert trig2["data"]["run"]["member_id"] == worker["id"]
    assert schedule_calls == 2

    # List runs bounded by limit.
    runs = await tools.handle(
        LEADER_JOBS_TOOL_NAME,
        leader_ctx,
        {"action": "runs", "job_id": job["id"], "limit": 1},
    )
    assert runs["ok"] is True
    assert len(runs["data"]["runs"]) == 1

    # Delete job and sync triggers.
    deleted = await tools.handle(
        LEADER_JOBS_TOOL_NAME, leader_ctx, {"action": "delete", "job_id": job["id"]}
    )
    assert deleted["ok"] is True
    assert deleted["data"]["deleted_job_id"] == job["id"]
    assert harness.host.triggers.list() == []


def test_job_agent_text_helpers() -> None:
    assert set(JOB_PRIVATE_TOOLS) == {JOB_STATUS_TOOL_NAME, LEADER_JOBS_TOOL_NAME}
    assert format_leader_jobs_context([]) == ""

    catalog = format_leader_jobs_context(
        [
            {
                "id": "fjob_1",
                "name": "Deploy Check",
                "description": "Verify staging health.",
                "enabled": True,
                "agent_member_id": "",
                "triggers": [{"type": "schedule", "schedule": "0 * * * *", "enabled": True}],
            },
            {
                "id": "fjob_2",
                "name": "PR Triage",
                "description": "",
                "enabled": False,
                "agent_member_id": "mem_2",
                "triggers": [],
            },
        ]
    )
    assert "Deploy Check (id: fjob_1)" in catalog
    assert "Leader (default)" in catalog
    assert "schedule (0 * * * *)" in catalog
    assert "Verify staging health." in catalog
    assert "PR Triage (id: fjob_2 [disabled])" in catalog
    assert "mem_2" in catalog
    assert "manual only" in catalog

    instructions = build_job_run_instructions(
        {"id": "fac_1", "name": "Alpha", "remit": "Build quality software."},
        {"id": "mem_1", "name": "Worker", "role": "engineer", "instructions": "Run tests."},
        {"id": "jrun_1"},
    )
    assert "Alpha" in instructions
    assert "Build quality software." in instructions
    assert "factory_job_status" in instructions

    message = build_job_run_message(
        {
            "job_name": "PR Triage",
            "prompt": "Check open PRs.",
            "input_note": "Prioritize security.",
            "trigger_event": {"event": "pull_request", "action": "opened"},
        }
    )
    assert "Job: PR Triage" in message
    assert "Check open PRs." in message
    assert "Prioritize security." in message
    assert "pull_request" in message
