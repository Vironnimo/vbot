"""Tests for the public cross-Factory CRUD Tool (``factory``)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from resources.extensions.factory._job_triggers import FactoryJobTriggers
from resources.extensions.factory._public_tool import (
    PUBLIC_DESCRIPTION,
    PUBLIC_PARAMETERS,
    PUBLIC_TOOL_NAME,
    FactoryPublicTool,
)
from resources.extensions.factory.store import FactoryStore
from tests.resources.extensions.factory.factory_test_support import build_factory

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


async def _setup_public_tool(tmp_path: Path, factory_store: FactoryStore):
    harness = await build_factory(tmp_path, factory_store)
    harness.host.authority = "user"
    harness.service.job_triggers = FactoryJobTriggers(harness.service)  # type: ignore[attr-defined]

    created_agents: dict[str, Json] = {}

    async def create_identity(agent_id: str, **config: Any) -> Json:
        created_agents[agent_id] = {
            "agent_id": agent_id,
            "incarnation": f"proof-{agent_id}",
            "name": config["name"],
            "model": config.get("model") or "test/worker",
            "placement": "project",
            "created_by_owner": True,
        }
        return created_agents[agent_id]

    async def get_identity(agent_id: str) -> Json:
        if agent_id in created_agents:
            return created_agents[agent_id]
        return {
            "agent_id": agent_id,
            "incarnation": f"proof-{agent_id}",
            "name": agent_id,
            "model": "test/worker",
            "placement": "personal",
            "created_by_owner": False,
        }

    async def profiles(ids: list[str]) -> dict[str, Json]:
        return {agent_id: await get_identity(agent_id) for agent_id in ids}

    cast(Any, harness.host).identity_agents = SimpleNamespace(
        create=create_identity,
        get=get_identity,
        profiles=profiles,
    )
    tool = FactoryPublicTool(harness.service)
    return harness, tool


async def test_public_tool_metadata_and_list_get_by_factory_and_project(
    factory_store: FactoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert PUBLIC_TOOL_NAME == "factory"
    assert "Software Factories" in PUBLIC_DESCRIPTION
    assert "action" in PUBLIC_PARAMETERS["properties"]

    harness, tool = await _setup_public_tool(tmp_path, factory_store)
    monkeypatch.setattr(harness.service, "request_schedule", lambda: None)
    created = await tool.handle(
        harness.context(tool_call_id="c1"),
        {
            "action": "create_factory",
            "project_id": "project",
            "name": "Alpha Factory",
            "remit": "Build Alpha",
            "leader_name": "Alpha Leader",
            "leader_model": "test/worker",
        },
    )
    assert created["ok"] is True
    factory_id = created["data"]["factory"]["id"]

    listed = await tool.handle(harness.context(tool_call_id="c2"), {"action": "list_factories"})
    assert listed["ok"] is True
    assert [item["id"] for item in listed["data"]["factories"]] == [factory_id]

    by_id = await tool.handle(
        harness.context(tool_call_id="c3"),
        {"action": "get_factory", "factory_id": factory_id},
    )
    assert by_id["ok"] is True
    assert by_id["data"]["factory"]["id"] == factory_id
    assert by_id["data"]["factory"]["leader_available"] is True

    by_project = await tool.handle(
        harness.context(tool_call_id="c4"),
        {"action": "get_factory", "project_id": "project"},
    )
    assert by_project["ok"] is True
    assert by_project["data"]["factory"]["id"] == factory_id

    updated = await tool.handle(
        harness.context(tool_call_id="c5"),
        {
            "action": "update_factory",
            "project_id": "project",
            "remit": "Build Alpha cleanly",
        },
    )
    assert updated["ok"] is True
    assert updated["data"]["factory"]["remit"] == "Build Alpha cleanly"

    archived = await tool.handle(
        harness.context(tool_call_id="c6"),
        {"action": "archive_factory", "project_id": "project"},
    )
    assert archived["ok"] is True
    assert archived["data"]["factory"]["state"] == "archived"


async def test_create_factory_and_copy_specialists_and_jobs_across_projects(
    factory_store: FactoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, tool = await _setup_public_tool(tmp_path, factory_store)
    monkeypatch.setattr(harness.service, "request_schedule", lambda: None)

    # Create Factory A (project) with a specialist and a job.
    fa = await tool.handle(
        harness.context(tool_call_id="fa-create"),
        {
            "action": "create_factory",
            "project_id": "project",
            "name": "Project A Factory",
            "leader_name": "Leader A",
            "leader_model": "test/worker",
        },
    )
    assert fa["ok"] is True

    spec_a = await tool.handle(
        harness.context(tool_call_id="spec-a"),
        {
            "action": "create_member",
            "project_id": "project",
            "name": "Reviewer A",
            "role": "reviewer",
            "instructions": "Review diffs carefully.",
            "model": "test/reviewer",
        },
    )
    assert spec_a["ok"] is True
    member_a = spec_a["data"]["member"]

    job_a = await tool.handle(
        harness.context(tool_call_id="job-a"),
        {
            "action": "create_job",
            "project_id": "project",
            "name": "Daily Review",
            "description": "Review recent commits",
            "prompt": "Check git log and review changes.",
            "agent_member_id": member_a["id"],
            "triggers": [{"type": "schedule", "title": "Morning", "schedule": "0 9 * * *"}],
        },
    )
    assert job_a["ok"] is True

    # Create Factory B (other) and copy specialist + job from Project A.
    fb = await tool.handle(
        harness.context(tool_call_id="fb-create"),
        {
            "action": "create_factory",
            "project_id": "other",
            "name": "Project B Factory",
            "leader_name": "Leader B",
            "leader_model": "test/worker",
        },
    )
    assert fb["ok"] is True
    factory_b_id = fb["data"]["factory"]["id"]

    source_members = await tool.handle(
        harness.context(tool_call_id="list-ma"),
        {"action": "list_members", "project_id": "project"},
    )
    assert source_members["ok"] is True
    copied_source_member = next(
        m for m in source_members["data"]["members"] if m["id"] == member_a["id"]
    )

    spec_b = await tool.handle(
        harness.context(tool_call_id="spec-b"),
        {
            "action": "create_member",
            "factory_id": factory_b_id,
            "name": copied_source_member["name"],
            "role": copied_source_member["role"],
            "instructions": copied_source_member["instructions"],
            "model": "test/reviewer",
        },
    )
    assert spec_b["ok"] is True
    member_b_id = spec_b["data"]["member"]["id"]

    source_jobs = await tool.handle(
        harness.context(tool_call_id="list-ja"),
        {"action": "list_jobs", "project_id": "project"},
    )
    assert source_jobs["ok"] is True
    src_job = source_jobs["data"]["jobs"][0]

    job_b = await tool.handle(
        harness.context(tool_call_id="job-b"),
        {
            "action": "create_job",
            "project_id": "other",
            "name": src_job["name"],
            "description": src_job["description"],
            "prompt": src_job["prompt"],
            "agent_member_id": member_b_id,
            "triggers": [
                {
                    "type": t["type"],
                    "title": t["title"],
                    "schedule": t["schedule"],
                    "enabled": t["enabled"],
                }
                for t in src_job["triggers"]
            ],
        },
    )
    assert job_b["ok"] is True
    assert job_b["data"]["job"]["factory_id"] == factory_b_id
    assert job_b["data"]["job"]["agent_member_id"] == member_b_id
    assert len(job_b["data"]["job"]["triggers"]) == 1

    # Update and retire member B.
    updated_member = await tool.handle(
        harness.context(tool_call_id="upd-mb"),
        {
            "action": "update_member",
            "member_id": member_b_id,
            "role": "lead-reviewer",
            "instructions": "Review everything.",
        },
    )
    assert updated_member["ok"] is True
    assert updated_member["data"]["member"]["role"] == "lead-reviewer"

    retired_member = await tool.handle(
        harness.context(tool_call_id="ret-mb"),
        {"action": "retire_member", "member_id": member_b_id, "project_id": "other"},
    )
    assert retired_member["ok"] is True
    assert retired_member["data"]["member"]["state"] == "retired"


async def test_job_crud_with_trigger_sync_and_trigger_run_lifecycle(
    factory_store: FactoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, tool = await _setup_public_tool(tmp_path, factory_store)
    scheduled_calls: list[str] = []
    monkeypatch.setattr(harness.service, "request_schedule", lambda: scheduled_calls.append("tick"))

    fa = await tool.handle(
        harness.context(tool_call_id="f1"),
        {
            "action": "create_factory",
            "project_id": "project",
            "name": "Alpha",
            "leader_name": "Leader",
            "leader_model": "test/worker",
        },
    )
    assert fa["ok"] is True
    factory_id = fa["data"]["factory"]["id"]

    specialist = await tool.handle(
        harness.context(tool_call_id="m1"),
        {
            "action": "create_member",
            "factory_id": factory_id,
            "name": "Builder",
            "role": "builder",
            "model": "test/worker",
        },
    )
    assert specialist["ok"] is True
    specialist_id = specialist["data"]["member"]["id"]

    # Create job with a schedule trigger -> synced to host triggers.
    created = await tool.handle(
        harness.context(tool_call_id="j-create"),
        {
            "action": "create_job",
            "project_id": "project",
            "name": "Nightly Check",
            "prompt": "Run pytest",
            "triggers": [{"type": "schedule", "title": "Nightly", "schedule": "0 2 * * *"}],
        },
    )
    assert created["ok"] is True
    job_id = created["data"]["job"]["id"]
    assert harness.host.triggers is not None
    assert len(harness.host.triggers.list()) == 1

    # Invalid schedule on update rolls back and keeps old trigger.
    bad_update = await tool.handle(
        harness.context(tool_call_id="j-bad-upd"),
        {
            "action": "update_job",
            "job_id": job_id,
            "triggers": [{"type": "schedule", "title": "Bad", "schedule": "bad-cron"}],
        },
    )
    assert bad_update["ok"] is False
    assert bad_update["error"]["code"] == "invalid_schedule"

    # Valid update syncs new trigger.
    updated = await tool.handle(
        harness.context(tool_call_id="j-upd"),
        {
            "action": "update_job",
            "job_id": job_id,
            "prompt": "Run pytest -q",
            "triggers": [{"type": "schedule", "title": "Hourly", "schedule": "0 * * * *"}],
        },
    )
    assert updated["ok"] is True
    assert updated["data"]["job"]["prompt"] == "Run pytest -q"
    assert harness.host.triggers.list()[0]["schedule"] == "0 * * * *"

    # Trigger job with member_id override (by role) and input_note.
    scheduled_before = len(scheduled_calls)
    triggered = await tool.handle(
        harness.context(tool_call_id="j-trig"),
        {
            "action": "trigger_job",
            "job_id": job_id,
            "member_id": "builder",
            "input_note": "Focus on tests/",
        },
    )
    assert triggered["ok"] is True
    run = triggered["data"]["run"]
    assert run["origin"] == "public_tool"
    assert run["member_id"] == specialist_id
    assert run["input_note"] == "Focus on tests/"
    assert len(scheduled_calls) == scheduled_before + 1

    # list_runs filters by job_id and project_id.
    runs = await tool.handle(
        harness.context(tool_call_id="j-runs"),
        {"action": "list_runs", "project_id": "project", "job_id": job_id, "limit": 5},
    )
    assert runs["ok"] is True
    assert [r["id"] for r in runs["data"]["runs"]] == [run["id"]]

    # Delete job removes host trigger.
    deleted = await tool.handle(
        harness.context(tool_call_id="j-del"),
        {"action": "delete_job", "job_id": job_id},
    )
    assert deleted["ok"] is True
    assert deleted["data"]["deleted_job_id"] == job_id
    assert harness.host.triggers.list() == []


async def test_public_tool_error_responses(factory_store: FactoryStore, tmp_path: Path) -> None:
    harness, tool = await _setup_public_tool(tmp_path, factory_store)

    missing_action = await tool.handle(harness.context(tool_call_id="e1"), {})
    assert missing_action["ok"] is False
    assert missing_action["error"]["code"] == "invalid_arguments"

    bad_action = await tool.handle(harness.context(tool_call_id="e2"), {"action": "remove_factory"})
    assert bad_action["ok"] is False
    assert bad_action["error"]["code"] == "invalid_arguments"
    assert "list_factories" in bad_action["error"]["message"]

    missing_project = await tool.handle(
        harness.context(tool_call_id="e3"), {"action": "create_factory"}
    )
    assert missing_project["ok"] is False
    assert missing_project["error"]["code"] == "invalid_arguments"

    unknown_project = await tool.handle(
        harness.context(tool_call_id="e4"),
        {"action": "get_factory", "project_id": "does-not-exist"},
    )
    assert unknown_project["ok"] is False
    assert unknown_project["error"]["code"] == "not_found"

    missing_job_id = await tool.handle(
        harness.context(tool_call_id="e5"),
        {"action": "update_job", "prompt": "New prompt"},
    )
    assert missing_job_id["ok"] is False
    assert missing_job_id["error"]["code"] == "invalid_arguments"


async def test_public_tool_refuses_factory_sessions_and_unauthorized_mutations(
    factory_store: FactoryStore, tmp_path: Path
) -> None:
    from core.runs import RunExecutionOwner

    harness, tool = await _setup_public_tool(tmp_path, factory_store)

    factory_owner = RunExecutionOwner(
        extension="factory",
        group_id="fjob.jrun_1",
        participant_id="worker",
        generation_id="gen-1",
        epoch="1",
    )
    from_factory = await tool.handle(
        harness.context(tool_call_id="sec-1", execution_owner=factory_owner),
        {"action": "list_factories"},
    )
    assert from_factory["ok"] is False
    assert from_factory["error"]["code"] == "forbidden"

    harness.host.authority = "none"
    read_ok = await tool.handle(
        harness.context(tool_call_id="sec-2"),
        {"action": "list_factories"},
    )
    assert read_ok["ok"] is True

    mut_denied = await tool.handle(
        harness.context(tool_call_id="sec-3"),
        {"action": "create_factory", "project_id": "project"},
    )
    assert mut_denied["ok"] is False
    assert mut_denied["error"]["code"] == "forbidden"

    harness.host.authority = "authorized_task"
    mut_allowed = await tool.handle(
        harness.context(tool_call_id="sec-4"),
        {
            "action": "create_factory",
            "project_id": "project",
            "leader_name": "Auto Leader",
            "leader_model": "test/worker",
        },
    )
    assert mut_allowed["ok"] is True
