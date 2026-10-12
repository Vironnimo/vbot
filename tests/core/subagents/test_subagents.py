"""Sub-Agent behavior at the ``subagent`` and ``message_parent`` Tools.

A Sub-Agent is a Session linked to its Parent Session. ``run`` starts one in the
background; vBot forwards the final answer of every turn the Parent or a
delivery started there; ``send`` reaches it at its next step or starts a turn;
``background_tasks`` lists, checks and stops the caller's whole tree; the user's
first message in the Session ends forwarding.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.subagents.subagents as subagents_module
from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.projects import AgentResolutionError, ResolutionProjectNotFoundError
from core.runs import Run, RunAdmission, RunAdmissionBlockedError, RunKind, RunStatus
from core.sessions import (
    SESSION_WORKING_PROJECT_META_KEY,
    SUBAGENT_PARENT_META_KEY,
    SUBAGENT_TAKEN_OVER_AT_META_KEY,
    SessionAddress,
)
from core.subagents import SubAgentState
from core.subagents._constants import (
    FACTS_NO_SIBLINGS_PENDING_TEXT,
    FACTS_NOTHING_RUNNING_TEXT,
    MESSAGE_PARENT_NOT_SUBAGENT_MESSAGE,
)
from tests.core.subagents.subagents_test_support import (
    SubAgentHarness,
    address,
)
from tests.core.subagents.subagents_test_support import (
    harness as harness,
)
from tests.core.tools.command_manager_helpers import Harness as CommandHarness
from tests.core.tools.command_manager_helpers import command_harness

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def commands(harness: SubAgentHarness, tmp_path: Path) -> AsyncIterator[CommandHarness]:
    """Background commands of the harness's Sessions, on fake processes."""
    async with command_harness(tmp_path, max_live=4) as subject:
        harness.use_commands(subject.manager)
        yield subject


async def _background_command(
    commands: CommandHarness, owner: SessionAddress, description: str
) -> str:
    command_id, _process = await commands.launch(owner=owner, description=description)
    commands.manager.to_background(command_id, deliver=True)
    return command_id


async def test_run_starts_a_linked_subagent_in_the_background(harness: SubAgentHarness) -> None:
    harness.loop.hold("review")

    data = await harness.spawn("review", agent_id="worker")

    assert data["status"] == "running"
    assert data["id"].startswith("sub_")
    assert data["agent_id"] == "worker"
    child = harness.subagent_session(data["id"])
    assert child.session_id == data["session_id"]
    link = harness.sessions.metadata_value(child, SUBAGENT_PARENT_META_KEY)
    assert (link["id"], link["agent_id"], link["session_id"]) == (
        data["id"],
        "parent",
        harness.parent.session_id,
    )
    assert harness.sessions.get_metadata(child)["auto_title"] == "Do review"
    [turn] = harness.loop.turns
    assert (turn.content, turn.parent_agent_input) == ("review", True)
    await harness.until(lambda: turn.run is not None)
    assert turn.run is not None
    assert turn.run.run_kind is RunKind.SUBAGENT
    assert turn.run.work_id == data["id"]
    assert "activity_note" in data
    assert harness.triggers.notices == []


async def test_run_without_description_is_refused_with_the_corrected_call(
    harness: SubAgentHarness,
) -> None:
    result = await harness.call({"content": "review the module"})

    assert result["ok"] is False
    message = result["error"]["message"]
    assert '"description": "<3-5 word title>"' in message
    assert '"content": "review the module"' in message
    assert harness.loop.turns == []


@pytest.mark.parametrize(
    ("caller_project", "allowed", "target", "code"),
    [
        # allowed_agents names other scopes by their qualified address.
        (None, ["worker@vbot"], "worker", "agent_not_allowed"),
        # An empty list leaves only a copy of the caller.
        (None, [], "worker", "agent_not_allowed"),
        # A Project Agent stays inside its own Project whatever the policy says.
        ("acme", None, "worker@vbot", "agent_not_allowed"),
        (None, None, "workre", "agent_not_found"),
        # A built-in Agent resolves as an Agent but is no delegation target.
        (None, None, "librarian", "agent_not_found"),
        (None, None, "live-backend", "agent_not_found"),
    ],
    ids=["other-scope", "self-only", "other-project", "unknown", "librarian", "live-agent"],
)
async def test_targets_outside_the_callers_choices_are_refused_with_them(
    harness: SubAgentHarness,
    caller_project: str | None,
    allowed: list[str] | None,
    target: str,
    code: str,
) -> None:
    caller = address("parent", "caller", caller_project)

    result = await harness.call(
        {"description": "Do review", "content": "review", "agent_id": target},
        caller,
        allowed_agents=allowed,
    )

    assert result["error"]["code"] == code
    assert "Omit agent_id to delegate to a copy of yourself" in result["error"]["message"]
    assert harness.loop.turns == []
    assert harness.sessions.list("worker") == []


