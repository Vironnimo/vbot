"""Sub-Agent behavior at the ``subagent`` and ``message_parent`` Tools.

A Sub-Agent is a Session linked to its Parent Session. ``run`` starts one in the
background; vBot forwards the final answer of every turn the Parent or a
delivery started there; ``send`` reaches it at its next step or starts a turn;
``list`` and ``cancel`` cover the caller's whole tree; the user's first message
in the Session ends forwarding.
"""

from __future__ import annotations

import pytest

from core.projects import AgentResolutionError, ResolutionProjectNotFoundError
from core.runs import RunKind, RunStatus
from core.sessions import SUBAGENT_PARENT_META_KEY
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

pytestmark = pytest.mark.asyncio


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
        # The built-in Librarian resolves as an Agent but is no delegation target.
        (None, None, "librarian", "agent_not_found"),
    ],
    ids=["other-scope", "self-only", "other-project", "unknown", "librarian"],
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


async def test_answer_names_working_subagents_of_the_subagent(harness: SubAgentHarness) -> None:
    outer = await harness.spawn("outer")
    await harness.settle()
    outer_session = harness.subagent_session(outer["id"])
    harness.loop.hold("inner")
    inner = await harness.spawn("inner", session=outer_session)

    await harness.start_turn(outer_session, "status please")
    await harness.until(lambda: len(harness.triggers.to(harness.parent)) == 2)

    body = harness.triggers.to(harness.parent)[1].body
    assert f"Still running for this Sub-Agent: Sub-Agent {inner['id']} (Do inner)." in body


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
    foreign = await harness.call({"action": "send", "id": stranger["id"], "content": "hi"})

    assert nested["error"]["code"] == "subagent_not_direct"
    assert outer["id"] in nested["error"]["message"]
    assert foreign["error"]["code"] == "subagent_not_found"
    assert outer["id"] in foreign["error"]["message"]
    assert foreign["error"]["message"].count(stranger["id"]) == 1


async def test_list_shows_the_whole_tree(harness: SubAgentHarness) -> None:
    outer = await harness.spawn("outer")
    await harness.settle()
    harness.loop.hold("inner")
    inner = await harness.spawn("inner", session=harness.subagent_session(outer["id"]))

    result = await harness.call({"action": "status"})

    entries = {entry["id"]: entry for entry in result["data"]["subagents"]}
    assert entries.keys() == {outer["id"], inner["id"]}
    assert entries[outer["id"]]["state"] == "idle"
    assert "parent_id" not in entries[outer["id"]]
    assert entries[inner["id"]]["state"] == "working"
    assert entries[inner["id"]]["parent_id"] == outer["id"]
    assert entries[inner["id"]]["title"] == "Do inner"
    assert "answer" not in entries[outer["id"]]
    assert "note" in result["data"]


async def test_cancel_stops_a_subagent_and_everything_below_it(
    harness: SubAgentHarness,
) -> None:
    harness.loop.hold("outer")
    outer = await harness.spawn("outer")
    harness.loop.hold("inner")
    inner = await harness.spawn("inner", session=harness.subagent_session(outer["id"]))
    runs = harness.manager.active_runs()

    result = await harness.call({"action": "cancel", "id": outer["id"]})
    await harness.settle()

    assert result["data"]["status"] == "cancelled"
    assert [run.status for run in runs] == [RunStatus.CANCELLED, RunStatus.CANCELLED]
    # The Parent learned the outcome from its own Tool result.
    assert harness.triggers.to(harness.parent) == []
    assert harness.triggers.to(harness.subagent_session(outer["id"])) == []

    again = await harness.call({"action": "cancel", "id": inner["id"]})
    assert again["error"]["code"] == "subagent_not_running"


async def test_user_stop_all_stops_the_tree_without_waking_anyone(
    harness: SubAgentHarness,
) -> None:
    harness.loop.hold("outer")
    outer = await harness.spawn("outer")
    outer_session = harness.subagent_session(outer["id"])
    harness.loop.hold("inner")
    await harness.spawn("inner", session=outer_session)
    runs = harness.manager.active_runs()

    stopped = await harness.coordinator.stop_tree(outer_session)
    await harness.settle()

    assert stopped == 2
    assert [run.status for run in runs] == [RunStatus.CANCELLED, RunStatus.CANCELLED]
    assert harness.triggers.to(outer_session) == []
    [notice] = harness.triggers.to(harness.parent)
    assert "Its turn was cancelled by the user." in notice.body


@pytest.mark.parametrize(
    ("settings", "depth", "code"),
    [
        ({"max_subagent_depth": 1}, 1, "subagent_depth_exceeded"),
        ({"max_active_subagents": 1}, 0, "subagent_limit_exceeded"),
    ],
    ids=["depth", "active-per-tree"],
)
async def test_limits_refuse_another_subagent(
    harness: SubAgentHarness, settings: dict[str, int], depth: int, code: str
) -> None:
    harness.storage.settings = settings
    harness.loop.hold("first")
    first = await harness.spawn("first")
    caller = harness.subagent_session(first["id"]) if depth else harness.parent

    result = await harness.call({"description": "Do second", "content": "second"}, caller)

    assert result["error"]["code"] == code
    assert [turn.content for turn in harness.loop.turns] == ["first"]


async def test_takeover_ends_forwarding_and_parent_messages(harness: SubAgentHarness) -> None:
    harness.loop.hold("review")
    data = await harness.spawn("review")
    child = harness.subagent_session(data["id"])

    assert harness.sessions.mark_subagent_taken_over(child) is True
    assert harness.sessions.mark_subagent_taken_over(child) is False
    harness.coordinator.subagent_taken_over(child)
    await harness.finished("review")

    [notice] = harness.triggers.to(harness.parent)
    assert notice.wake is False
    assert "The user took over this Sub-Agent" in notice.body
    refused = await harness.call({"action": "send", "id": data["id"], "content": "more"})
    assert refused["error"]["code"] == "subagent_taken_over"
    listed = await harness.call({"action": "list"})
    assert listed["data"]["subagents"][0]["state"] == "taken over by the user"
    gone = await harness.message_parent("question", child)
    assert gone["error"]["code"] == "subagent_taken_over"


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

    listed = await harness.call({"action": "list"})
    path = listed["data"]["subagents"][0]["activity_file"]
    assert path in data["activity_note"]
    files = [file for file in harness.storage.temporary_files.root.rglob("*") if file.is_file()]
    assert len(files) == 1
