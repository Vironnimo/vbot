"""Delegating work: target resolution, authorization, child Sessions and Run admission.

Also covers how a ``run`` call is interpreted against tracked work: work ids,
stand-in Session ids and Sessions addressed without their Agent.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.projects import (
    AgentResolutionError,
    ModelConfigurationError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.runs import RunExecutionOwner, RunInterruptedError, RunKind
from core.subagents import SUBAGENT_SESSION_STARTED_EVENT
from core.subagents._constants import (
    DEFAULT_SUBAGENT_TIMEOUT_MINUTES,
    SUBAGENT_BACKGROUND_UNAVAILABLE_NOTE,
    SUBAGENT_FOREGROUND_ONLY_NOTE,
    SUBAGENT_USER_CANCEL_MESSAGE,
    TOP_LEVEL_BACKGROUND_NOTE,
)
from tests.core.subagents.subagents_test_support import (
    JsonObject,
    RaisingResolver,
    SubAgentHarness,
    activity_path_from_note,
    address,
    done,
    make_context,
)
from tests.core.subagents.subagents_test_support import (
    harness as harness,
)

pytestmark = pytest.mark.asyncio

BRIEF = "Review src/a.py for unused imports and report file:line findings. Do not edit."
RUNNING_NOTE = TOP_LEVEL_BACKGROUND_NOTE


@pytest.mark.parametrize(
    ("caller_project", "target", "child_agent", "returned_agent"),
    [
        (None, "worker", "worker", "worker"),
        ("acme", "worker", "worker", "worker@acme"),
        ("acme", None, "parent", "parent@acme"),
    ],
)
async def test_child_session_lives_in_the_callers_scope_and_links_its_parent(
    harness: SubAgentHarness,
    caplog: pytest.LogCaptureFixture,
    caller_project: str | None,
    target: str | None,
    child_agent: str,
    returned_agent: str,
) -> None:
    arguments: JsonObject = {"content": "spawn"}
    if target is not None:
        arguments["agent_id"] = target
    caplog.set_level(logging.INFO, logger="vbot.subagents")

    result = await harness.spawn(arguments, project_id=caller_project)

    assert result["agent_id"] == returned_agent
    assert result.get("project_id") == caller_project
    child = address(child_agent, result["session_id"], caller_project)
    assert harness.sessions.exists(child)
    other_scope = "elsewhere" if caller_project is None else None
    assert not harness.sessions.exists(address(child_agent, result["session_id"], other_scope))
    [started] = await harness.started()
    assert started.run.project_id == caller_project
    assert started.admission.run_kind is RunKind.SUBAGENT
    assert started.admission.work_id == result["id"]
    [spawned] = [record for record in caplog.records if record.name == "vbot.subagents"]
    spawn_fields = (
        "parent_run=parent-run",
        "parent_session=parent-session",
        f"child_session={result['session_id']}",
        f"agent={child_agent}",
        f"run={started.run.id}",
    )
    assert spawned.levelno == logging.INFO
    assert all(field in spawned.getMessage() for field in spawn_fields)
    assert (caller_project, child_agent) in harness.resolver_calls
    assert harness.sessions.get_metadata(child)["is_subagent_session"] is True
    assert harness.sessions.get_metadata(child)["subagent_parent"] == {
        "id": result["id"],
        "agent_id": "parent",
        "session_id": "parent-session",
        "run_id": "parent-run",
        "tool_call_id": "tool-call-one",
        "tool_call_index": 0,
        "project_id": caller_project,
    }


@pytest.mark.parametrize(
    ("caller_agent", "caller_project", "target", "returned_agent"),
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
    harness: SubAgentHarness,
    caller_agent: str,
    caller_project: str | None,
    target: str | None,
    returned_agent: str,
) -> None:
    harness.use_agents({"parent", "lead", "builder"})
    events: list[tuple[str, JsonObject]] = []
    context = make_context(
        agent_id=caller_agent,
        project_id=caller_project,
        emit_hook=lambda event_type, payload: events.append((event_type, payload)),
    )
    child_agent = returned_agent.split("@")[0]
    arguments: JsonObject = {"content": BRIEF}
    if target is not None:
        arguments["agent_id"] = target

    child = await harness.spawn(arguments, context)
    listed = await harness.call({"action": "status"}, context)

    assert child["agent_id"] == returned_agent
    assert child["project_id"] == "vbot"
    # Events and Session metadata keep bare ids.
    assert (events[0][1]["data"]["agent_id"], events[0][1]["data"]["project_id"]) == (
        child_agent,
        "vbot",
    )
    child_address = address(child_agent, child["session_id"], "vbot")
    assert harness.sessions.get_metadata(child_address)["subagent_parent"]["project_id"] == (
        caller_project
    )
    assert [entry["agent_id"] for entry in listed["data"]["subagents"]] == [returned_agent]
    [first] = await harness.started()
    first.run.mark_completed(done("first"))
    await harness.settle()
    assert len(harness.triggers.notices) == 1
    assert returned_agent in harness.triggers.bodies[0]

    continued = await harness.spawn(
        {"agent_id": child["agent_id"], "session_id": child["session_id"], "content": "Go on."},
        context,
    )

    assert continued["agent_id"] == returned_agent
    assert continued["session_id"] == child["session_id"]
    second = (await harness.started(2))[1].run
    assert (second.agent_id, second.project_id, second.session_id) == (
        child_agent,
        "vbot",
        child["session_id"],
    )
    assert harness.resolver_calls[-1] == ("vbot", child_agent)
    assert [session.id for session in harness.sessions.list(child_agent, project_id="vbot")] == [
        child["session_id"]
    ]
    assert harness.sessions.list(child_agent) == []


async def test_allowed_agents_name_other_scopes_by_their_canonical_address(
    harness: SubAgentHarness,
) -> None:
    context = make_context(allowed_agents=["worker@vbot"])

    denied = await harness.call({"content": "spawn", "agent_id": "worker"}, context)
    allowed = await harness.call({"content": "spawn", "agent_id": "worker@vbot"}, context)

    assert denied["error"]["code"] == "agent_not_allowed"
    assert allowed["ok"] is True
    assert allowed["data"]["project_id"] == "vbot"
    assert len(await harness.started()) == 1


async def test_empty_target_policy_allows_only_a_copy_of_yourself(
    harness: SubAgentHarness,
) -> None:
    context = make_context(allowed_agents=[])

    refused = await harness.call({"content": BRIEF, "agent_id": "worker"}, context)
    generic = await harness.call({"content": BRIEF, "agent_id": "general-purpose"}, context)
    own = await harness.call({"content": BRIEF}, context)

    assert refused["error"] == {
        "code": "agent_not_allowed",
        "message": (
            "Agent worker is not available to you as a Sub-Agent. Omit agent_id to delegate "
            "to a copy of yourself; no other Agents are available to you."
        ),
    }
    assert generic["ok"] and own["ok"]
    assert [child.run.agent_id for child in await harness.started(2)] == ["parent", "parent"]
    assert set(harness.resolver_calls) == {(None, "parent")}
    assert harness.sessions.list("worker") == []


async def test_project_caller_cannot_reach_another_project(harness: SubAgentHarness) -> None:
    result = await harness.call({"content": "spawn", "agent_id": "worker@vbot"}, project_id="acme")

    assert result["error"]["code"] == "agent_not_allowed"
    assert harness.resolver_calls == []
    assert harness.manager.started == []


@pytest.mark.parametrize(
    ("arguments", "caller_project", "message"),
    [
        (
            {"prompt": BRIEF, "subagent_type": "Explore"},
            None,
            "Agent not found: Explore. Omit agent_id to delegate to a copy of yourself, or use "
            "one of these Agent ids exactly: worker.",
        ),
        (
            {"content": BRIEF, "agent_id": "workre"},
            None,
            "Agent not found: workre. Omit agent_id to delegate to a copy of yourself, or use "
            "one of these Agent ids exactly: worker.",
        ),
        (
            {"content": BRIEF, "agent_id": "ghost"},
            "acme",
            "Agent not found: ghost. Omit agent_id to delegate to a copy of yourself; no other "
            "Agents are available to you.",
        ),
    ],
)
async def test_unknown_target_is_refused_with_the_choices_before_session_work(
    harness: SubAgentHarness, arguments: JsonObject, caller_project: str | None, message: str
) -> None:
    result = await harness.call(arguments, project_id=caller_project)

    assert result["error"] == {"code": "agent_not_found", "message": message}
    assert harness.manager.started == []
    assert harness.sessions.list(arguments.get("agent_id", "Explore")) == []


@pytest.mark.parametrize("name", ["general-purpose", "General Purpose", "default", "self"])
async def test_generic_worker_name_delegates_to_a_copy_of_yourself(
    harness: SubAgentHarness, name: str
) -> None:
    result = await harness.spawn({"prompt": BRIEF, "subagent_type": name})

    assert [child.run.agent_id for child in await harness.started()] == ["parent"]
    assert result["note"] == (
        f'agent_id "{name}" is not an Agent id, so a copy of you runs this task, as when '
        f"agent_id is omitted. {RUNNING_NOTE}"
    )


async def test_generic_name_selects_an_agent_with_exactly_that_id(
    harness: SubAgentHarness,
) -> None:
    harness.use_agents({"parent", "worker", "default"})

    result = await harness.spawn({"prompt": BRIEF, "subagent_type": "default"})

    assert [child.run.agent_id for child in await harness.started()] == ["default"]
    assert result["note"] == RUNNING_NOTE


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (
            ModelConfigurationError("model is not usable in this instance"),
            "invalid_arguments",
            "model is not usable in this instance",
        ),
        (
            AgentResolutionError("agent 'stranded' has no usable model"),
            "agent_unavailable",
            "agent 'stranded' has no usable model",
        ),
        (ResolutionProjectNotFoundError("Project not found: acme"), "project_not_found", "acme"),
        (
            ResolutionAgentNotFoundError("agent 'stranded' is not on project 'acme' team"),
            "agent_not_found",
            "is not on project 'acme' team",
        ),
    ],
)
async def test_target_that_cannot_run_fails_before_session_work(
    harness: SubAgentHarness, error: Exception, code: str, message: str
) -> None:
    harness.runtime.agent_resolver = RaisingResolver(error)

    result = await harness.call(
        {"content": "spawn", "agent_id": "stranded", "model": "openai/ghost-model"},
        project_id="acme",
    )

    assert result["error"]["code"] == code
    assert message in result["error"]["message"]
    if code == "agent_unavailable":
        assert result["error"]["retryable"] is False
        assert "stranded@acme" in result["error"]["message"]
    assert harness.manager.started == []
    assert harness.sessions.list("stranded", project_id="acme") == []


@pytest.mark.parametrize(
    ("settings", "prior_spawns", "context_depth", "code"),
    [
        ({"max_subagent_depth": 2}, 0, 2, "subagent_depth_exceeded"),
        ({"max_subagents_per_turn": 1}, 1, 0, "subagent_limit_exceeded"),
    ],
)
async def test_delegation_limits_refuse_further_work(
    harness: SubAgentHarness,
    settings: JsonObject,
    prior_spawns: int,
    context_depth: int,
    code: str,
) -> None:
    harness.storage.settings = settings
    for _ in range(prior_spawns):
        await harness.spawn({"content": "first"})

    result = await harness.call({"content": "spawn"}, nesting_depth=context_depth)

    assert result["error"]["code"] == code
    assert len(harness.manager.started) == prior_spawns


@pytest.mark.parametrize(
    ("settings", "expected"),
    [({"subagent_timeout_minutes": 17}, 17), ({}, DEFAULT_SUBAGENT_TIMEOUT_MINUTES)],
)
async def test_foreground_timeout_minutes_reports_the_enforced_bound(
    harness: SubAgentHarness, settings: JsonObject, expected: int
) -> None:
    harness.storage.settings = settings

    assert harness.coordinator.foreground_timeout_minutes() == expected


@pytest.mark.parametrize(
    ("arguments", "asked_to_wait"),
    [
        ({"background": False}, True),
        ({"run_in_background": False}, True),
        ({"blocking": True}, True),
        ({"blocking": "true"}, True),
        ({"non_blocking": "false"}, True),
        ({"background": True}, False),
        ({"non_blocking": "true"}, False),
        ({"blocking": False}, False),
        ({}, False),
    ],
)
async def test_top_level_delegation_runs_in_the_background(
    harness: SubAgentHarness, arguments: JsonObject, asked_to_wait: bool
) -> None:
    result = await harness.spawn({"content": "do work", **arguments})

    assert result["status"] == "running"
    assert result["delivery"] == "automatic"
    assert result["id"].startswith("sub_")
    assert "run_id" not in result and "queue_item_id" not in result
    assert "activity_file" not in result
    assert Path(activity_path_from_note(result["activity_note"])).exists()
    expected = f"{SUBAGENT_BACKGROUND_UNAVAILABLE_NOTE} {RUNNING_NOTE}" if asked_to_wait else None
    assert result["note"] == (expected or RUNNING_NOTE)
    # The child loop runs one level deeper than its Parent.
    assert harness.loop.tasks["do work"].nesting_depth == 1


@pytest.mark.parametrize(
    ("arguments", "note"),
    [
        # A Sub-Agent's Sub-Agents always run in the foreground.
        ({"background": True}, SUBAGENT_FOREGROUND_ONLY_NOTE),
        ({"run_in_background": "true"}, SUBAGENT_FOREGROUND_ONLY_NOTE),
        ({"blocking": False}, SUBAGENT_FOREGROUND_ONLY_NOTE),
        ({"background": False}, None),
        ({"blocking": True}, None),
    ],
)
async def test_nested_delegation_waits_whatever_mode_was_requested(
    harness: SubAgentHarness, arguments: JsonObject, note: str | None
) -> None:
    call = harness.call_in_background({"content": "do work", **arguments}, nesting_depth=1)
    [child] = await harness.started()
    child.run.mark_completed(done())
    result = await call

    assert result["ok"] is True, result
    assert result["data"]["delivery"] == "inline"
    assert result["data"]["result"] == "done"
    assert result["data"].get("note") == note
    assert harness.loop.tasks["do work"].nesting_depth == 2


def _complete(run: Any, parent: Any) -> None:
    run.mark_completed(done("child done", usage={"input_tokens": 1, "output_tokens": 2}))


def _fail(run: Any, parent: Any) -> None:
    run.mark_failed(RuntimeError("provider failed"))


def _interrupt(run: Any, parent: Any) -> None:
    partial = done("partial result", interrupted=True, interruption_cause="network")
    run.mark_interrupted(RunInterruptedError("network", result=partial))


def _user_cancel_through_parent(run: Any, parent: Any) -> None:
    parent.request_cancel(reason="user")
    assert run.cancel_requested and run.cancel_reason == "user"
    run.mark_cancelled()


def _cancel(run: Any, parent: Any) -> None:
    run.request_cancel()
    run.mark_cancelled()


@pytest.mark.parametrize(
    ("settle", "expected"),
    [
        (
            _complete,
            {
                "status": "completed",
                "result": "child done",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        ),
        (_fail, {"status": "failed", "result": "provider failed"}),
        (
            _interrupt,
            {
                "status": "interrupted",
                "result": "partial result",
                "interrupted": True,
                "interruption_cause": "network",
            },
        ),
        (
            _user_cancel_through_parent,
            {
                "status": "cancelled",
                "result": SUBAGENT_USER_CANCEL_MESSAGE,
                "cancelled_by_user": True,
            },
        ),
        (_cancel, {"status": "cancelled", "result": None}),
    ],
)
async def test_foreground_result_reports_how_the_child_ended(
    harness: SubAgentHarness, settle: Any, expected: JsonObject
) -> None:
    call = harness.call_in_background({"content": "do work", "agent_id": "worker"}, nesting_depth=1)
    [child] = await harness.started()
    settle(child.run, harness.manager.parent_run)
    result = await call

    assert result["ok"] is True, result
    data = result["data"]
    assert {key: data.get(key) for key in expected} == expected
    assert data["delivery"] == "inline"
    assert "run_id" not in data and "queue_item_id" not in data
    if "cancelled_by_user" not in expected:
        assert "cancelled_by_user" not in data
    if expected.get("interrupted"):
        assert "`worker`" in data["note"] and f"`{data['session_id']}`" in data["note"]


async def test_cancelling_the_waiting_call_is_not_swallowed(harness: SubAgentHarness) -> None:
    call = harness.call_in_background({"content": "do work"}, nesting_depth=1)
    await harness.started()

    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call


async def test_session_started_events_precede_the_foreground_result(
    harness: SubAgentHarness,
) -> None:
    events: list[tuple[str, JsonObject]] = []
    context = make_context(
        nesting_depth=1, emit_hook=lambda event_type, payload: events.append((event_type, payload))
    )

    call = harness.call_in_background({"content": "spawn"}, context)
    [child] = await harness.started()

    activity_file = events[0][1]["data"]["activity_file"]
    work_id = events[0][1]["data"]["id"]
    assert isinstance(activity_file, str) and Path(activity_file).exists()
    tool_call = {"id": "tool-call-one", "index": 0, "name": "subagent"}
    started_data = {
        "id": work_id,
        "agent_id": "parent",
        "session_id": child.run.session_id,
        "status": "running",
        "delivery": "inline",
        "activity_file": activity_file,
    }
    assert events == [
        (SUBAGENT_SESSION_STARTED_EVENT, {"tool_call": tool_call, "data": started_data}),
        (
            SUBAGENT_SESSION_STARTED_EVENT,
            {"tool_call": tool_call, "data": {**started_data, "run_id": child.run.id}},
        ),
    ]
    child.run.mark_completed(done())
    result = await call
    assert (result["data"]["id"], result["data"]["delivery"]) == (work_id, "inline")


async def test_run_local_overrides_reach_resolution_and_the_child_loop(
    harness: SubAgentHarness,
) -> None:
    await harness.spawn({"content": "do work", "model": "openai/gpt-mini", "thinking_effort": ""})
    await harness.spawn(
        {"content": "more work", "model": "openai/gpt-mini", "thinking_effort": "none"}
    )
    await harness.spawn(
        {"content": "deep work", "model": "openai/gpt-mini", "thinking_effort": "high"}
    )

    efforts = [
        harness.loop.tasks[task].overrides.thinking_effort
        for task in ("do work", "more work", "deep work")
    ]
    assert efforts == [None, "none", "high"]
    assert all(
        harness.loop.tasks[task].overrides.model == "openai/gpt-mini"
        for task in ("do work", "more work", "deep work")
    )
    # Target validation resolves the Agent with the same overrides the child Run uses.
    assert harness.runtime.agent_resolver.calls[0][2] == harness.loop.tasks["do work"].overrides


async def test_execution_owner_is_inherited_without_session_grants(
    harness: SubAgentHarness,
) -> None:
    owner = RunExecutionOwner("fixture", "group", "peer", "generation", "epoch")
    context = replace(make_context(), execution_owner=owner, session_tool_grants=("hidden",))

    result = await harness.spawn({"content": "work", "agent_id": "worker"}, context)

    [child] = await harness.started()
    assert child.admission.owner == owner
    assert child.admission.contributes_to_agent_activity is False
    assert harness.sessions.temporary_binding(address("worker", result["session_id"])) is None


async def test_spawn_opens_the_child_session_off_the_event_loop(harness: SubAgentHarness) -> None:
    create = harness.sessions.create
    entered = threading.Event()
    release = threading.Event()
    threads: list[int] = []

    def blocked_create(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        return create(*args, **kwargs)

    harness.sessions.create = blocked_create  # type: ignore[method-assign]
    call = harness.call_in_background({"content": "spawn", "agent_id": "worker"})
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # The Event Loop keeps serving other work while the Session opens.
        await harness.settle()
        assert harness.manager.started == []
    finally:
        release.set()
    result = await asyncio.wait_for(call, timeout=5)

    assert result["ok"] is True
    assert threads and threading.get_ident() not in threads
    metadata = harness.sessions.get_metadata(address("worker", result["data"]["session_id"]))
    assert metadata["subagent_parent"]["session_id"] == "parent-session"


async def test_each_run_gets_its_own_activity_file(harness: SubAgentHarness) -> None:
    harness.sessions.create("parent", session_id="existing-sub-session")
    arguments = {"agent_id": "parent", "session_id": "existing-sub-session"}

    first = await harness.spawn({"content": "first", **arguments})
    second = await harness.spawn({"content": "second", **arguments})

    first_path = Path(activity_path_from_note(first["activity_note"]))
    second_path = Path(activity_path_from_note(second["activity_note"]))
    assert first_path != second_path
    assert first_path.exists() and second_path.exists()


async def test_activity_file_failure_does_not_block_delegation(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_create(_category: str, _suffix: str) -> object:
        raise OSError("disk unavailable")

    monkeypatch.setattr(harness.storage.temporary_files, "create", fail_create)

    result = await harness.spawn({"content": "spawn"})

    assert "activity_file" not in result
    assert "activity_note" not in result
    assert len(await harness.started()) == 1


@pytest.mark.parametrize(
    ("arguments", "auto_title", "existing"),
    [
        ({"description": "A" * 80}, "A" * 48, False),
        (
            {
                "content": "  Inspect   session\ntitles and report the complete implementation "
                "outcome safely  ",
                "description": "   ",
            },
            "Inspect session titles and report the complete i",
            False,
        ),
        ({"description": "Verify remaining edge case"}, "Original automatic title", True),
    ],
)
async def test_new_child_session_takes_its_title_from_the_task(
    harness: SubAgentHarness, arguments: JsonObject, auto_title: str, existing: bool
) -> None:
    call: JsonObject = {"content": "Inspect the Tool contract.", "agent_id": "worker", **arguments}
    if existing:
        harness.sessions.create("worker", session_id="existing")
        harness.sessions.set_auto_title(address("worker", "existing"), "Original automatic title")
        harness.sessions.set_title(address("worker", "existing"), "Manual session title")
        call["session_id"] = "existing"

    result = await harness.spawn(call)

    metadata = harness.sessions.get_metadata(address("worker", result["session_id"]))
    assert metadata["auto_title"] == auto_title
    assert metadata["auto_title_initialized"] is True
    assert metadata.get("title") == ("Manual session title" if existing else None)


@pytest.mark.parametrize(
    ("caller_project", "session_id"), [(None, "new-review"), ("acme", "existing")]
)
async def test_existing_session_is_continued_whatever_its_id_says(
    harness: SubAgentHarness, caller_project: str | None, session_id: str
) -> None:
    child = address("worker", session_id, caller_project)
    harness.sessions.create("worker", session_id=session_id, project_id=caller_project)
    harness.sessions.set_metadata(child, {"platform": "telegram"})

    result = await harness.spawn(
        {"content": BRIEF, "agent_id": "worker", "session_id": session_id},
        project_id=caller_project,
    )

    assert result["session_id"] == session_id
    assert result["note"] == RUNNING_NOTE
    [started] = await harness.started()
    assert (started.run.session_id, started.run.project_id) == (session_id, caller_project)
    assert len(harness.sessions.list("worker", project_id=caller_project)) == 1
    metadata = harness.sessions.get_metadata(child)
    assert metadata["platform"] == "telegram"
    assert metadata["is_subagent_session"] is True
    assert metadata["subagent_parent"]["session_id"] == "parent-session"


@pytest.mark.parametrize("session_id", ["new-review", "audit-do-not-use-placeholder", "INVALID_X"])
async def test_stand_in_session_id_that_names_no_session_starts_a_new_one(
    harness: SubAgentHarness, session_id: str
) -> None:
    result = await harness.spawn({"content": BRIEF, "agent_id": "worker", "session_id": session_id})

    assert result["session_id"] != session_id
    [started] = await harness.started()
    assert (started.run.agent_id, started.run.session_id) == ("worker", result["session_id"])
    assert result["note"] == (
        f'No Session "{session_id}" exists and that value reads as a stand-in, so this task '
        f"started a new Session. Continue it with the session_id of this result. {RUNNING_NOTE}"
    )


@pytest.mark.parametrize(
    ("arguments", "caller_project", "owner_hint"),
    [
        ({"agent_id": "worker", "session_id": "audit-storage"}, None, False),
        ({"session_id": "ses_unknown"}, None, True),
        # A project caller never finds a Session of the identity layout.
        ({"agent_id": "worker", "session_id": "identity-only"}, "acme", False),
    ],
)
async def test_session_id_that_names_no_session_is_refused(
    harness: SubAgentHarness,
    arguments: JsonObject,
    caller_project: str | None,
    owner_hint: bool,
) -> None:
    harness.sessions.create("worker", session_id="identity-only")
    tracked = await harness.spawn({"content": "first", "agent_id": "worker"}, project_id=None)

    result = await harness.call({"content": BRIEF, **arguments}, project_id=caller_project)

    assert result["error"]["code"] == "session_not_found"
    message = result["error"]["message"]
    owner = arguments.get("agent_id", "parent")
    assert message.startswith(f"No Session {arguments['session_id']} exists for Agent {owner}")
    assert message.endswith('repeat this call without "session_id".')
    assert ("If that Session belongs to another Agent" in message) is owner_hint
    tracked_text = (
        f"Tracked work: {tracked['id']} (agent_id worker, session_id {tracked['session_id']}, "
        "running)."
    )
    assert (tracked_text in message) is (caller_project is None)
    assert len(harness.manager.started) == 1


async def test_session_id_alone_continues_your_own_or_a_tracked_session(
    harness: SubAgentHarness,
) -> None:
    harness.sessions.create("parent", session_id="own-session")
    child = await harness.spawn({"content": BRIEF, "agent_id": "worker"})
    (await harness.started())[0].run.mark_completed(done())

    own = await harness.spawn({"content": "Go on.", "session_id": "own-session"})
    tracked = await harness.spawn({"content": "Go on.", "session_id": child["session_id"]})

    assert (own["agent_id"], own["session_id"]) == ("parent", "own-session")
    assert (tracked["agent_id"], tracked["session_id"]) == ("worker", child["session_id"])
    runs = [child.run for child in await harness.started(3)]
    assert [(run.agent_id, run.session_id) for run in runs[1:]] == [
        ("parent", "own-session"),
        ("worker", child["session_id"]),
    ]


async def test_run_ignores_a_label_id_and_names_the_assigned_id(harness: SubAgentHarness) -> None:
    result = await harness.spawn({"action": "run", "content": BRIEF, "id": "audit-execution"})

    assert result["id"].startswith("sub_")
    assert result["note"] == (
        'id "audit-execution" was ignored: run assigns the work id above; use that id with '
        f"status or cancel. {RUNNING_NOTE}"
    )


@pytest.mark.parametrize("action", [None, "continue"])
@pytest.mark.parametrize("tracked", [True, False])
async def test_work_id_with_a_task_asks_whether_to_continue(
    harness: SubAgentHarness, tracked: bool, action: str | None
) -> None:
    child = await harness.spawn({"content": BRIEF, "agent_id": "worker"})
    work_id = child["id"] if tracked else "sub_abcdefghijkl"
    arguments: JsonObject = {"content": "Also check tests.", "id": work_id}
    if action is not None:
        arguments["action"] = action

    result = await harness.call(arguments)

    continuation = (
        'To continue its Session, call {"agent_id": "worker", "session_id": '
        f'"{child["session_id"]}", "content": "<follow-up>"}}.'
        if tracked
        else "To continue that Sub-Agent's Session, pass the agent_id and session_id from its "
        'result instead of "id".'
    )
    assert result["error"] == {
        "code": "invalid_arguments",
        "message": (
            f'subagent was not run: "id" names Sub-Agent work {work_id}, so it is unclear '
            f"whether to continue that Sub-Agent or to start new work. {continuation} To start "
            'new work, repeat this call without "id".'
        ),
    }
    assert len(harness.manager.started) == 1


async def test_copied_work_id_with_its_session_continues_that_session(
    harness: SubAgentHarness,
) -> None:
    child = await harness.spawn({"content": BRIEF, "agent_id": "worker"})
    (await harness.started())[0].run.mark_completed(done())

    result = await harness.spawn(
        {
            "action": "run",
            "content": "Go on.",
            "id": child["id"],
            "agent_id": "worker",
            "session_id": child["session_id"],
        }
    )

    assert result["session_id"] == child["session_id"]
    assert result["note"] == RUNNING_NOTE
    continued = (await harness.started(2))[1]
    assert (continued.run.agent_id, continued.run.session_id) == ("worker", child["session_id"])
    assert await continued.task() == "Go on."


async def test_work_id_of_another_session_conflicts_with_session_id(
    harness: SubAgentHarness,
) -> None:
    child = await harness.spawn({"content": BRIEF, "agent_id": "worker"})
    other = harness.sessions.create("worker")

    result = await harness.call(
        {"content": "Go on.", "id": child["id"], "agent_id": "worker", "session_id": other.id}
    )

    assert result["error"]["message"] == (
        f"subagent was not run: work {child['id']} belongs to Session {child['session_id']}, "
        f"but session_id is {other.id}. To continue work {child['id']}, call "
        f'{{"agent_id": "worker", "session_id": "{child["session_id"]}", "content": '
        f'"<follow-up>"}}; to continue Session {other.id}, repeat this call without "id".'
    )
    assert len(harness.manager.started) == 1


async def test_continue_operation_with_its_session_continues_it(harness: SubAgentHarness) -> None:
    # vBot's earlier contract spelled a continuation as operation "continue".
    session = harness.sessions.create("worker")

    await harness.spawn(
        {
            "request": {
                "operation": "continue",
                "agent_id": "worker",
                "session_id": session.id,
                "content": "Go on.",
                "background": True,
            }
        }
    )

    [child] = await harness.started()
    assert (child.run.agent_id, child.run.session_id) == ("worker", session.id)
    assert await child.task() == "Go on."


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"agent_id": "parent", "session_id": "parent-session"},
            "session_id is your own active Session; a Sub-Agent needs another Session. Repeat "
            'this call without "session_id" to start a new one.',
        ),
        (
            {"agent_id": "worker@vbot@extra", "session_id": "child"},
            "agent address must be 'agent' or 'agent@projekt', got: 'worker@vbot@extra'",
        ),
    ],
)
async def test_unusable_target_address_is_refused(
    harness: SubAgentHarness, arguments: JsonObject, message: str
) -> None:
    result = await harness.call({"content": BRIEF, **arguments})

    assert result["error"] == {"code": "invalid_arguments", "message": message}
    assert harness.manager.started == []
