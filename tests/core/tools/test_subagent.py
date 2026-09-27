"""The ``subagent`` Tool: its definition, the call shapes it accepts, and its display.

Other harness dialects, action synonyms, placeholder fields and copied result
fields reach exactly the Agent, Session and task the call meant. Calls whose
meaning is unclear, or that ask for something vBot cannot do, are refused
before any work starts, with the corrected call.
"""

from __future__ import annotations

import copy

import pytest

from core.subagents._constants import (
    SUBAGENT_CANCEL_NOTHING_TRACKED_MESSAGE,
    SUBAGENT_MISSING_TASK_MESSAGE,
    SUBAGENT_STATUS_LIST_NOTE,
    SUBAGENT_STATUS_RUNNING_NOTE,
    TOP_LEVEL_BACKGROUND_NOTE,
)
from core.tools.subagent import (
    SUBAGENT_TOOL_DESCRIPTION,
    SUBAGENT_TOOL_NAME,
    SUBAGENT_TOOL_PARAMETERS,
)
from tests.core.subagents.subagents_test_support import (
    JsonObject,
    SubAgentHarness,
)
from tests.core.subagents.subagents_test_support import (
    harness as harness,
)

pytestmark = pytest.mark.asyncio

BRIEF = 'Read-only review of src/a.py. Preserve literal {"action":"CANCEL"}.\nReference R-17.'
DEFAULT_TITLE = " ".join(BRIEF.split())[:48]
PARAMETER_LIST = (
    "subagent parameters: action, content, description, agent_id, session_id, model, "
    "thinking_effort, id."
)


async def test_tool_definition_is_one_flat_open_contract(harness: SubAgentHarness) -> None:
    assert [tool.name for tool in harness.registry.list_tools()] == [SUBAGENT_TOOL_NAME]
    subagent = harness.registry.get(SUBAGENT_TOOL_NAME)
    assert subagent.description == SUBAGENT_TOOL_DESCRIPTION
    assert subagent.parameters == SUBAGENT_TOOL_PARAMETERS
    assert "oneOf" not in subagent.parameters
    assert "additionalProperties" not in subagent.parameters
    assert subagent.parameters["required"] == []
    properties = subagent.parameters["properties"]
    assert list(properties) == [
        "action",
        "content",
        "description",
        "agent_id",
        "session_id",
        "model",
        "thinking_effort",
        "id",
    ]
    assert all(
        isinstance(schema.get("description"), str) and schema["description"]
        for schema in properties.values()
    )
    assert properties["action"]["enum"] == ["run", "status", "cancel"]
    assert properties["description"]["type"] == "string"
    assert "maxLength" not in properties["description"]
    assert properties["thinking_effort"]["enum"] == [
        "high",
        "low",
        "max",
        "medium",
        "minimal",
        "none",
        "xhigh",
    ]


@pytest.mark.parametrize(
    ("arguments", "target", "title"),
    [
        ({"content": BRIEF}, "parent", None),
        ({"action": "run", "content": BRIEF}, "parent", None),
        (
            {"agent_id": "worker", "content": BRIEF, "description": "Review source"},
            "worker",
            "Review source",
        ),
        # Wrapped and aliased payloads.
        ({"arguments": {"Agent-ID": "worker", "content": BRIEF}}, "worker", None),
        ({"request": {"operation": "RUN", "content": BRIEF}}, "parent", None),
        ({"run": {"content": BRIEF}}, "parent", None),
        ({"request": BRIEF}, "parent", None),
        ({"request": {"content": BRIEF, "agent_id": "worker", "background": True}}, "worker", None),
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
        # Action synonyms
        ({"action": "spawn", "message": BRIEF}, "parent", None),
        ({"action": "Delegate", "content": BRIEF, "target": "worker"}, "worker", None),
    ],
)
async def test_call_shapes_reach_the_exact_agent_and_task(
    harness: SubAgentHarness, arguments: JsonObject, target: str, title: str | None
) -> None:
    before = copy.deepcopy(arguments)

    result = await harness.call(arguments)

    assert result["ok"], result
    assert arguments == before
    [child] = await harness.started()
    assert child.run.agent_id == target == result["data"]["agent_id"]
    assert child.run.session_id == result["data"]["session_id"]
    assert await child.task() == BRIEF
    assert result["data"]["note"] == TOP_LEVEL_BACKGROUND_NOTE
    assert harness.sessions.list_summaries(target)[0]["auto_title"] == (title or DEFAULT_TITLE)


