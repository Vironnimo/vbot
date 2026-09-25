"""Real dispatch accepts direct delegation without guessing a different effect."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest
import pytest_asyncio

from core.chat import ChatMessage
from core.sessions import SessionAddress

from .subagent_test_support import (
    BACKGROUND_TASK_SETTLE_TICKS,
    FakeAgentResolver,
    FakeAgents,
    dispatch_harness,
    make_context,
)

BRIEF = 'Read-only review of src/a.py. Preserve literal {"action":"CANCEL"}.\nReference R-17.'


@pytest_asyncio.fixture
async def dispatch_runtime(tmp_path):
    async with dispatch_harness(tmp_path) as harness:
        yield harness


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,target",
    [
        ({"content": BRIEF}, "parent"),
        ({"agent_id": "worker", "content": BRIEF, "description": "Review source"}, "worker"),
        ({"arguments": {"Agent-ID": "worker", "content": BRIEF}}, "worker"),
        ({"request": {"operation": "RUN", "content": BRIEF}}, "parent"),
        ({"run": {"content": BRIEF}}, "parent"),
        ({"action": "run", "content": BRIEF}, "parent"),
    ],
)
async def test_direct_delegation_reaches_exact_agent_with_unchanged_payload(
    dispatch_runtime, arguments, target
):
    fixture = dispatch_runtime
    before = copy.deepcopy(arguments)
    result = await fixture.registry.dispatch(fixture.context, arguments)
    assert result["ok"], result
    assert arguments == before
    assert len(fixture.manager.started) == 1
    agent, session, executor, run = fixture.manager.started[0]
    assert agent == target == result["data"]["agent_id"]
    assert session == result["data"]["session_id"]
    received = await executor(run)
    assert received.content == f"handled: {BRIEF}"
    metadata = fixture.runtime.chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id=target, session_id=session)
    )
    assert metadata["is_subagent_session"]
    assert metadata["subagent_parent"]["session_id"] == fixture.context.session_id


@pytest.mark.asyncio
async def test_omitted_action_continues_exact_owning_session(dispatch_runtime):
    fixture = dispatch_runtime
    session = fixture.runtime.chat_sessions.create("worker")
    result = await fixture.registry.dispatch(
        fixture.context, {"agent_id": "worker", "session_id": session.id, "content": BRIEF}
    )
    assert result["ok"], result
    assert result["data"]["session_id"] == session.id
    assert fixture.manager.started[0][:2] == ("worker", session.id)
    assert len(fixture.runtime.chat_sessions.list("worker")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller_agent,caller_project,target_argument,expected_agent_id",
    [
        # An Identity Agent delegates to a Project Agent.
        ("parent", None, "builder@vbot", "builder@vbot"),
        # A Project Agent delegates to a Team member of its own Project.
        ("lead", "vbot", "builder", "builder@vbot"),
        # A Project Agent delegates to a copy of itself.
        ("lead", "vbot", None, "lead@vbot"),
    ],
)
async def test_returned_agent_id_continues_the_exact_child_session(
    dispatch_runtime, caller_agent, caller_project, target_argument, expected_agent_id
):
    """Copying the returned agent_id and session_id reaches the same child Session."""
    fixture = dispatch_runtime
    agents = FakeAgents({"parent", "lead", "builder"})
    fixture.runtime.agents = agents
    fixture.runtime.agent_resolver = FakeAgentResolver(agents)
    context = make_context(agent_id=caller_agent, project_id=caller_project)
    child_agent = expected_agent_id.split("@")[0]
    arguments = {"content": BRIEF}
    if target_argument is not None:
        arguments["agent_id"] = target_argument

    spawned = await fixture.registry.dispatch(context, arguments)
    listed = await fixture.registry.dispatch(context, {"action": "status"})

    assert spawned["ok"], spawned
    child = spawned["data"]
    assert child["agent_id"] == expected_agent_id
    assert child["project_id"] == "vbot"
    assert [entry["agent_id"] for entry in listed["data"]["subagents"]] == [expected_agent_id]
    first_run = fixture.manager.started[0][3]
    first_run.mark_completed(ChatMessage.assistant(model="fixture", content="first"))
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)
    assert len(fixture.triggers.calls) == 1
    assert expected_agent_id in fixture.triggers.calls[0][1]

    continued = await fixture.registry.dispatch(
        context,
        {"agent_id": child["agent_id"], "session_id": child["session_id"], "content": "Go on."},
    )

    assert continued["ok"], continued
    assert continued["data"]["agent_id"] == expected_agent_id
    assert continued["data"]["session_id"] == child["session_id"]
    second_run = fixture.manager.started[1][3]
    assert (second_run.agent_id, second_run.project_id, second_run.session_id) == (
        child_agent,
        "vbot",
        child["session_id"],
    )
    assert fixture.runtime.agent_resolver.calls[-1][:2] == ("vbot", child_agent)
    assert [
        session.id for session in fixture.runtime.chat_sessions.list(child_agent, project_id="vbot")
    ] == [child["session_id"]]
    assert fixture.runtime.chat_sessions.list(child_agent) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["", "none", "high"])
async def test_direct_delegation_preserves_run_overrides(dispatch_runtime, effort):
    fixture = dispatch_runtime
    result = await fixture.registry.dispatch(
        fixture.context,
        {"content": BRIEF, "model": "openai/test-model", "thinking_effort": effort},
    )
    assert result["ok"], result
    overrides = fixture.runtime.streaming_chat_loop.seen_agent_overrides[-1]
    assert overrides.model == "openai/test-model"
    assert overrides.thinking_effort == (effort or None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        # No task, or a decision is missing.
        {},
        {"description": "Review source"},
        {"content": ""},
        {"content": " "},
        {"content": "."},
        {"id": "existing-work"},
        {"id": "sub_abcdefghijkl", "agent_id": "worker"},
        # An explicit invalid choice never counts as omission.
        {"content": BRIEF, "action": "rn"},
        {"content": BRIEF, "action": "cancel"},
        # An explicit target that does not exist is never replaced.
        {"content": BRIEF, "agent_id": "workre"},
        {"content": BRIEF, "subagent_type": "Explore"},
        {"content": BRIEF, "session_id": "existing-session"},
        # A work id with run could mean continuing that work or starting new work.
        {"content": BRIEF, "id": "sub_abcdefghijkl"},
        # Conflicting instructions are never settled by picking one.
        {"content": BRIEF, "agent_id": "worker", "Agent-ID": "parent"},
        {"content": BRIEF, "prompt": "Another task."},
        {"content": BRIEF, "action": "run", "operation": "cancel"},
        {"content": BRIEF, "background": True, "blocking": True},
        # Requests vBot cannot honor as written.
        {"content": BRIEF, "priority": "high"},
        {"content": BRIEF, "run_id": "private-run"},
        {"content": BRIEF, "toolsets": ["web"]},
        {"tasks": [{"goal": BRIEF}, {"goal": "Second task."}]},
    ],
)
async def test_ambiguity_and_unsupported_constraints_never_start_work(dispatch_runtime, arguments):
    fixture = dispatch_runtime
    before = copy.deepcopy(arguments)
    try:
        result = await fixture.registry.dispatch(fixture.context, arguments)
    except ValueError:
        pass
    else:
        assert not result["ok"], result
    assert arguments == before
    assert fixture.manager.started == []
    assert fixture.manager.enqueued == []
    assert fixture.runtime.chat_sessions.list("parent") == []
    assert fixture.runtime.chat_sessions.list("worker") == []


@pytest.mark.asyncio
async def test_explicit_status_without_work_remains_an_observation(dispatch_runtime):
    fixture = dispatch_runtime
    result = await fixture.registry.dispatch(fixture.context, {"action": "status"})
    assert result["data"] == {"subagents": []}
    assert fixture.manager.started == []


@pytest.mark.asyncio
async def test_omitted_action_does_not_bypass_target_authorization(dispatch_runtime):
    fixture = dispatch_runtime
    context = replace(fixture.context, tool_settings={"subagent": {"allowed_agents": []}})
    result = await fixture.registry.dispatch(context, {"content": BRIEF, "agent_id": "worker"})
    assert result["error"]["code"] == "agent_not_allowed"
    assert fixture.runtime.agent_resolver.calls == []
    assert fixture.manager.started == []
    assert fixture.runtime.chat_sessions.list("worker") == []