async def test_subagent_in_another_scope_is_addressed_by_its_qualified_agent_id(
    harness: SubAgentHarness,
) -> None:
    result = await harness.call(
        {"description": "Do review", "content": "review", "agent_id": "worker@vbot"},
        allowed_agents=["worker@vbot"],
    )

    assert result["ok"], result
    data = result["data"]
    assert (data["agent_id"], data["project_id"]) == ("worker@vbot", "vbot")
    child = harness.subagent_session(data["id"])
    assert (child.agent_id, child.project_id) == ("worker", "vbot")
    await harness.settle()
    [answer] = harness.triggers.to(harness.parent)
    assert f"agent_id worker@vbot, session_id {data['session_id']}" in answer.body
    sent = await harness.call({"action": "send", "id": data["id"], "content": "more"})
    assert sent["ok"], sent
    assert sent["data"]["agent_id"] == "worker@vbot"


@pytest.mark.parametrize(
    ("target", "working_project_id"),
    [
        pytest.param("worker", "alpha", id="identity-target"),
        pytest.param("parent", "alpha", id="self-copy"),
        pytest.param("worker@team", "team", id="team-target-in-its-project"),
    ],
)
async def test_a_subagent_works_in_its_parents_project_unless_it_is_a_team_agent(
    harness: SubAgentHarness, target: str, working_project_id: str
) -> None:
    harness.resolver.projects.add("alpha")

    result = await harness.call(
        {"description": "Do review", "content": "review", "agent_id": target},
        allowed_agents=["*"],
        working_project_id="alpha",
    )
    await harness.settle()

    assert result["ok"], result
    child = harness.subagent_session(result["data"]["id"])
    stored = harness.sessions.metadata_value(child, SESSION_WORKING_PROJECT_META_KEY)
    assert stored == working_project_id
    [turn] = harness.loop.turns
    assert turn.run is not None
    assert turn.run.working_project_id == working_project_id


async def test_a_subagent_whose_project_is_gone_cannot_continue(
    harness: SubAgentHarness,
) -> None:
    harness.resolver.projects.add("alpha")
    data = await harness.call(
        {"description": "Do review", "content": "review"}, working_project_id="alpha"
    )
    await harness.settle()
    harness.resolver.projects.discard("alpha")

    result = await harness.call({"action": "send", "id": data["data"]["id"], "content": "more"})

    assert result["error"]["code"] == "project_not_found"
    assert result["error"]["message"] == (
        f"subagent was not run: Sub-Agent {data['data']['id']} works in Project alpha, "
        "which no longer exists, so it cannot continue. "
        "Delegate the work to a new Sub-Agent instead."
    )
    assert [turn.content for turn in harness.loop.turns] == ["review"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AgentResolutionError("agent 'worker' has no usable model"), "agent_unavailable"),
        (ResolutionProjectNotFoundError("Project not found: acme"), "project_not_found"),
    ],
)
async def test_target_that_cannot_run_fails_before_session_work(
    harness: SubAgentHarness, error: Exception, code: str
) -> None:
    harness.resolver.failures["worker"] = error

    result = await harness.call(
        {"description": "Do review", "content": "review", "agent_id": "worker"}
    )

    assert result["error"]["code"] == code
    if code == "agent_unavailable":
        assert result["error"]["retryable"] is False
        assert (
            "Agent worker cannot run: agent 'worker' has no usable model"
            in (result["error"]["message"])
        )
    assert harness.loop.turns == []
    assert harness.sessions.list("worker") == []


async def test_model_and_thinking_effort_belong_to_the_subagent_session(
    harness: SubAgentHarness,
) -> None:
    harness.resolver.unusable_models.add("acme/broken")
    refused = await harness.call(
        {"description": "Do review", "content": "review", "model": "acme/broken"}
    )
    assert refused["error"]["code"] == "invalid_arguments"
    assert harness.loop.turns == []

    data = await harness.spawn("review", model="acme/good", thinking_effort="low")
    child = harness.subagent_session(data["id"])
    stored = await harness.resolver.session_overrides_async(child)
    assert (stored.model, stored.thinking_effort) == ("acme/good", "low")
    await harness.settle()

    # A stored Model that can no longer run stops the next message, naming the fix.
    harness.resolver.unusable_models.add("acme/good")
    blocked = await harness.call({"action": "send", "id": data["id"], "content": "more"})
    assert blocked["error"]["code"] == "invalid_arguments"
    assert '"model"' in blocked["error"]["message"]

    sent = await harness.call(
        {"action": "send", "id": data["id"], "content": "more", "model": "acme/other"}
    )
    assert sent["ok"], sent
    stored = await harness.resolver.session_overrides_async(child)
    assert (stored.model, stored.thinking_effort) == ("acme/other", "low")