@pytest.mark.parametrize(
    ("arguments", "task"),
    [
        (
            {"goal": "Fix the failing test.", "context": "The repository uses Python 3.12."},
            "Fix the failing test.\n\nContext:\nThe repository uses Python 3.12.",
        ),
        ({"context": BRIEF}, BRIEF),
    ],
)
async def test_hermes_context_travels_with_its_goal(
    harness: SubAgentHarness, arguments: JsonObject, task: str
) -> None:
    assert (await harness.call(arguments))["ok"]
    [child] = await harness.started()
    assert await child.task() == task


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
        ({"content": BRIEF, "agent_id": "   "}, "parent"),
        ({"content": BRIEF, "agent_id": None, "action": None}, "parent"),
        ({"content": BRIEF, "agent_id": "worker", "session_id": "new"}, "worker"),
        ({"content": BRIEF, "agent_id": "worker", "session_id": ""}, "worker"),
    ],
)
async def test_placeholder_fields_count_as_omitted(
    harness: SubAgentHarness, arguments: JsonObject, target: str
) -> None:
    result = await harness.call(arguments)

    assert result["ok"], result
    [child] = await harness.started()
    assert child.run.agent_id == target
    assert await child.task() == BRIEF
    # A new Session, the Agent's own model and effort, and no notes about the placeholders.
    assert len(harness.sessions.list(target)) == 1
    assert harness.loop.tasks[BRIEF].overrides is None
    assert result["data"]["note"] == TOP_LEVEL_BACKGROUND_NOTE


def _unknown(name: str, hint: str = "") -> str:
    return f'subagent was not run:\n- "{name}" is not a parameter.{hint}\n{PARAMETER_LIST}'


def _conflict(field: str, first: str, second: str, first_value: str, second_value: str) -> str:
    return (
        f'Conflicting values for {field}: {first} is "{first_value}" and {second} is '
        f'"{second_value}". Send only the intended one.'
    )


def _id_without_action(work_id: str) -> str:
    return (
        f"subagent was not run: it received work id {work_id} but no action. To inspect that "
        f'work, call {{"action": "status", "id": "{work_id}"}}; to stop it, call '
        f'{{"action": "cancel", "id": "{work_id}"}}. To delegate new work, send the task as '
        '"content" without "id".'
    )


CONTINUE_WITHOUT_SESSION = (
    "subagent was not run: continuing a Sub-Agent needs its Session. Send the agent_id and "
    'session_id from that Sub-Agent\'s result with the follow-up as "content", or omit '
    '"action" to start new work.'
)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        # No task, or a decision is missing.
        ({}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"description": "Review source"}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"content": ""}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"content": " "}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"content": "."}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"id": "existing-work"}, _id_without_action("existing-work")),
        ({"id": "sub_abcdefghijkl", "agent_id": "worker"}, _id_without_action("sub_abcdefghijkl")),
        # An explicit invalid choice never counts as omission.
        (
            {"content": BRIEF, "action": "rn"},
            'subagent was not run: "action" must be one of "run", "status", "cancel"; '
            'received "rn".',
        ),
        ({"content": BRIEF, "action": "cancel"}, SUBAGENT_CANCEL_NOTHING_TRACKED_MESSAGE),
        (
            {"content": BRIEF, "thinking_effort": "extreme"},
            'subagent was not run: "thinking_effort" must be one of "high", "low", "max", '
            '"medium", "minimal", "none", "xhigh"; received "extreme".',
        ),
        # Conflicting instructions are never settled by picking one.
        (
            {"content": BRIEF, "agent_id": "worker", "Agent-ID": "parent"},
            _conflict("agent_id", "agent_id", "Agent-ID", "worker", "parent"),
        ),
        (
            {"content": "Task.", "prompt": "Another task."},
            _conflict("content", "content", "prompt", "Task.", "Another task."),
        ),
        (
            {"content": BRIEF, "action": "run", "operation": "cancel"},
            _conflict("action", "action", "operation", "run", "cancel"),
        ),
        (
            {"content": BRIEF, "background": True, "blocking": True},
            'Conflicting "background" and "blocking" values; provide one intended value.',
        ),
        # Requests vBot cannot honor as written.
        ({"content": BRIEF, "priority": "high"}, _unknown("priority")),
        ({"content": BRIEF, "run_id": "private-run"}, _unknown("run_id", ' Did you mean "id"?')),
        ({"content": BRIEF, "unexpected": True}, _unknown("unexpected")),
        ({"action": "status", "id": "sub_test", "unexpected": True}, _unknown("unexpected")),
        ({"action": "cancel", "id": "sub_test", "unexpected": True}, _unknown("unexpected")),
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
        (
            {"action": "resume", "content": "Go on.", "agent_id": "worker"},
            CONTINUE_WITHOUT_SESSION,
        ),
        (
            {"action": "continue", "content": "Go on.", "session_id": "new"},
            CONTINUE_WITHOUT_SESSION,
        ),
    ],
)
async def test_unclear_or_unsupported_calls_are_refused_before_any_work(
    harness: SubAgentHarness, arguments: JsonObject, message: str
) -> None:
    before = copy.deepcopy(arguments)

    result = await harness.call(arguments)

    assert result["error"] == {"code": "invalid_arguments", "message": message}
    assert arguments == before
    await harness.settle()
    assert harness.manager.started == []
    assert harness.manager.enqueued == []
    assert harness.sessions.list("parent") == []
    assert harness.sessions.list("worker") == []


