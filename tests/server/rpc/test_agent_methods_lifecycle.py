"""Identity Agent rename and delete: reference retargeting, rollback and refusals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.automation.bootstrap import BootstrapService
from core.channels import ChannelConfigError
from core.runs import Run
from core.sessions import SessionAddress
from tests.server.rpc_test_support import (
    InstrumentedAgentDeleteLock,
    JsonObject,
    StubAdapter,
    call,
    make_state,
    rpc_error,
    rpc_result,
)
from tests.server.rpc_test_support import _no_models_dev_fetch as _no_models_dev_fetch


def _agent_names(state: Any) -> dict[str, str]:
    return {agent.id: agent.name for agent in state.runtime.agents.list()}


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
        },
    )
    historical = state.runtime.chat_sessions.create("coder", session_id="historical")
    fork = await state.runtime.chat_sessions.fork(historical.address, target_agent_id="child")
    channels = [SimpleNamespace(id="telegram", agent_id="coder")]
    jobs = [
        SimpleNamespace(id="active", agent_id="coder", project_id=None, status="active"),
        SimpleNamespace(id="history", agent_id="coder", project_id=None, status="completed"),
        SimpleNamespace(id="project", agent_id="coder", project_id="vbot", status="active"),
    ]
    bootstrap_creator = BootstrapService(
        state.runtime.trigger_service, tmp_path, startup_id="earlier-startup"
    )
    bootstrap_jobs = {
        status: bootstrap_creator.create_job(agent_id="coder", prompt=status, mode="once")
        for status in ("active", "paused", "failed", "completed")
    }
    bootstrap_creator.disable_job(bootstrap_jobs["paused"].id)
    earlier_run: dict[str, Any] = {
        "last_started_startup_id": "earlier-startup",
        "last_run_id": "run-before",
        "last_session_id": "session-before",
    }
    bootstrap_creator.restore_job(
        replace(
            bootstrap_jobs["failed"],
            status="failed",
            last_outcome="failed",
            last_error="boom",
            **earlier_run,
        )
    )
    bootstrap_creator.restore_job(
        replace(
            bootstrap_jobs["completed"], status="completed", last_outcome="success", **earlier_run
        )
    )
    bootstrap_before = {job.id: job for job in bootstrap_creator.list_jobs()}
    retargeted = [job.id for job in bootstrap_before.values() if job.status != "completed"]

    channel_update_loops: list[asyncio.AbstractEventLoop] = []

    async def update_channel(channel_id: str, **fields: Any) -> None:
        channel_update_loops.append(asyncio.get_running_loop())
        channel = next(item for item in channels if item.id == channel_id)
        channel.agent_id = fields["agent_id"]

    def retarget_agent(job_id: str, agent_id: str) -> Any:
        job = next(item for item in jobs if item.id == job_id)
        job.agent_id = agent_id
        return job

    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: channels,
        update_channel=update_channel,
    )
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: jobs,
        retarget_agent=retarget_agent,
    )
    bootstrap_service = BootstrapService(
        state.runtime.trigger_service, tmp_path, startup_id="rename-startup"
    )
    state.runtime.bootstrap_service = bootstrap_service

    result = await rpc_result(state, "agent.rename", id="coder", new_id="researcher")

    assert result["id"] == "researcher"
    assert result["rename"] == {
        "old_id": "coder",
        "new_id": "researcher",
        "channels_updated": ["telegram"],
        "cron_jobs_updated": ["active"],
        "bootstrap_jobs_updated": retargeted,
        "agent_policies_updated": ["manager", "researcher"],
        "session_links_updated": 1,
    }
    assert channels[0].agent_id == "researcher"
    # The rename worker hands Channel changes to the Event Loop owning the adapters.
    assert channel_update_loops == [asyncio.get_running_loop()]
    assert jobs[0].agent_id == "researcher"
    assert jobs[1].agent_id == "coder"
    assert jobs[2].agent_id == "coder"
    # Only the Agent id moves: a rename neither un-pauses nor re-arms a job and keeps
    # a failed one's error, while a completed one-shot stays untouched history.
    bootstrap_after = {job.id: job for job in bootstrap_service.list_jobs()}
    for job_id in retargeted:
        assert bootstrap_after[job_id] == replace(bootstrap_before[job_id], agent_id="researcher")
    completed_id = bootstrap_jobs["completed"].id
    assert bootstrap_after[completed_id] == bootstrap_before[completed_id]
    assert bootstrap_after[completed_id].agent_id == "coder"
    assert state.runtime.agents.get("researcher").tools["subagent"]["allowed_agents"] == [
        "researcher",
        "coder@vbot",
    ]
    assert state.runtime.agents.get("manager").tools["subagent"]["allowed_agents"] == ["researcher"]
    child_metadata = state.runtime.chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="child", session_id="child-session")
    )
    assert child_metadata["subagent_parent"]["agent_id"] == "researcher"
    # Fork provenance is not a reference the rename rewrites (``session_links_updated``
    # counts only the Sub-Agent parent): it names the source Session's own address,
    # which this stub Agent store leaves in place.
    fork_source = state.runtime.chat_sessions.get_metadata(fork.address)["fork_source"]
    assert (fork_source["agent_id"], fork_source["session_id"]) == ("coder", "historical")
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
    child = SessionAddress(project_id=None, agent_id="child", session_id="child-session")
    state.runtime.chat_sessions.set_metadata(
        child,
        {
            "subagent_parent": {
                "agent_id": "coder",
                "session_id": "parent-session",
                "run_id": "parent-run",
                "project_id": None,
            }
        },
    )
    # Compare against the stored (normalized) form of the parent reference.
    original_metadata = state.runtime.chat_sessions.get_metadata(child)
    assert original_metadata["subagent_parent"]["agent_id"] == "coder"
    channels = [
        SimpleNamespace(id="first", agent_id="coder"),
        SimpleNamespace(id="second", agent_id="coder"),
    ]

    channel_update_loops: list[asyncio.AbstractEventLoop] = []

    async def update_channel(channel_id: str, **fields: Any) -> None:
        channel_update_loops.append(asyncio.get_running_loop())
        if channel_id == "second" and fields["agent_id"] == "researcher":
            raise ChannelConfigError("adapter preflight failed")
        channel = next(item for item in channels if item.id == channel_id)
        channel.agent_id = fields["agent_id"]

    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: channels,
        update_channel=update_channel,
    )

    await rpc_error(state, "agent.rename", id="coder", new_id="researcher")

    assert state.runtime.agents.get("coder").id == "coder"
    assert all(channel.agent_id == "coder" for channel in channels)
    # Forward changes and their rollback both ran on the Event Loop.
    assert channel_update_loops == [asyncio.get_running_loop()] * 3
    assert state.runtime.agents.get("manager").tools["subagent"]["allowed_agents"] == ["coder"]
    assert state.runtime.chat_sessions.get_metadata(child) == original_metadata
    assert state.event_bus.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("agent.rename", {"id": "coder", "new_id": "researcher"}),
        ("agent.delete", {"id": "coder"}),
    ],
)
async def test_an_agent_with_an_active_run_cannot_be_renamed_or_deleted(
    tmp_path: Path, method: str, params: JsonObject
) -> None:
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
    before = _agent_names(state)

    error = await rpc_error(state, method, **params)

    assert error["code"] == "agent_busy"
    assert _agent_names(state) == before
    release.set()
    assert await run.wait() == "done"


def _open_subagent_relation(busy_agent_id: str) -> Callable[[Any], None]:
    def arrange(state: Any) -> None:
        state.runtime.subagents = SimpleNamespace(
            batch_tracker=SimpleNamespace(
                references_identity_agent=lambda agent_id: agent_id == busy_agent_id
            )
        )

    return arrange


def _existing_destination(state: Any) -> None:
    state.runtime.agents.create("researcher", "Researcher")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        pytest.param(_open_subagent_relation("coder"), "agent_busy", id="subagent-source"),
        pytest.param(_open_subagent_relation("researcher"), "agent_busy", id="subagent-target"),
        pytest.param(_existing_destination, "domain_error", id="existing-destination"),
    ],
)
async def test_a_refused_agent_rename_leaves_every_agent_in_place(
    tmp_path: Path, arrange: Callable[[Any], None], code: str
) -> None:
    state = make_state(tmp_path, StubAdapter())
    arrange(state)
    before = _agent_names(state)

    error = await rpc_error(state, "agent.rename", id="coder", new_id="researcher")

    assert error["code"] == code
    assert _agent_names(state) == before


def _references(service: str, *entries: JsonObject) -> Callable[[Any], None]:
    """Install a Channel, cron or bootstrap service listing *entries*."""

    def arrange(state: Any) -> None:
        listed = [SimpleNamespace(**entry) for entry in entries]
        fake = SimpleNamespace(list_channels=lambda: listed, list_jobs=lambda: listed)
        setattr(state.runtime, service, fake)

    return arrange


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "code", "named"),
    [
        pytest.param(
            _references("channel_service", {"id": "tg-coder", "agent_id": "coder"}),
            "agent_in_use",
            "channel:tg-coder",
            id="channel",
        ),
        # A bare cron job (no Project) targets the identity Agent.
        pytest.param(
            _references(
                "cron_service", {"id": "job-coder", "agent_id": "coder", "project_id": None}
            ),
            "agent_in_use",
            "cron:job-coder",
            id="cron",
        ),
        pytest.param(
            _references(
                "bootstrap_service",
                {"id": "boot-coder", "agent_id": "coder", "project_id": None, "status": "active"},
            ),
            "agent_in_use",
            "bootstrap:boot-coder",
            id="bootstrap",
        ),
    ],
)
async def test_agent_delete_refuses_a_referenced_agent(
    tmp_path: Path, arrange: Callable[[Any], None], code: str, named: str
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    arrange(state)
    before = _agent_names(state)

    error = await rpc_error(state, "agent.delete", id="coder")

    assert error["code"] == code
    assert named in error["message"]
    assert _agent_names(state) == before


@pytest.mark.asyncio
async def test_agent_delete_refuses_the_last_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    error = await rpc_error(state, "agent.delete", id="coder")

    assert error["code"] == "last_agent"
    assert list(_agent_names(state)) == ["coder"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job",
    [
        # A Project-qualified job targets that Project's Team Agent, not the
        # same-named identity Agent.
        pytest.param({"project_id": "vbot"}, id="project-qualified"),
        pytest.param({"project_id": None, "status": "completed"}, id="terminal-history"),
    ],
)
async def test_agent_delete_ignores_cron_jobs_that_do_not_target_the_identity_agent(
    tmp_path: Path, job: JsonObject
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    _references("cron_service", {"id": "job-coder", "agent_id": "coder", **job})(state)

    result = await rpc_result(state, "agent.delete", id="coder")

    assert result["agent_id"] == "coder"
    assert list(_agent_names(state)) == ["writer"]


@pytest.mark.asyncio
async def test_agent_delete_serializes_minimum_one_check_and_delete(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    agent_delete_lock = InstrumentedAgentDeleteLock()
    state.agent_delete_lock = agent_delete_lock

    responses = await asyncio.gather(
        call(state, "agent.delete", id="coder"),
        call(state, "agent.delete", id="writer"),
    )

    successes = [response for response in responses if response["ok"]]
    failures = [response for response in responses if not response["ok"]]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error"]["code"] == "last_agent"
    assert len(state.runtime.agents.list()) == 1
    assert len(successes[0]["result"]["remaining_agents"]) == 1
    assert agent_delete_lock.max_active == 1