async def test_every_answer_reaches_the_parent_with_what_is_still_running(
    harness: SubAgentHarness,
) -> None:
    harness.loop.answers["review"] = "Found two bugs."
    data = await harness.spawn("review")
    await harness.settle()

    [notice] = harness.triggers.to(harness.parent)
    child = harness.subagent_session(data["id"])
    run = harness.loop.turns[0].run
    assert run is not None
    assert notice.wake is True
    assert notice.origin_run_id == run.id
    assert f"### Sub-Agent {data['id']} — Do review" in notice.body
    assert f"session_id {child.session_id}" in notice.body
    assert "Its turn completed." in notice.body
    assert "Found two bugs." in notice.body
    assert notice.body.endswith(FACTS_NOTHING_RUNNING_TEXT)

    # A delivery's turn in the Sub-Agent Session forwards too; the user's never does.
    await harness.start_turn(child, "background result arrived")
    await harness.settle()
    await harness.start_turn(child, "user question", kind=RunKind.USER)
    await harness.settle()

    bodies = [notice.body for notice in harness.triggers.to(harness.parent)]
    assert len(bodies) == 2
    assert "answer to background result arrived" in bodies[1]


@pytest.mark.parametrize(
    ("progression", "outcome"),
    [
        ("answer", "Its turn completed."),
        ("partial-answer", "Its turn was cancelled."),
        ("compaction", "Its turn completed."),
        ("steering", "Its turn was cancelled."),
        ("steering-tool", "Its turn was cancelled."),
        ("steering-reasoning", "Its turn was cancelled."),
        ("steering-answer", "Its turn completed."),
    ],
)
async def test_a_stopped_turn_forwards_its_latest_progression_outcome(
    harness: SubAgentHarness, progression: str, outcome: str
) -> None:
    data = await harness.spawn("review")
    await harness.settle()
    child = harness.subagent_session(data["id"])
    answered = asyncio.Event()

    async def execute(run: Run) -> ChatMessage:
        session = (await harness.sessions.get_async(child)).for_run(run.id)
        answer = ChatMessage.assistant(
            model="fixture", content="All fixed.", interrupted=progression == "partial-answer"
        )
        messages = [answer]
        if progression == "compaction":
            messages.extend(
                [
                    ChatMessage.compaction_checkpoint(
                        summary="The task was completed.",
                        projection=[answer],
                        compacted_token_count=1,
                    ),
                    ChatMessage.note("Context compacted."),
                ]
            )
        if progression.startswith("steering"):
            messages.append(ChatMessage.user("Also finish the follow-up task."))
        if progression == "steering-tool":
            messages.append(
                ChatMessage.assistant(
                    model="fixture",
                    content=None,
                    tool_calls=[ToolCall(id="follow-up-call", name="probe", arguments={})],
                )
            )
        if progression == "steering-reasoning":
            messages.append(
                ChatMessage.assistant(
                    model="fixture",
                    content=None,
                    reasoning="Checking the follow-up.",
                    interrupted=True,
                )
            )
        if progression == "steering-answer":
            messages.append(ChatMessage.assistant(model="fixture", content="Follow-up fixed."))
        await session.append_many_async(messages)
        answered.set()
        # Compaction after the answer, for example, until Stop arrives.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    run = await harness.manager.start(
        child, execute, admission=RunAdmission(run_kind=RunKind.SYSTEM)
    )
    await answered.wait()
    run.request_cancel()
    await harness.settle()

    body = harness.triggers.to(harness.parent)[-1].body
    assert outcome in body
    assert ("Follow-up fixed." if progression == "steering-answer" else "All fixed.") in body


async def test_answer_names_the_work_the_subagent_still_runs(
    harness: SubAgentHarness, commands: CommandHarness
) -> None:
    outer = await harness.spawn("outer")
    await harness.settle()
    outer_session = harness.subagent_session(outer["id"])
    harness.loop.hold("inner")
    inner = await harness.spawn("inner", session=outer_session)
    tests = await _background_command(commands, outer_session, "Run the tests")
    ended = await _background_command(commands, outer_session, "Build")
    commands.launcher.processes[-1].exit(0)
    await commands.finish(ended)

    await harness.start_turn(outer_session, "status please")
    await harness.until(lambda: len(harness.triggers.to(harness.parent)) == 2)

    body = harness.triggers.to(harness.parent)[1].body
    assert (
        f"Still running for this Sub-Agent: background command {tests} (Run the tests); "
        f"Sub-Agent {inner['id']} (Do inner)."
    ) in body


async def test_answer_names_the_parents_other_subagents_whose_answers_are_still_to_come(
    harness: SubAgentHarness,
) -> None:
    gate = harness.loop.hold("slow")
    slow = await harness.spawn("slow")
    await harness.spawn("fast")
    await harness.until(lambda: len(harness.triggers.to(harness.parent)) == 1)

    first = harness.triggers.to(harness.parent)[0].body
    assert first.endswith(
        f"{FACTS_NOTHING_RUNNING_TEXT}\n"
        f"Answers still to come from your other Sub-Agents: {slow['id']} (Do slow)."
    )

    gate.set()
    await harness.settle()
    last = harness.triggers.to(harness.parent)[1].body
    assert last.endswith(f"{FACTS_NOTHING_RUNNING_TEXT}\n{FACTS_NO_SIBLINGS_PENDING_TEXT}")