async def test_empty_toolsets_request_nothing(harness: SubAgentHarness) -> None:
    result = await harness.call({"goal": BRIEF, "toolsets": []})
    assert result["ok"], result


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "status"},
        {"action": "status", "ids": []},
        {"action": "status", "content": "spawn"},
        {"action": "list", "id": ""},
    ],
)
async def test_status_without_tracked_work_is_an_empty_listing(
    harness: SubAgentHarness, arguments: JsonObject
) -> None:
    result = await harness.call(arguments)

    assert result == {"ok": True, "error": None, "data": {"subagents": []}, "artifacts": []}
    assert harness.manager.started == []


async def test_status_ignores_fields_it_does_not_use(harness: SubAgentHarness) -> None:
    child = await harness.spawn({"content": BRIEF, "agent_id": "worker"})

    single = await harness.call(
        {
            "action": "status",
            "agent_id": "worker",
            "content": "status",
            "description": "Audit progress",
            "id": child["id"],
            "model": "openai/x",
            "session_id": child["session_id"],
            "thinking_effort": "high",
        }
    )
    listing = await harness.call({"action": "list", "id": "", "agent_id": "worker"})

    assert single["ok"], single
    assert (single["data"]["id"], single["data"]["status"]) == (child["id"], "running")
    assert single["data"]["note"] == SUBAGENT_STATUS_RUNNING_NOTE
    assert [entry["id"] for entry in listing["data"]["subagents"]] == [child["id"]]
    assert listing["data"]["note"] == SUBAGENT_STATUS_LIST_NOTE
    assert len(harness.manager.started) == 1


@pytest.mark.parametrize("action", ["cancel", "stop", "Kill"])
async def test_cancel_words_ignore_echoed_fields_and_stop_the_exact_work(
    harness: SubAgentHarness, action: str
) -> None:
    child = await harness.spawn({"content": BRIEF, "agent_id": "worker"})

    result = await harness.call(
        {
            "action": action,
            "id": child["id"],
            "agent_id": "worker",
            "content": "stop the audit",
            "session_id": child["session_id"],
            "thinking_effort": "high",
        }
    )

    assert result["ok"], result
    assert result["data"]["status"] == "cancelled"
    [started] = await harness.started()
    assert started.run.status.value == "cancelled"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"description": "Review imports", "prompt": BRIEF, "subagent_type": "worker"},
            [("description", "Review imports"), ("identifier", "worker")],
        ),
        (
            {"action": "run", "content": "Inspect the Tool contract.", "agent_id": "reviewer"},
            [("text", "Inspect the Tool contract."), ("identifier", "reviewer")],
        ),
        ({"action": "spawn", "task": "Check links"}, [("text", "Check links")]),
        (
            {"goal": "Check links", "title": "Links", "agent": "worker"},
            [("description", "Links"), ("identifier", "worker")],
        ),
        (
            {"action": "stop", "work_id": "sub_abcdefghijkl"},
            [("text", "cancel"), ("identifier", "sub_abcdefghijkl")],
        ),
        (
            {"action": "status", "id": "unused", "agent_id": "worker"},
            [("text", "status"), ("identifier", "worker")],
        ),
        ({"action": "status"}, [("text", "status")]),
    ],
)
async def test_display_labels_what_the_call_meant(
    harness: SubAgentHarness, arguments: JsonObject, expected: list[tuple[str, str]]
) -> None:
    display = harness.registry.get(SUBAGENT_TOOL_NAME).display.to_payload(arguments)

    assert [(part.get("kind", "text"), part["value"]) for part in display["primary"]] == expected
