"""The ``subagent`` Tool: its definition, the call shapes it accepts, and its display.

Other harness dialects, action synonyms, placeholder fields and copied result
fields reach exactly the Agent and task the call meant. Calls whose meaning is
unclear, or that ask for something vBot cannot do, are refused before any work
starts, with the corrected call.
"""

from __future__ import annotations

import copy

import pytest

from core.subagents._constants import (
    SUBAGENT_BACKGROUND_IGNORED_NOTE,
    SUBAGENT_MISSING_TASK_MESSAGE,
    SUBAGENT_STARTED_NOTE,
)
from core.tools.availability import MESSAGE_PARENT_TOOL_NAME
from core.tools.subagent import (
    MESSAGE_PARENT_TOOL_DESCRIPTION,
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
PARAMETER_LIST = (
    "subagent parameters: action, content, description, agent_id, id, model, thinking_effort."
)


async def test_tool_definitions_are_flat_open_contracts(harness: SubAgentHarness) -> None:
    subagent = harness.registry.get(SUBAGENT_TOOL_NAME)
    assert subagent.description == SUBAGENT_TOOL_DESCRIPTION
    assert subagent.parameters == SUBAGENT_TOOL_PARAMETERS
    assert "additionalProperties" not in subagent.parameters
    assert subagent.parameters["required"] == []
    properties = subagent.parameters["properties"]
    assert list(properties) == [
        "action",
        "content",
        "description",
        "agent_id",
        "id",
        "model",
        "thinking_effort",
    ]
    assert all(schema.get("description") for schema in properties.values())
    assert properties["action"]["enum"] == ["run", "send", "list", "cancel"]

    message_parent = harness.registry.get(MESSAGE_PARENT_TOOL_NAME)
    assert message_parent.description == MESSAGE_PARENT_TOOL_DESCRIPTION
    assert message_parent.parameters["required"] == ["content"]
    # Offered only through the grant of a Sub-Agent Session, never from the catalog.
    assert message_parent.catalog_visible is False
    assert message_parent.session_scoped is True


@pytest.mark.parametrize(
    ("arguments", "target", "title"),
    [
        ({"content": BRIEF, "description": "Review source"}, "parent", "Review source"),
        (
            {"action": "run", "agent_id": "worker", "content": BRIEF, "description": "Review"},
            "worker",
            "Review",
        ),
        # Wrapped and aliased payloads.
        (
            {"arguments": {"Agent-ID": "worker", "content": BRIEF, "title": "Review"}},
            "worker",
            "Review",
        ),
        (
            {"request": {"operation": "RUN", "content": BRIEF, "label": "Review"}},
            "parent",
            "Review",
        ),
        # Claude Code / opencode Task
        (
            {"description": "Review imports", "prompt": BRIEF, "subagent_type": "worker"},
            "worker",
            "Review imports",
        ),
        (
            {"description": "Review imports", "prompt": BRIEF, "run_in_background": True},
            "parent",
            "Review imports",
        ),
        # pi, OpenClaw, Hermes
        (
            {"task": BRIEF, "label": "Review imports", "agentId": "worker"},
            "worker",
            "Review imports",
        ),
        ({"instructions": BRIEF, "title": "Review imports"}, "parent", "Review imports"),
        # Action synonyms
        ({"action": "spawn", "message": BRIEF, "summary": "Review"}, "parent", "Review"),
        (
            {"action": "Delegate", "content": BRIEF, "target": "worker", "title": "Review"},
            "worker",
            "Review",
        ),
    ],
)
async def test_call_shapes_reach_the_exact_agent_and_task(
    harness: SubAgentHarness, arguments: JsonObject, target: str, title: str
) -> None:
    before = copy.deepcopy(arguments)

    result = await harness.call(arguments)

    assert result["ok"], result
    assert arguments == before
    assert result["data"]["agent_id"] == target
    assert [turn.content for turn in harness.loop.turns] == [BRIEF]
    assert harness.sessions.list_summaries(target)[0]["auto_title"] == title
    assert result["data"]["note"] == SUBAGENT_STARTED_NOTE


async def test_requested_foreground_run_is_named_as_ignored(harness: SubAgentHarness) -> None:
    result = await harness.call(
        {"content": BRIEF, "description": "Review", "run_in_background": False}
    )

    assert result["ok"], result
    assert result["data"]["note"] == f"{SUBAGENT_BACKGROUND_IGNORED_NOTE} {SUBAGENT_STARTED_NOTE}"


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
    result = await harness.call({**arguments, "title": "Fix test"})

    assert result["ok"], result
    assert [turn.content for turn in harness.loop.turns] == [task]


@pytest.mark.parametrize(
    ("arguments", "target"),
    [
        (
            {
                "action": "run",
                "agent_id": "worker",
                "content": BRIEF,
                "id": "unused",
                "model": "",
                "session_id": "invalid-placeholder",
                "thinking_effort": "",
            },
            "worker",
        ),
        ({"content": BRIEF, "id": ".", "session_id": ".", "agent_id": "."}, "parent"),
        (
            {
                "content": BRIEF,
                "id": "placeholder",
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
async def test_placeholder_fields_count_as_omitted(
    harness: SubAgentHarness, arguments: JsonObject, target: str
) -> None:
    result = await harness.call({**arguments, "description": "Audit"})

    assert result["ok"], result
    assert result["data"]["agent_id"] == target
    assert [turn.content for turn in harness.loop.turns] == [BRIEF]
    child = harness.subagent_session(result["data"]["id"])
    assert harness.sessions.metadata_value(child, "agent_overrides") is None
    assert result["data"]["note"] == SUBAGENT_STARTED_NOTE


def _unknown(name: str, hint: str = "") -> str:
    return f'subagent was not run:\n- "{name}" is not a parameter.{hint}\n{PARAMETER_LIST}'


def _conflict(field: str, first: str, second: str, first_value: str, second_value: str) -> str:
    return (
        f'Conflicting values for {field}: {first} is "{first_value}" and {second} is '
        f'"{second_value}". Send only the intended one.'
    )


def _id_without_action(subagent_id: str) -> str:
    return (
        f"subagent was not run: it received Sub-Agent id {subagent_id} but no action. To "
        f'message that Sub-Agent, call {{"action": "send", "id": "{subagent_id}", "content": '
        f'"<message>"}}; to stop it, call {{"action": "cancel", "id": "{subagent_id}"}}.'
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        # No task, or a decision is missing.
        ({}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"description": "Review source"}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"content": " "}, SUBAGENT_MISSING_TASK_MESSAGE),
        ({"id": "sub_abcdefghijkl", "agent_id": "worker"}, _id_without_action("sub_abcdefghijkl")),
        (
            {"goal": "Check links."},
            'subagent was not run: a new Sub-Agent needs "description", a 3-5 word title that '
            'the user sees. Repeat the call with it: {"description": "<3-5 word title>", '
            '"content": "Check links."}.',
        ),
        # An explicit invalid choice never counts as omission.
        (
            {"content": BRIEF, "action": "rn"},
            'subagent was not run: "action" must be one of "run", "send", "list", "cancel"; '
            'received "rn".',
        ),
        (
            {"content": BRIEF, "description": "Audit", "thinking_effort": "extreme"},
            'subagent was not run: "thinking_effort" must be one of "high", "low", "max", '
            '"medium", "minimal", "none", "xhigh"; received "extreme".',
        ),
        # Conflicting instructions are never settled by picking one.
        (
            {"content": BRIEF, "agent_id": "worker", "Agent-ID": "parent"},
            _conflict("agent_id", "agent_id", "Agent-ID", "worker", "parent"),
        ),
        (
            {"content": BRIEF, "background": True, "blocking": True},
            'Conflicting "background" and "blocking" values; provide one intended value.',
        ),
        # Requests vBot cannot honor as written.
        ({"content": BRIEF, "priority": "high"}, _unknown("priority")),
        ({"content": BRIEF, "run_id": "private-run"}, _unknown("run_id", ' Did you mean "id"?')),
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
            {"action": "resume", "content": "Go on."},
            'send needs "id", the Sub-Agent to message; nothing was sent. You have no '
            'Sub-Agents. Call {"action": "send", "id": "<id>", "content": "<message>"}.',
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
    assert harness.loop.turns == []
    assert harness.sessions.list("worker") == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "list"},
        {"action": "status", "content": "spawn"},
        {"action": "check", "id": ""},
    ],
)
async def test_list_without_subagents_is_an_empty_listing(
    harness: SubAgentHarness, arguments: JsonObject
) -> None:
    result = await harness.call(arguments)

    assert result == {"ok": True, "error": None, "data": {"subagents": []}, "artifacts": []}


@pytest.mark.parametrize("action", ["cancel", "stop", "Kill"])
async def test_cancel_words_ignore_echoed_fields_and_stop_the_exact_subagent(
    harness: SubAgentHarness, action: str
) -> None:
    harness.loop.hold(BRIEF)
    child = await harness.spawn(BRIEF, agent_id="worker")

    result = await harness.call(
        {
            "action": action,
            "id": child["id"],
            "agent_id": "worker",
            "content": "stop the audit",
            "session_id": child["session_id"],
        }
    )

    assert result["ok"], result
    assert result["data"]["status"] == "cancelled"


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
        (
            {"action": "message", "id": "sub_abcdefghijkl", "content": "More."},
            [("text", "send"), ("identifier", "sub_abcdefghijkl")],
        ),
        (
            {"action": "stop", "work_id": "sub_abcdefghijkl"},
            [("text", "cancel"), ("identifier", "sub_abcdefghijkl")],
        ),
        ({"action": "status"}, [("text", "list")]),
    ],
)
async def test_display_labels_what_the_call_meant(
    harness: SubAgentHarness, arguments: JsonObject, expected: list[tuple[str, str]]
) -> None:
    display = harness.registry.get(SUBAGENT_TOOL_NAME).display.to_payload(arguments)

    assert [(part.get("kind", "text"), part["value"]) for part in display["primary"]] == expected