async def test_send_to_a_working_subagent_reaches_its_running_turn(
    harness: SubAgentHarness,
) -> None:
    harness.loop.hold("review")
    data = await harness.spawn("review")

    result = await harness.call({"action": "send", "id": data["id"], "content": "also tests"})

    assert result["ok"], result
    assert result["data"]["status"] == "steered"
    child = harness.subagent_session(data["id"])
    [queued] = harness.manager.list_queued(
        child.agent_id, child.session_id, project_id=child.project_id
    )
    assert queued.steering is True
    assert queued.display_content == "also tests"


async def test_send_to_an_idle_subagent_starts_its_next_turn(harness: SubAgentHarness) -> None:
    data = await harness.spawn("review")
    await harness.settle()

    result = await harness.call({"action": "send", "id": data["id"], "content": "now fix them"})
    await harness.settle()

    assert result["data"]["status"] == "started"
    assert [turn.content for turn in harness.loop.turns] == ["review", "now fix them"]
    assert harness.loop.turns[1].parent_agent_input is True
    bodies = [notice.body for notice in harness.triggers.to(harness.parent)]
    assert "answer to now fix them" in bodies[-1]


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "continue", "id": "{id}", "content": "more"},
        {"id": "{id}", "content": "more"},
        {"content": "more", "session_id": "{session_id}"},
        {"action": "message", "session_id": "{session_id}", "content": "more"},
    ],
    ids=["continue-word", "implied-by-id", "run-with-session", "session-only"],
)
async def test_other_spellings_of_send_reach_the_same_subagent(
    harness: SubAgentHarness, arguments: dict[str, str]
) -> None:
    data = await harness.spawn("review")
    await harness.settle()
    call = {
        key: value.format(id=data["id"], session_id=data["session_id"])
        for key, value in arguments.items()
    }

    result = await harness.call(call)
    await harness.settle()

    assert result["ok"], result
    assert result["data"]["id"] == data["id"]
    assert harness.loop.turns[-1].content == "more"


async def test_send_reaches_only_direct_subagents(harness: SubAgentHarness) -> None:
    outer = await harness.spawn("outer")
    await harness.settle()
    inner = await harness.spawn("inner", session=harness.subagent_session(outer["id"]))
    await harness.settle()
    stranger = await harness.spawn("stranger", session=harness.sessions.create("parent").address)
    await harness.settle()

    nested = await harness.call({"action": "send", "id": inner["id"], "content": "hi"})
    without_tasks = await harness.call(
        {"action": "send", "id": inner["id"], "content": "hi"}, allowed_tools=["subagent"]
    )
    foreign = await harness.call({"action": "send", "id": stranger["id"], "content": "hi"})

    assert nested["error"]["code"] == "subagent_not_direct"
    assert outer["id"] in nested["error"]["message"]
    # The refusal names the stop only while the Agent can call background_tasks.
    assert "background_tasks" in nested["error"]["message"]
    assert without_tasks["error"]["code"] == "subagent_not_direct"
    assert "background_tasks" not in without_tasks["error"]["message"]
    assert foreign["error"]["code"] == "subagent_not_found"
    assert outer["id"] in foreign["error"]["message"]
    assert foreign["error"]["message"].count(stranger["id"]) == 1


async def test_background_tasks_lists_and_checks_the_whole_tree(
    harness: SubAgentHarness, commands: CommandHarness
) -> None:
    outer = await harness.spawn("outer")
    await harness.settle()
    outer_session = harness.subagent_session(outer["id"])
    harness.loop.hold("inner")
    inner = await harness.spawn("inner", session=outer_session)
    tests = await _background_command(commands, outer_session, "Run the tests")

    listed = await harness.background_tasks({"action": "list"})
    checked = await harness.background_tasks({"action": "status", "id": outer["id"]})
    deeper = await harness.background_tasks({"action": "status", "id": inner["id"]})

    rows = listed["data"]["tasks"]
    assert [(row["id"], row["type"], row["status"]) for row in rows] == [
        (outer["id"], "subagent", "idle"),
        (inner["id"], "subagent", "working"),
        (tests, "command", "running"),
    ]
    assert "parent_id" not in rows[0]
    assert (rows[1]["parent_id"], rows[1]["title"]) == (outer["id"], "Do inner")
    assert rows[2]["subagent"] == outer["id"]
    # Only a deeper Sub-Agent works, and its answer goes to its own Parent: no
    # message is to come to the caller.
    assert "notes" not in listed["data"]
    assert checked["data"]["running"] == [
        f"background command {tests} (Run the tests)",
        f"Sub-Agent {inner['id']} (Do inner)",
    ]
    assert checked["data"]["next"].startswith("The Sub-Agent is idle. To give it more work")
    assert deeper["data"]["next"].startswith(
        f"The Sub-Agent is working for your Sub-Agent {outer['id']}"
    )


