"""Tests for rpc agents lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.channels import ChannelConfigError
from core.runs import Run
from core.sessions import SessionAddress
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    InstrumentedAgentDeleteLock,
    StubAdapter,
    make_state,
)
from tests.server.rpc_test_support import _no_models_dev_fetch as _no_models_dev_fetch


@pytest.mark.asyncio
async def test_agent_rename_retargets_live_references_and_publishes_mapping(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update(
        "coder",
        tools={"subagent": {"allowed_agents": ["coder", "coder@vbot"]}},
    )
    state.runtime.agents.create(
        "manager",
        "Manager",
        tools={"subagent": {"allowed_agents": ["coder"]}},
    )
    state.runtime.chat_sessions.create("child", session_id="child-session")
    state.runtime.chat_sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="child", session_id="child-session"),
        {
            "subagent_parent": {
                "agent_id": "coder",
                "session_id": "parent-session",
                "run_id": "parent-run",
                "project_id": None,
            },
            "fork_source": {"agent_id": "coder", "session_id": "historical"},
        },
    )
    channels = [SimpleNamespace(id="telegram", agent_id="coder")]
    jobs = [
        SimpleNamespace(id="active", agent_id="coder", project_id=None, status="active"),
        SimpleNamespace(id="history", agent_id="coder", project_id=None, status="completed"),
        SimpleNamespace(id="project", agent_id="coder", project_id="vbot", status="active"),
    ]
    bootstrap_jobs = [
        SimpleNamespace(id="boot-active", agent_id="coder", project_id=None, status="active"),
        SimpleNamespace(id="boot-history", agent_id="coder", project_id=None, status="completed"),
    ]

    def update_channel(channel_id: str, **fields: Any) -> None:
        channel = next(item for item in channels if item.id == channel_id)
        channel.agent_id = fields["agent_id"]

    def update_job(job_id: str, **fields: Any) -> Any:
        job = next(item for item in jobs if item.id == job_id)
        job.agent_id = fields["agent_id"]
        return job

    def update_bootstrap_job(job_id: str, **fields: Any) -> Any:
        job = next(item for item in bootstrap_jobs if item.id == job_id)
        job.agent_id = fields["agent_id"]
        return job

    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: channels,
        update_channel=update_channel,
    )
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: jobs,
        update_job=update_job,
    )
    state.runtime.bootstrap_service = SimpleNamespace(
        list_jobs=lambda: bootstrap_jobs,
        update_job=update_bootstrap_job,
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is True
    assert response["result"]["id"] == "researcher"
    assert response["result"]["rename"] == {
        "old_id": "coder",
        "new_id": "researcher",
        "channels_updated": ["telegram"],
        "cron_jobs_updated": ["active"],
        "bootstrap_jobs_updated": ["boot-active"],
        "agent_policies_updated": ["manager", "researcher"],
        "session_links_updated": 1,
    }
    assert channels[0].agent_id == "researcher"
    assert jobs[0].agent_id == "researcher"
    assert jobs[1].agent_id == "coder"
    assert jobs[2].agent_id == "coder"
    assert bootstrap_jobs[0].agent_id == "researcher"
    assert bootstrap_jobs[1].agent_id == "coder"
    assert state.runtime.agents.get("researcher").tools["subagent"]["allowed_agents"] == [
        "researcher",
        "coder@vbot",
    ]
    assert state.runtime.agents.get("manager").tools["subagent"]["allowed_agents"] == ["researcher"]
    child_metadata = state.runtime.chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="child", session_id="child-session")
    )
    assert child_metadata["subagent_parent"]["agent_id"] == "researcher"
    assert child_metadata["fork_source"]["agent_id"] == "coder"
    events = [event["payload"] for event in state.event_bus.events]
    assert events == [
        {
            "kind": "agents",
            "scope": {"old_agent_id": "coder", "new_agent_id": "researcher"},
        },
        {
            "kind": "sessions",
            "scope": {"old_agent_id": "coder", "new_agent_id": "researcher"},
        },
        {"kind": "channels"},
        {"kind": "cron"},
    ]


@pytest.mark.asyncio
async def test_agent_rename_rejects_active_run(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    release = asyncio.Event()
    coder = state.runtime.agents.get("coder")

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        SessionAddress(project_id=None, agent_id="coder", session_id=coder.current_session_id),
        hold_run,
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_busy"
    assert state.runtime.agents.get("coder").id == "coder"
    release.set()
    assert await run.wait() == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_agent_id", ["coder", "researcher"])
async def test_agent_rename_rejects_open_subagent_relation(
    tmp_path: Path,
    busy_agent_id: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.subagents = SimpleNamespace(
        batch_tracker=SimpleNamespace(
            references_identity_agent=lambda agent_id: agent_id == busy_agent_id
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_busy"
    assert state.runtime.agents.get("coder").id == "coder"


@pytest.mark.asyncio
async def test_agent_rename_rolls_back_all_changes_when_reference_update_fails(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create(
        "manager",
        "Manager",
        tools={"subagent": {"allowed_agents": ["coder"]}},
    )
    state.runtime.chat_sessions.create("child", session_id="child-session")
    original_metadata = {
        "subagent_parent": {
            "agent_id": "coder",
            "session_id": "parent-session",
            "run_id": "parent-run",
            "project_id": None,
        }
    }
    state.runtime.chat_sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="child", session_id="child-session"),
        original_metadata,
    )
    channels = [
        SimpleNamespace(id="first", agent_id="coder"),
        SimpleNamespace(id="second", agent_id="coder"),
    ]

    def update_channel(channel_id: str, **fields: Any) -> None:
        if channel_id == "second" and fields["agent_id"] == "researcher":
            raise ChannelConfigError("adapter preflight failed")
        channel = next(item for item in channels if item.id == channel_id)
        channel.agent_id = fields["agent_id"]

    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: channels,
        update_channel=update_channel,
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert state.runtime.agents.get("coder").id == "coder"
    assert all(channel.agent_id == "coder" for channel in channels)
    assert state.runtime.agents.get("manager").tools["subagent"]["allowed_agents"] == ["coder"]
    assert (
        state.runtime.chat_sessions.get_metadata(
            SessionAddress(project_id=None, agent_id="child", session_id="child-session")
        )
        == original_metadata
    )
    assert state.event_bus.events == []


@pytest.mark.asyncio
async def test_agent_rename_rejects_existing_destination(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("researcher", "Researcher")

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert state.runtime.agents.get("coder").id == "coder"
    assert state.runtime.agents.get("researcher").name == "Researcher"


@pytest.mark.asyncio
async def test_agent_delete_rejects_last_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "last_agent"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_active_run(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    release = asyncio.Event()
    coder = state.runtime.agents.get("coder")

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        SessionAddress(project_id=None, agent_id="coder", session_id=coder.current_session_id),
        hold_run,
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_busy"
    assert state.runtime.agents.get("coder").id == "coder"

    release.set()
    assert await run.wait() == "done"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_channel_reference(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: [SimpleNamespace(id="tg-coder", agent_id="coder")]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "channel:tg-coder" in response["error"]["message"]
    assert state.runtime.agents.get("coder").id == "coder"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_cron_reference(tmp_path: Path) -> None:
    # A bare cron job (project_id=None) targets the identity agent, so it blocks
    # the identity-agent delete.
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: [SimpleNamespace(id="job-coder", agent_id="coder", project_id=None)]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "cron:job-coder" in response["error"]["message"]
    assert state.runtime.agents.get("coder").id == "coder"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_bootstrap_reference(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.bootstrap_service = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="boot-coder",
                agent_id="coder",
                project_id=None,
                status="active",
            )
        ]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "bootstrap:boot-coder" in response["error"]["message"]


@pytest.mark.asyncio
async def test_agent_delete_ignores_project_qualified_cron_reference(tmp_path: Path) -> None:
    # A project-qualified cron job (project_id set) targets that project's Team
    # agent, not the same-named identity agent, so it must not block the identity
    # delete.
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: [SimpleNamespace(id="job-coder", agent_id="coder", project_id="vbot")]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is True
    assert response["result"]["agent_id"] == "coder"


@pytest.mark.asyncio
async def test_agent_delete_ignores_terminal_cron_history(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="job-coder",
                agent_id="coder",
                project_id=None,
                status="completed",
            )
        ]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is True


@pytest.mark.asyncio
async def test_agent_delete_serializes_minimum_one_check_and_delete(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    agent_delete_lock = InstrumentedAgentDeleteLock()
    state.agent_delete_lock = agent_delete_lock

    coder_delete, writer_delete = await asyncio.gather(
        dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}}),
        dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "writer"}}),
    )

    responses = [coder_delete, writer_delete]
    successes = [response for response in responses if response["ok"]]
    failures = [response for response in responses if not response["ok"]]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error"]["code"] == "last_agent"
    assert len(state.runtime.agents.list()) == 1
    assert len(successes[0]["result"]["remaining_agents"]) == 1
    assert agent_delete_lock.max_active == 1


@pytest.mark.asyncio
async def test_session_create_make_current_updates_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "session.create",
            "params": {"agent_id": "coder", "session_id": "current-two", "make_current": True},
        },
    )

    assert response["ok"] is True
    assert state.runtime.agents.get("coder").current_session_id == "current-two"