async def test_details_show_the_task_the_message_and_the_listed_subagents(
    harness: SubAgentHarness,
) -> None:
    display = harness.registry.get(SUBAGENT_TOOL_NAME).display

    def details(arguments: JsonObject, data: JsonObject) -> list[JsonObject]:
        result: JsonObject = {"ok": True, "error": None, "data": data, "artifacts": []}
        shown: list[JsonObject] = display.to_payload(arguments, result=result)["details"]
        return shown

    assert details(
        {"task": "Check links", "title": "Links"}, {"id": "sub_a", "status": "running"}
    ) == [{"type": "text", "label": "task", "text": "Check links"}]
    assert details(
        {"action": "send", "id": "sub_a", "content": "Also docs."},
        {"id": "sub_a", "status": "steered"},
    ) == [
        {"type": "text", "label": "content", "text": "Also docs."},
        {
            "type": "notice",
            "level": "info",
            "text": "The sub-agent receives the message at its next step.",
        },
    ]
    assert details(
        {"action": "list"},
        {
            "subagents": [
                {
                    "id": "sub_a",
                    "title": "Check links",
                    "agent_id": "worker",
                    "state": "working",
                    "last_tool": "bash",
                }
            ]
        },
    ) == [
        {"type": "results", "items": [{"title": "Check links", "meta": "worker · working · bash"}]}
    ]