async def test_inspect_reports_the_work_a_subagent_still_runs(
    harness: SubAgentHarness, commands: CommandHarness
) -> None:
    outer = await harness.spawn("outer")
    await harness.settle()
    outer_session = harness.subagent_session(outer["id"])
    harness.loop.hold("inner")
    inner = await harness.spawn("inner", session=outer_session)
    tests = await _background_command(commands, outer_session, "Run the tests")

    projection = await harness.coordinator.inspect(
        outer_session.agent_id, outer_session.session_id, outer["id"]
    )

    assert projection is not None
    assert projection["status"] == "completed"
    assert projection["running"] == [
        {"kind": "command", "id": tests, "label": "Run the tests"},
        {"kind": "subagent", "id": inner["id"], "label": "Do inner"},
    ]


@pytest.mark.parametrize("user_stop", [False, True], ids=["cancel-tool", "stop-all"])
@pytest.mark.parametrize("historical_children", [0, 1_000], ids=["small-tree", "large-tree"])
async def test_cancel_stops_the_whole_tree_before_waiting_for_cleanup(
    harness: SubAgentHarness,
    user_stop: bool,
    historical_children: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.storage.settings = {"max_subagent_depth": 3}
    harness.loop.hold("outer")
    outer = await harness.spawn("outer")
    outer_session = harness.subagent_session(outer["id"])
    harness.loop.hold("inner")
    inner = await harness.spawn("inner", session=outer_session)
    inner_session = harness.subagent_session(inner["id"])
    outer_run, inner_run = harness.manager.active_runs()
    # Keep the active Runs real, but project old children from memory instead
    # of creating a thousand persisted Sessions just to exercise traversal.
    old_sessions = [address("parent", f"old-child-{index}") for index in range(historical_children)]
    old_addresses = set(old_sessions)
    session_children = harness.sessions.subagent_children
    session_metadata = harness.sessions.get_metadata

    def children_with_history(parent: SessionAddress) -> list[SessionAddress]:
        if parent in old_addresses:
            return []
        children = session_children(parent)
        return [*old_sessions, *children] if parent == outer_session else children

    def metadata_with_history(target: SessionAddress) -> dict[str, Any]:
        if target not in old_addresses:
            return session_metadata(target)
        return {
            SUBAGENT_PARENT_META_KEY: {
                "id": f"sub_{target.session_id}",
                "agent_id": outer_session.agent_id,
                "session_id": outer_session.session_id,
                "project_id": outer_session.project_id,
            },
        }

    monkeypatch.setattr(harness.sessions, "subagent_children", children_with_history)
    monkeypatch.setattr(harness.sessions, "get_metadata", metadata_with_history)
    cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        cleanup_entered.set()
        await release_cleanup.wait()

    outer_run.add_cancel_callback(cleanup)
    stopping = asyncio.create_task(
        harness.coordinator.stop_tree(outer_session)
        if user_stop
        else harness.background_tasks({"action": "stop", "id": outer["id"]})
    )
    try:
        await cleanup_entered.wait()
        assert inner_run.cancel_requested
        # An overlapping call cannot create or resume work while the root's
        # cancellation cleanup is still pending.
        spawned = await harness.call(
            {"description": "Do late work", "content": "late work"},
            inner_session,
            cancellation_hook=lambda: inner_run.cancel_requested,
        )
        sent = await harness.call(
            {
                "action": "send",
                "id": inner["id"],
                "content": "late follow-up",
                "model": "replacement/model",
            },
            outer_session,
            cancellation_hook=lambda: outer_run.cancel_requested,
        )
        assert spawned["error"]["code"] == sent["error"]["code"] == "run_cancelled"
        assert (await harness.resolver.session_overrides_async(inner_session)).model is None
        assert [turn.content for turn in harness.loop.turns] == ["outer", "inner"]
    finally:
        release_cleanup.set()
        result = await stopping
    await harness.settle()

    assert [outer_run.status, inner_run.status] == [RunStatus.CANCELLED, RunStatus.CANCELLED]
    assert harness.triggers.to(outer_session) == []
    if user_stop:
        assert result == 2
        [notice] = harness.triggers.to(harness.parent)
        assert "Its turn was cancelled by the user." in notice.body
    else:
        assert isinstance(result, dict)
        assert result["data"]["status"] == "stopped"
        # The Parent learned the outcome from its own Tool result.
        assert harness.triggers.to(harness.parent) == []

    again = await harness.background_tasks({"action": "stop", "id": inner["id"]})
    assert again["data"]["status"] == "idle"


@pytest.mark.parametrize("user_stop", [False, True], ids=["cancel-tool", "stop-all"])
async def test_a_tree_stop_stops_its_background_commands_without_delivery(
    harness: SubAgentHarness, commands: CommandHarness, user_stop: bool
) -> None:
    child = await harness.spawn("child")
    await harness.settle()
    child_session = harness.subagent_session(child["id"])
    own = await _background_command(commands, harness.parent, "Serve")
    childs = await _background_command(commands, child_session, "Run the tests")
    ended = await _background_command(commands, child_session, "Build")
    commands.launcher.processes[-1].exit(0)
    await commands.finish(ended)

    if user_stop:
        # Stop all: the Session's own commands and those of every Sub-Agent below it.
        assert await harness.coordinator.stop_tree(harness.parent) == 2
        stopped = [own, childs]
    else:
        result = await harness.background_tasks({"action": "stop", "id": child["id"]})
        assert result["data"]["status"] == "stopped"
        stopped = [childs]

    for command_id in stopped:
        report = await commands.finish(command_id)
        assert (report.status, report.stop_reason) == ("stopped", "scope")
    if not user_stop:
        assert commands.manager.status(own) == "running"
    # A stopped tree delivers nothing, also not a result that was still due.
    assert [submission["notice_id"] for submission in commands.sink.submissions] == [
        f"command:{ended}"
    ]
    assert commands.sink.cancellations == [f"command:{ended}"]


async def test_stop_all_includes_a_child_whose_admission_is_in_flight(
    harness: SubAgentHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission_entered, release_admission = asyncio.Event(), asyncio.Event()
    stop_waiting = asyncio.Event()
    activities = harness.coordinator._activities  # noqa: SLF001
    ensure_activity = activities.ensure
    admission_lock = harness.coordinator._admission_lock  # noqa: SLF001
    acquire = admission_lock.acquire

    async def held_activity(address):
        admission_entered.set()
        await release_admission.wait()
        return await ensure_activity(address)

    async def observed_acquire():
        if admission_lock.locked():
            stop_waiting.set()
        return await acquire()

    monkeypatch.setattr(activities, "ensure", held_activity)
    monkeypatch.setattr(admission_lock, "acquire", observed_acquire)
    harness.loop.hold("child")
    admission = asyncio.create_task(harness.spawn("child"))
    await admission_entered.wait()
    stopping = asyncio.create_task(harness.coordinator.stop_tree(harness.parent))
    blocked = asyncio.create_task(stop_waiting.wait())
    try:
        # Either stop waits for the current admission, or the broken implementation
        # returns before the new child's Run exists. Neither path needs a timer.
        await asyncio.wait([stopping, blocked], return_when=asyncio.FIRST_COMPLETED)
    finally:
        release_admission.set()
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)
        await admission
        stopped = await stopping

    assert stopped == 1
    assert harness.manager.active_runs() == []
    [(_event_type, started)] = harness.events
    assert harness.manager.get(started["data"]["run_id"]).status is RunStatus.CANCELLED
    assert harness.triggers.to(harness.parent) == []


@pytest.mark.parametrize("action", ["run", "send"])
async def test_a_call_cancelled_once_its_work_began_still_returns_its_result(
    harness: SubAgentHarness,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    harness.loop.hold("review")
    working = await harness.spawn("review")
    record_event = harness._record_event  # noqa: SLF001
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_event(event_type, payload):
        await record_event(event_type, payload)
        entered.set()
        await release.wait()

    # Only the Tool call emits this event; the original Run's forwarding task
    # also opens activity files and could signal an activity-file gate too early.
    monkeypatch.setattr(harness, "_record_event", held_event)
    arguments: dict[str, Any] = (
        {"description": "Do fix", "content": "fix"}
        if action == "run"
        else {"action": "send", "id": working["id"], "content": "fix"}
    )
    call = asyncio.create_task(harness.call(arguments))
    await entered.wait()
    # The calling Run's cancel lands after the work was admitted, before the
    # Tool returns its result, regardless of when background forwarding runs.
    call.cancel()
    release.set()
    result = await call

    # The Parent gets the result, so it never starts the same work again.
    assert result["ok"], result
    assert [turn.content for turn in harness.loop.turns] == ["review", "fix"]
    harness.subagent_session(result["data"]["id"])


async def test_a_subagent_whose_run_cannot_start_leaves_no_session(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_arguments: Any, **_options: Any) -> Run:
        raise RunAdmissionBlockedError("run manager is shutting down")

    monkeypatch.setattr(harness.manager, "start", refuse)

    result = await harness.call({"description": "Do review", "content": "review"})

    assert result["ok"] is False
    assert harness.sessions.subagent_children(harness.parent) == []
    assert harness.sessions.list_addresses(agent_id="parent") == [harness.parent]


@pytest.mark.parametrize(
    ("settings", "nested", "code"),
    [
        ({"max_subagent_depth": 1}, True, "subagent_depth_exceeded"),
        ({"max_active_subagents": 1}, False, "subagent_limit_exceeded"),
        # The working Sub-Agent counts for its Parent's limit, not for its own.
        ({"max_active_subagents": 1}, True, None),
        ({"max_active_subagents_total": 1}, True, "subagent_app_limit_exceeded"),
    ],
    ids=["depth", "active-per-session", "per-session-ignores-the-parents", "active-app-wide"],
)
async def test_limits_refuse_another_subagent(
    harness: SubAgentHarness, settings: dict[str, int], nested: bool, code: str | None
) -> None:
    harness.storage.settings = settings
    harness.loop.hold("first")
    first = await harness.spawn("first")
    caller = harness.subagent_session(first["id"]) if nested else harness.parent

    result = await harness.call({"description": "Do second", "content": "second"}, caller)

    if code is None:
        assert result["ok"] is True
        assert [turn.content for turn in harness.loop.turns] == ["first", "second"]
        return
    assert result["error"]["code"] == code
    assert "{" not in result["error"]["message"]
    assert [turn.content for turn in harness.loop.turns] == ["first"]
    if code == "subagent_limit_exceeded":
        # The refusal names the stop only while the Agent can call background_tasks.
        without_tasks = await harness.call(
            {"description": "Do second", "content": "second"}, caller, allowed_tools=["subagent"]
        )
        assert "background_tasks" in result["error"]["message"]
        assert without_tasks["error"]["code"] == code
        assert "background_tasks" not in without_tasks["error"]["message"]


async def test_send_counts_against_the_limit_only_when_it_starts_an_idle_subagent(
    harness: SubAgentHarness,
) -> None:
    harness.storage.settings = {"max_active_subagents": 1}
    idle = await harness.spawn("idle")
    await harness.settle()
    gate = harness.loop.hold("busy")
    busy = await harness.spawn("busy")

    refused = await harness.call(
        {"action": "send", "id": idle["id"], "content": "more", "model": "acme/other"}
    )
    steered = await harness.call({"action": "send", "id": busy["id"], "content": "also"})

    assert refused["error"]["code"] == "subagent_limit_exceeded"
    stored = await harness.resolver.session_overrides_async(harness.subagent_session(idle["id"]))
    assert stored.model != "acme/other"
    assert steered["data"]["status"] == "steered"
    gate.set()
    await harness.settle()
    started = await harness.call({"action": "send", "id": idle["id"], "content": "more"})
    assert started["data"]["status"] == "started"


async def test_parallel_starts_respect_the_active_limit_with_a_stale_count(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.storage.settings = {"max_active_subagents": 1}
    harness.loop.hold("first")
    harness.loop.hold("second")
    first_started = asyncio.Event()
    tree_reads = 0
    run_async = harness.sessions.run_async

    async def stale_second_tree_read(function: Any, *arguments: Any, **options: Any) -> Any:
        # The second start counts, then resumes only after the first start finished.
        nonlocal tree_reads
        result = await run_async(function, *arguments, **options)
        if function is subagents_module.children:
            tree_reads += 1
            if tree_reads == 2:
                await first_started.wait()
        return result

    monkeypatch.setattr(harness.sessions, "run_async", stale_second_tree_read)
    first = asyncio.create_task(harness.call({"description": "Do first", "content": "first"}))
    second = asyncio.create_task(harness.call({"description": "Do second", "content": "second"}))
    await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
    first_started.set()
    results = [await first, await second]

    assert sorted(bool(result["ok"]) for result in results) == [False, True]
    [refused] = [result for result in results if not result["ok"]]
    assert refused["error"]["code"] == "subagent_limit_exceeded"
    assert len(harness.loop.turns) == 1


def _take_over(harness: SubAgentHarness, session: SessionAddress) -> None:
    """Mark *session* taken over, as the user's first message in it does."""
    harness.sessions.mutate_metadata(
        session,
        lambda metadata: metadata.__setitem__(
            SUBAGENT_TAKEN_OVER_AT_META_KEY, "2026-10-07T12:00:00+00:00"
        ),
    )
    harness.coordinator.subagent_taken_over(session)


async def test_takeover_hands_the_subagent_and_its_work_to_the_user(
    harness: SubAgentHarness, commands: CommandHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.loop.hold("review")
    data = await harness.spawn("review")
    child = harness.subagent_session(data["id"])
    deep = await harness.spawn("deep", child)
    serve = await _background_command(commands, child, "Serve")
    tests = await _background_command(commands, harness.subagent_session(deep["id"]), "Test")
    # The tree as a call read it just before the user took over.
    stale = await harness.coordinator.subagents(harness.parent)

    _take_over(harness, child)
    # The takeover notice is independent of the Run's answer forwarding.
    await harness.triggers.wait_for_notice(f"subagent-takeover:{data['id']}")
    await harness.finished("review")

    [notice] = harness.triggers.to(harness.parent)
    assert notice.wake is False
    assert "The user took over this Sub-Agent" in notice.body
    refused = await harness.call({"action": "send", "id": data["id"], "content": "more"})
    assert refused["error"]["code"] == "subagent_taken_over"
    gone = await harness.message_parent("question", child)
    assert gone["error"]["code"] == "subagent_taken_over"

    # Another Sub-Agent of the Parent, whose own Sub-Agent the user took over and works in.
    outer = await harness.spawn("outer")
    outer_session = harness.subagent_session(outer["id"])
    inner = await harness.spawn("inner", outer_session)
    inner_session = harness.subagent_session(inner["id"])
    await harness.settle()
    _take_over(harness, inner_session)
    harness.loop.hold("user work")
    user_run = await harness.start_turn(inner_session, "user work", kind=RunKind.USER)
    build = await _background_command(commands, outer_session, "Build")
    watch = await _background_command(commands, inner_session, "Watch")

    # The Parent keeps the taken-over Sub-Agents' rows; what their Sessions run, and the
    # Sub-Agents below them, work for the user.
    listed = await harness.background_tasks({"action": "list"})
    assert [(row["id"], row["status"]) for row in listed["data"]["tasks"]] == [
        (data["id"], "taken over by the user"),
        (outer["id"], "idle"),
        (inner["id"], "taken over by the user"),
        (build, "running"),
    ]
    shown = await harness.background_tasks({"action": "status", "id": data["id"]})
    assert "running" not in shown["data"]
    # Each refusal names the task and the Sub-Agents up to the one the user took over, and
    # offers no call: what happens to the task is the user's to decide. No Agent above
    # stops a Sub-Agent the user took over.
    for task_id, action, named in [
        (serve, "stop", (serve, data["id"])),
        (tests, "status", (tests, deep["id"], data["id"])),
        (deep["session_id"], "stop", (deep["id"], data["id"])),
        (data["id"], "stop", (data["id"],)),
        (inner["id"], "stop", (inner["id"],)),
        (watch, "stop", (watch, inner["id"])),
    ]:
        refusal = await harness.background_tasks({"action": action, "id": task_id})
        assert refusal["error"]["code"] == "subagent_taken_over"
        message = refusal["error"]["message"]
        assert all(name in message for name in named), message
        assert "{" not in message
    # A stop that read the tree before the takeover is refused all the same.
    stale_reads: list[list[SubAgentState]] = []
    current_tree = harness.coordinator.subagents

    async def stale_once(caller: SessionAddress) -> list[SubAgentState]:
        return stale_reads.pop() if stale_reads else await current_tree(caller)

    monkeypatch.setattr(harness.coordinator, "subagents", stale_once)
    for raced, named in [(data["id"], (data["id"],)), (deep["id"], (deep["id"], data["id"]))]:
        stale_reads.append(stale)
        refusal = await harness.background_tasks({"action": "stop", "id": raced})
        assert refusal["error"]["code"] == "subagent_taken_over"
        assert all(name in refusal["error"]["message"] for name in named)
    assert [commands.manager.status(task) for task in (serve, tests, watch)] == ["running"] * 3

    # Stopping a Sub-Agent above a takeover stops its own work and ends there.
    stopped = await harness.background_tasks({"action": "stop", "id": outer["id"]})
    assert stopped["data"]["status"] == "stopped"
    assert inner["id"] in stopped["data"]["notes"][-1]
    assert (await commands.finish(build)).stop_reason == "scope"
    # Nor does the Parent's Stop all reach the user's Sub-Agents.
    await harness.coordinator.stop_tree(harness.parent)
    assert [commands.manager.status(task) for task in (serve, tests, watch)] == ["running"] * 3
    assert user_run.cancel_requested is False

    # The user's Stop all in the Session the user took over stops everything below it.
    await harness.coordinator.stop_tree(child)
    for command_id in (serve, tests):
        assert (await commands.finish(command_id)).stop_reason == "scope"


async def test_message_parent_reaches_the_parent_while_the_subagent_works(
    harness: SubAgentHarness,
) -> None:
    harness.loop.hold("review")
    data = await harness.spawn("review")
    child = harness.subagent_session(data["id"])

    result = await harness.message_parent("Which branch?", child)

    assert result["ok"], result
    [notice] = harness.triggers.to(harness.parent)
    assert notice.wake is True
    assert f"### Message from Sub-Agent {data['id']} — Do review" in notice.body
    assert "Which branch?" in notice.body

    outside = await harness.message_parent("hello", address("parent", harness.parent.session_id))
    assert outside["error"]["message"] == MESSAGE_PARENT_NOT_SUBAGENT_MESSAGE


async def test_activity_file_follows_every_turn_of_a_subagent(
    harness: SubAgentHarness,
) -> None:
    data = await harness.spawn("review")
    await harness.settle()
    await harness.call({"action": "send", "id": data["id"], "content": "again"})
    await harness.settle()

    listed = await harness.background_tasks({"action": "list"})
    path = listed["data"]["tasks"][0]["activity_file"]
    assert path in data["activity_note"]
    files = [file for file in harness.storage.temporary_files.root.rglob("*") if file.is_file()]
    assert len(files) == 1
