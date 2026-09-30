"""Identity Agent rename and delete RPCs: the rename mapping, and refusals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents import AgentStore
from core.database import DatabaseUnavailableError, SnapshotBarrier
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


def _job_service(jobs: list[SimpleNamespace]) -> SimpleNamespace:
    def retarget_agent(job_id: str, agent_id: str) -> SimpleNamespace:
        job = next(item for item in jobs if item.id == job_id)
        job.agent_id = agent_id
        return job

    async def retarget_agent_async(job_id: str, agent_id: str) -> SimpleNamespace:
        return retarget_agent(job_id, agent_id)

    return SimpleNamespace(
        list_jobs=lambda: jobs,
        retarget_agent=retarget_agent,
        retarget_agent_async=retarget_agent_async,
    )


@pytest.mark.asyncio
async def test_agent_rename_publishes_the_mapping_of_retargeted_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(tmp_path, StubAdapter())
    agents = AgentStore(tmp_path, sessions=state.runtime.chat_sessions)
    state.runtime.agents = agents
    monkeypatch.setattr(state.runtime.agent_resolver, "_agents", agents)
    agents.create("coder", tools={"subagent": {"allowed_agents": ["coder", "coder@vbot"]}})
    agents.create("manager", "Manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    child = SessionAddress(project_id=None, agent_id="manager", session_id="child")
    state.runtime.chat_sessions.create("coder", session_id="parent")
    state.runtime.chat_sessions.create("manager", session_id="child")
    state.runtime.chat_sessions.set_metadata(
        child,
        {"subagent_parent": {"agent_id": "coder", "session_id": "parent", "project_id": None}},
    )
    channels = [SimpleNamespace(id="telegram", agent_id="coder")]
    # A data snapshot that finds a compound mutation in flight gives up at once.
    state.runtime.snapshot_barrier = SnapshotBarrier(capture_wait_seconds=0.0)
    snapshots_during_rename: list[str] = []

    async def retarget_agent_async(agent_id: str, new_agent_id: str) -> tuple[str, ...]:
        moved = [channel for channel in channels if channel.agent_id == agent_id]
        for channel in moved:
            channel.agent_id = new_agent_id
        try:
            with state.runtime.snapshot_barrier.capture():
                snapshots_during_rename.append("captured")
        except DatabaseUnavailableError:
            snapshots_during_rename.append("held off")
        return tuple(channel.id for channel in moved)

    async def list_channels_async() -> list[SimpleNamespace]:
        return channels

    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: channels,
        list_channels_async=list_channels_async,
        retarget_agent_async=retarget_agent_async,
    )
    # Completed history stays as it ran; a Project-qualified job targets that
    # Project's Team Agent, not the same-named Identity Agent.
    cron_jobs = [
        SimpleNamespace(id="cron-job", agent_id="coder", project_id=None, status="active"),
        SimpleNamespace(id="history", agent_id="coder", project_id=None, status="completed"),
        SimpleNamespace(id="project", agent_id="coder", project_id="vbot", status="active"),
    ]
    state.runtime.cron_service = _job_service(cron_jobs)
    state.runtime.bootstrap_service = _job_service(
        [SimpleNamespace(id="boot-job", agent_id="coder", project_id=None, status="paused")]
    )

    try:
        result = await rpc_result(state, "agent.rename", id="coder", new_id="researcher")
    finally:
        agents.close()

    assert result["id"] == "researcher"
    assert result["rename"] == {
        "old_id": "coder",
        "new_id": "researcher",
        "channels_updated": ["telegram"],
        "cron_jobs_updated": ["cron-job"],
        "bootstrap_jobs_updated": ["boot-job"],
        "agent_policies_updated": ["manager", "researcher"],
        "session_links_updated": 1,
    }
    assert [job.agent_id for job in cron_jobs] == ["researcher", "coder", "coder"]
    # Retargeting references is part of one compound mutation with the rename.
    assert snapshots_during_rename == ["held off"]
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


def _references(service: str, *entries: JsonObject) -> Callable[[Any], None]:
    """Install a Channel, cron or bootstrap service listing *entries*."""

    def arrange(state: Any) -> None:
        listed = [SimpleNamespace(**entry) for entry in entries]

        async def list_channels_async() -> list[SimpleNamespace]:
            return listed

        fake = SimpleNamespace(
            list_channels=lambda: listed,
            list_channels_async=list_channels_async,
            list_jobs=lambda: listed,
        )
        setattr(state.runtime, service, fake)

    return arrange


def _listed_job(service: str, **fields: Any) -> Callable[[Any], None]:
    """List one live job of the Identity Agent ``coder`` on the runtime's cron or bootstrap service.

    *fields* override the job's attributes.
    """

    def arrange(state: Any) -> None:
        job = {
            "id": "job-coder",
            "name": "Report",
            "agent_id": "coder",
            "project_id": None,
            "session_id": None,
            "status": "active",
            **fields,
        }
        getattr(state.runtime, service).jobs.append(SimpleNamespace(**job))

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
        # A reverted rename would take along a reference that already named the new id.
        pytest.param(
            _references("channel_service", {"id": "tg-old", "agent_id": "researcher"}),
            "agent_in_use",
            id="referenced-destination",
        ),
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
    assert state.event_bus.events == []


def _calendar_action(**fields: Any) -> Callable[[Any], None]:
    """List one Calendar action for the Agent ``coder`` unless *fields* say otherwise."""

    def arrange(state: Any) -> None:
        action = {"id": "act-coder", "event_id": "evt-1", "target": "coder", **fields}
        state.runtime.calendar_service.actions.actions.append(action)

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
        pytest.param(_listed_job("cron_service"), "agent_in_use", "cron:job-coder", id="cron"),
        pytest.param(
            _listed_job("bootstrap_service", id="boot-coder"),
            "agent_in_use",
            "bootstrap:boot-coder",
            id="bootstrap",
        ),
        pytest.param(_calendar_action(), "agent_in_use", "calendar:act-coder", id="calendar"),
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
    "arrange",
    [
        # A Project-qualified job targets that Project's Team Agent, not the
        # same-named identity Agent.
        pytest.param(_listed_job("cron_service", project_id="vbot"), id="project-qualified"),
        pytest.param(_listed_job("cron_service", status="completed"), id="terminal-history"),
        # An action that can no longer fire, for example of a past one-time event.
        pytest.param(_calendar_action(spent=True), id="calendar-used-up"),
    ],
)
async def test_agent_delete_ignores_automations_that_never_start_a_run_for_the_agent(
    tmp_path: Path, arrange: Callable[[Any], None]
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    arrange(state)

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
