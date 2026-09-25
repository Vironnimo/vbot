"""Real dispatch executes clear ``subagent`` intent written in other shapes.

Other harness dialects, action synonyms, placeholder fields and copied result
fields reach exactly the Agent, Session and task the call meant. Each repair is
paired with a nearby call that means something else and is refused or routed
differently.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

import core.subagents._constants as subagent_constants
from core.tools.contracts import ToolContractError

from .subagent_test_support import (
    FakeAgentResolver,
    FakeAgents,
    dispatch_harness,
)

pytestmark = pytest.mark.asyncio

BRIEF = "Review src/a.py for unused imports and report file:line findings. Do not edit."
RUNNING_NOTE = subagent_constants.TOP_LEVEL_BACKGROUND_NOTE


@pytest_asyncio.fixture
async def harness(tmp_path):
    async with dispatch_harness(tmp_path) as value:
        yield value


async def dispatch(harness, arguments, context=None):
    return await harness.registry.dispatch(context or harness.context, arguments)


async def delegated_task(harness, index: int = 0) -> str:
    """Return the task text the child Run of ``index`` received."""
    _, _, executor, run = harness.manager.started[index]
    received = await executor(run)
    return str(received.content).removeprefix("handled: ")


@pytest.mark.parametrize(
    ("arguments", "target", "title"),
    [
        # Claude Code / opencode Task
        (
            {"description": "Review imports", "prompt": BRIEF, "subagent_type": "worker"},
            "worker",
            "Review imports",
        ),
        (
            {
                "description": "Review imports",
                "prompt": BRIEF,
                "subagent_type": "worker",
                "background": True,
            },
            "worker",
            "Review imports",
        ),
        ({"prompt": BRIEF, "run_in_background": True}, "parent", None),
        # pi, OpenClaw, Hermes
        ({"agent": "worker", "task": BRIEF}, "worker", None),
        (
            {"task": BRIEF, "label": "Review imports", "agentId": "worker"},
            "worker",
            "Review imports",
        ),
        ({"goal": BRIEF}, "parent", None),
        ({"instructions": BRIEF, "title": "Review imports"}, "parent", "Review imports"),
        # Action synonyms and vBot's earlier shapes
        ({"action": "spawn", "message": BRIEF}, "parent", None),
        ({"action": "Delegate", "content": BRIEF, "target": "worker"}, "worker", None),
        ({"request": BRIEF}, "parent", None),
        ({"request": {"content": BRIEF, "agent_id": "worker", "background": True}}, "worker", None),
    ],
)
async def test_other_harness_dialects_reach_the_exact_agent_and_task(
    harness, arguments, target, title
):
    result = await dispatch(harness, arguments)

    assert result["ok"], result
    assert harness.manager.started[0][0] == target == result["data"]["agent_id"]
    assert await delegated_task(harness) == BRIEF
    assert result["data"]["note"] == RUNNING_NOTE
    summaries = harness.runtime.chat_sessions.list_summaries(target)
    assert summaries[0]["auto_title"] == (title or BRIEF[:48])


async def test_hermes_context_travels_with_its_goal(harness):
    result = await dispatch(
        harness, {"goal": "Fix the failing test.", "context": "The repository uses Python 3.12."}
    )
    assert result["ok"], result
    assert await delegated_task(harness) == (
        "Fix the failing test.\n\nContext:\nThe repository uses Python 3.12."
    )


async def test_context_alone_is_the_task(harness):
    result = await dispatch(harness, {"context": BRIEF})
    assert result["ok"], result
    assert await delegated_task(harness) == BRIEF


@pytest.mark.parametrize(
    ("arguments", "target"),
    [
        (
            {
                "action": "run",
                "agent_id": "worker",
                "content": BRIEF,
                "description": "Audit",
                "id": "unused",
                "model": "",
                "session_id": "invalid-placeholder",
                "thinking_effort": "",
            },
            "worker",
        ),
        (
            {"action": "run", "agent_id": "worker", "content": BRIEF, "id": " ", "session_id": " "},
            "worker",
        ),
        (
            {"action": "run", "content": BRIEF, "id": ".", "session_id": ".", "agent_id": "."},
            "parent",
        ),
        (
            {
                "content": BRIEF,
                "id": "placeholder",
                "session_id": "__omit__",
                "model": "default",
                "thinking_effort": "inherit",
            },
            "parent",
        ),
        ({"content": BRIEF, "agent_id": "", "action": ""}, "parent"),
        ({"content": BRIEF, "agent_id": None, "action": None}, "parent"),
        ({"content": BRIEF, "agent_id": "worker", "session_id": "new"}, "worker"),
    ],
)
async def test_placeholder_fields_count_as_omitted(harness, arguments, target):
    result = await dispatch(harness, arguments)

    assert result["ok"], result
    assert harness.manager.started[0][0] == target
    assert await delegated_task(harness) == BRIEF
    # A new Session, the Agent's own model and effort, and no notes about the placeholders.
    assert len(harness.runtime.chat_sessions.list(target)) == 1
    assert harness.runtime.streaming_chat_loop.seen_agent_overrides[-1] is None
    assert result["data"]["note"] == RUNNING_NOTE


@pytest.mark.parametrize("name", ["general-purpose", "General Purpose", "default", "self"])
async def test_generic_worker_name_delegates_to_a_copy_of_yourself(harness, name):
    result = await dispatch(harness, {"prompt": BRIEF, "subagent_type": name})

    assert result["ok"], result
    assert harness.manager.started[0][0] == "parent"
    assert result["data"]["note"] == (
        f'agent_id "{name}" is not an Agent id, so a copy of you runs this task, as when '
        f"agent_id is omitted. {RUNNING_NOTE}"
    )


async def test_generic_name_selects_an_agent_with_exactly_that_id(harness):
    agents = FakeAgents({"parent", "worker", "default"})
    harness.runtime.agents = agents
    harness.runtime.agent_resolver = FakeAgentResolver(agents)

    result = await dispatch(harness, {"prompt": BRIEF, "subagent_type": "default"})

    assert result["ok"], result
    assert harness.manager.started[0][0] == "default"
    assert result["data"]["note"] == RUNNING_NOTE


async def test_specific_unknown_agent_is_refused_with_the_choices(harness):
    result = await dispatch(harness, {"prompt": BRIEF, "subagent_type": "Explore"})

    assert result["error"] == {
        "code": "agent_not_found",
        "message": (
            "Agent not found: Explore. Omit agent_id to delegate to a copy of yourself, or use "
            "one of these Agent ids exactly: worker."
        ),
    }
    assert harness.manager.started == []


async def test_disallowed_agent_is_refused_with_the_choices(harness):
    context = replace(harness.context, tool_settings={"subagent": {"allowed_agents": []}})

    refused = await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}, context)
    generic = await dispatch(harness, {"content": BRIEF, "agent_id": "general-purpose"}, context)

    assert refused["error"] == {
        "code": "agent_not_allowed",
        "message": (
            "Agent worker is not available to you as a Sub-Agent. Omit agent_id to delegate "
            "to a copy of yourself; no other Agents are available to you."
        ),
    }
    assert generic["ok"], generic
    assert [started[0] for started in harness.manager.started] == ["parent"]


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
async def test_top_level_delegation_runs_in_background_and_says_so_when_asked_to_wait(
    harness, arguments, asked_to_wait
):
    result = await dispatch(harness, {"content": BRIEF, **arguments})

    assert result["ok"], result
    assert result["data"]["status"] == "running"
    assert result["data"]["delivery"] == "automatic"
    expected = (
        f"{subagent_constants.SUBAGENT_BACKGROUND_UNAVAILABLE_NOTE} {RUNNING_NOTE}"
        if asked_to_wait
        else RUNNING_NOTE
    )
    assert result["data"]["note"] == expected


async def test_run_ignores_a_label_id_and_names_the_assigned_id(harness):
    result = await dispatch(harness, {"action": "run", "content": BRIEF, "id": "audit-execution"})

    assert result["ok"], result
    assert result["data"]["id"].startswith("sub_")
    assert result["data"]["note"] == (
        'id "audit-execution" was ignored: run assigns the work id above; use that id with '
        f"status or cancel. {RUNNING_NOTE}"
    )


async def test_run_with_tracked_work_id_asks_whether_to_continue(harness):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]

    result = await dispatch(harness, {"content": "Also check tests.", "id": child["id"]})

    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"] == (
        f'subagent was not run: "id" names Sub-Agent work {child["id"]}, so it is unclear '
        "whether to continue that Sub-Agent or to start new work. To continue its Session, "
        f'call {{"agent_id": "worker", "session_id": "{child["session_id"]}", "content": '
        '"<follow-up>"}. To start new work, repeat this call without "id".'
    )
    assert len(harness.manager.started) == 1


async def test_run_with_untracked_work_id_asks_whether_to_continue(harness):
    result = await dispatch(harness, {"content": BRIEF, "id": "sub_abcdefghijkl"})

    assert result["error"]["message"] == (
        'subagent was not run: "id" names Sub-Agent work sub_abcdefghijkl, so it is unclear '
        "whether to continue that Sub-Agent or to start new work. To continue that "
        "Sub-Agent's Session, pass the agent_id and session_id from its result instead of "
        '"id". To start new work, repeat this call without "id".'
    )
    assert harness.manager.started == []


async def test_copied_work_id_with_its_session_continues_that_session(harness):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]
    harness.manager.started[0][3].mark_completed({})

    result = await dispatch(
        harness,
        {
            "action": "run",
            "content": "Go on.",
            "id": child["id"],
            "agent_id": "worker",
            "session_id": child["session_id"],
        },
    )

    assert result["ok"], result
    assert result["data"]["session_id"] == child["session_id"]
    assert harness.manager.started[1][:2] == ("worker", child["session_id"])
    assert await delegated_task(harness, 1) == "Go on."
    assert result["data"]["note"] == RUNNING_NOTE


async def test_work_id_of_another_session_conflicts_with_session_id(harness):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]
    other = harness.runtime.chat_sessions.create("worker")

    result = await dispatch(
        harness,
        {"content": "Go on.", "id": child["id"], "agent_id": "worker", "session_id": other.id},
    )

    assert result["error"]["message"] == (
        f"subagent was not run: work {child['id']} belongs to Session {child['session_id']}, "
        f"but session_id is {other.id}. To continue work {child['id']}, call "
        f'{{"agent_id": "worker", "session_id": "{child["session_id"]}", "content": '
        f'"<follow-up>"}}; to continue Session {other.id}, repeat this call without "id".'
    )
    assert len(harness.manager.started) == 1


@pytest.mark.parametrize("session_id", ["new-review", "audit-do-not-use-placeholder", "INVALID_X"])
async def test_stand_in_session_id_that_names_no_session_starts_a_new_one(harness, session_id):
    result = await dispatch(
        harness, {"content": BRIEF, "agent_id": "worker", "session_id": session_id}
    )

    assert result["ok"], result
    assert result["data"]["session_id"] != session_id
    assert harness.manager.started[0][:2] == ("worker", result["data"]["session_id"])
    assert result["data"]["note"] == (
        f'No Session "{session_id}" exists and that value reads as a stand-in, so this task '
        f"started a new Session. Continue it with the session_id of this result. {RUNNING_NOTE}"
    )


async def test_existing_session_is_continued_whatever_its_id_says(harness):
    harness.runtime.chat_sessions.create("worker", session_id="new-review")

    result = await dispatch(
        harness, {"content": BRIEF, "agent_id": "worker", "session_id": "new-review"}
    )

    assert result["ok"], result
    assert harness.manager.started[0][:2] == ("worker", "new-review")
    assert result["data"]["note"] == RUNNING_NOTE


async def test_label_session_id_that_names_no_session_is_refused(harness):
    result = await dispatch(
        harness, {"content": BRIEF, "agent_id": "worker", "session_id": "audit-storage"}
    )

    assert result["error"]["code"] == "session_not_found"
    assert result["error"]["message"].startswith(
        "No Session audit-storage exists for Agent worker; nothing was started."
    )
    assert result["error"]["message"].endswith('repeat this call without "session_id".')
    assert harness.manager.started == []
    assert harness.runtime.chat_sessions.list("worker") == []


async def test_continue_action_with_its_session_continues_it(harness):
    # vBot's earlier contract spelled a continuation as operation "continue".
    session = harness.runtime.chat_sessions.create("worker")

    result = await dispatch(
        harness,
        {
            "request": {
                "operation": "continue",
                "agent_id": "worker",
                "session_id": session.id,
                "content": "Go on.",
                "background": True,
            }
        },
    )

    assert result["ok"], result
    assert harness.manager.started[0][:2] == ("worker", session.id)
    assert await delegated_task(harness) == "Go on."


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "resume", "content": "Go on.", "agent_id": "worker"},
        {"action": "continue", "content": "Go on.", "session_id": "new"},
    ],
)
async def test_continue_action_without_a_session_is_refused(harness, arguments):
    with pytest.raises(ToolContractError) as raised:
        await dispatch(harness, arguments)

    assert str(raised.value) == (
        "subagent was not run: continuing a Sub-Agent needs its Session. Send the agent_id and "
        'session_id from that Sub-Agent\'s result with the follow-up as "content", or omit '
        '"action" to start new work.'
    )
    assert harness.manager.started == []


async def test_continue_action_with_a_work_id_names_its_session(harness):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]

    result = await dispatch(harness, {"action": "continue", "content": "Go on.", "id": child["id"]})

    assert result["error"]["message"].startswith(
        f'subagent was not run: "id" names Sub-Agent work {child["id"]}'
    )
    assert f'"session_id": "{child["session_id"]}"' in result["error"]["message"]
    assert len(harness.manager.started) == 1


async def test_session_id_alone_continues_tracked_child_of_another_agent(harness):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]
    harness.manager.started[0][3].mark_completed({})

    result = await dispatch(harness, {"content": "Go on.", "session_id": child["session_id"]})

    assert result["ok"], result
    assert result["data"]["agent_id"] == "worker"
    assert harness.manager.started[1][:2] == ("worker", child["session_id"])


async def test_work_id_without_action_names_both_calls(harness):
    result = await dispatch(harness, {"id": "sub_abcdefghijkl"})

    assert result["error"]["message"] == (
        "subagent was not run: it received work id sub_abcdefghijkl but no action. To inspect "
        'that work, call {"action": "status", "id": "sub_abcdefghijkl"}; to stop it, call '
        '{"action": "cancel", "id": "sub_abcdefghijkl"}. To delegate new work, send the task '
        'as "content" without "id".'
    )


@pytest.mark.parametrize("arguments", [{}, {"description": "Review"}, {"content": "  "}])
async def test_missing_task_names_the_call(harness, arguments):
    result = await dispatch(harness, arguments)
    assert result["error"]["message"] == subagent_constants.SUBAGENT_MISSING_TASK_MESSAGE
    assert harness.manager.started == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"goal": BRIEF, "toolsets": ["terminal", "web"]},
            "subagent was not run: a Sub-Agent always works with its Agent's own Tools, so "
            '"toolsets" cannot be chosen per call. Repeat this call without "toolsets", and '
            "state any Tool restriction in content.",
        ),
        (
            {"tasks": [{"goal": BRIEF}, {"goal": "Second task."}]},
            'subagent was not run: it takes one task per call. Send each item of "tasks" as its '
            'own subagent call, with that task as "content", all in the same turn so they run '
            "concurrently.",
        ),
    ],
)
async def test_requests_vbot_cannot_honor_are_refused_with_the_correction(
    harness, arguments, message
):
    with pytest.raises(ToolContractError) as raised:
        await dispatch(harness, arguments)
    assert str(raised.value) == message
    assert harness.manager.started == []


async def test_empty_toolsets_request_nothing(harness):
    result = await dispatch(harness, {"goal": BRIEF, "toolsets": []})
    assert result["ok"], result


async def test_status_ignores_fields_it_does_not_use(harness):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]

    single = await dispatch(
        harness,
        {
            "action": "status",
            "agent_id": "worker",
            "content": "status",
            "description": "Audit progress",
            "id": child["id"],
            "model": "openai/x",
            "session_id": child["session_id"],
            "thinking_effort": "high",
        },
    )
    listing = await dispatch(harness, {"action": "list", "id": "", "agent_id": "worker"})

    assert single["ok"], single
    assert (single["data"]["id"], single["data"]["status"]) == (child["id"], "running")
    assert single["data"]["note"] == subagent_constants.SUBAGENT_STATUS_RUNNING_NOTE
    assert [entry["id"] for entry in listing["data"]["subagents"]] == [child["id"]]
    assert listing["data"]["note"] == subagent_constants.SUBAGENT_STATUS_LIST_NOTE
    assert len(harness.manager.started) == 1


@pytest.mark.parametrize("action", ["cancel", "stop", "Kill"])
async def test_cancel_ignores_echoed_fields_and_stops_the_exact_work(harness, action):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]

    result = await dispatch(
        harness,
        {
            "action": action,
            "id": child["id"],
            "agent_id": "worker",
            "content": "stop the audit",
            "session_id": child["session_id"],
            "thinking_effort": "high",
        },
    )

    assert result["ok"], result
    assert result["data"]["status"] == "cancelled"
    assert harness.manager.started[0][3].status.value == "cancelled"


async def test_unknown_id_lists_the_tracked_work(harness):
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]

    result = await dispatch(harness, {"action": "status", "id": "last"})

    assert result["error"] == {
        "code": "subagent_not_found",
        "message": (
            "No Sub-Agent work with id last is tracked for this Session; work stops being "
            "tracked once its result was delivered to you, or when vBot restarts. Tracked "
            f"work: {child['id']} (agent_id worker, session_id {child['session_id']}, running)."
        ),
    }


async def test_unknown_id_without_tracked_work_says_so(harness):
    status = await dispatch(harness, {"action": "status", "id": "sub_aaaaaaaaaaaa"})
    cancel = await dispatch(harness, {"action": "cancel", "id": "sub_aaaaaaaaaaaa"})

    assert status["error"]["message"] == (
        "No Sub-Agent work is tracked for this Session, so id sub_aaaaaaaaaaaa was not found. "
        "Work stops being tracked once its result was delivered to you, or when vBot "
        "restarts; use the delivered result."
    )
    assert cancel["error"]["message"] == (
        "No Sub-Agent work is tracked for this Session, so id sub_aaaaaaaaaaaa was not found "
        "and nothing was cancelled. Work stops being tracked once it finished and its result "
        "was delivered to you, or when vBot restarts."
    )


async def test_cancel_without_id_names_the_exact_call(harness):
    nothing = await dispatch(harness, {"action": "cancel", "id": "  "})
    child = (await dispatch(harness, {"content": BRIEF, "agent_id": "worker"}))["data"]
    tracked = await dispatch(harness, {"action": "cancel"})

    assert nothing["error"]["message"] == (
        subagent_constants.SUBAGENT_CANCEL_NOTHING_TRACKED_MESSAGE
    )
    assert tracked["error"]["message"] == (
        'cancel needs "id", the work to stop; nothing was cancelled. Tracked work: '
        f"{child['id']} (agent_id worker, session_id {child['session_id']}, running). Call "
        f'{{"action": "cancel", "id": "{child["id"]}"}}.'
    )
    assert harness.manager.started[0][3].status.value == "running"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"description": "Review imports", "prompt": BRIEF, "subagent_type": "worker"},
            ["Review imports", "worker"],
        ),
        ({"action": "spawn", "task": "Check links"}, ["Check links"]),
        ({"action": "stop", "work_id": "sub_abcdefghijkl"}, ["cancel", "sub_abcdefghijkl"]),
        ({"action": "status", "id": "unused", "agent_id": "worker"}, ["status", "worker"]),
        ({"goal": "Check links", "title": "Links", "agent": "worker"}, ["Links", "worker"]),
    ],
)
async def test_display_labels_what_the_call_meant(harness, arguments, expected):
    display = harness.registry.get("subagent").display.to_payload(arguments)
    assert [part["value"] for part in display["primary"]] == expected
