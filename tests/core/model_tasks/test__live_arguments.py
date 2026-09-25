"""Live Tool calls as voice and backend Models actually write them."""

from __future__ import annotations

from typing import Any

import pytest

from core.model_tasks._live_arguments import PreparedLiveCall, prepare_live_call

JsonObject = dict[str, Any]


def prepared(name: Any, arguments: Any) -> tuple[str, JsonObject]:
    call = prepare_live_call(name, arguments)
    assert isinstance(call, PreparedLiveCall), call
    return call.name, call.arguments


def refused(name: Any, arguments: Any) -> tuple[str, str]:
    result = prepare_live_call(name, arguments)
    assert isinstance(result, dict), result
    assert result["ok"] is False
    return result["error"]["code"], result["error"]["message"]


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (
            "send_message",
            {"target": "s2", "text": "yes"},
            ("send_message", {"target": "s2", "text": "yes"}),
        ),
        # Arguments as JSON text, as realtime voice wires deliver them.
        (
            "send_message",
            '{"target": "s2", "text": "yes"}',
            ("send_message", {"target": "s2", "text": "yes"}),
        ),
        ("default_api:overview", None, ("overview", {})),
        ("functions.read", {"target": "t1"}, ("read", {"target": "t1"})),
        (
            "sendMessage",
            {"target": "s1", "text": "x"},
            ("send_message", {"target": "s1", "text": "x"}),
        ),
        (
            "Send-Message",
            {"target": "s1", "text": "x"},
            ("send_message", {"target": "s1", "text": "x"}),
        ),
    ],
)
def test_accepts_canonical_calls_in_wire_spellings(
    name: str, arguments: Any, expected: tuple[str, JsonObject]
) -> None:
    assert prepared(name, arguments) == expected


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("status", {}, ("overview", {})),
        (
            "startCodex",
            {"prompt": "fix the tests"},
            ("start_coding_terminal", {"program": "codex", "task": "fix the tests"}),
        ),
        (
            "delegate",
            {"agent": "Coder", "prompt": "Write docs", "copies": "3"},
            ("start_agent_session", {"agent": "Coder", "task": "Write docs", "count": 3}),
        ),
        ("cancel", {"session": "s4"}, ("stop", {"target": "s4"})),
        ("navigate", {"view": "Terminal"}, ("open", {"view": "terminals"})),
    ],
)
def test_maps_other_tool_names_whose_intent_is_clear(
    name: str, arguments: JsonObject, expected: tuple[str, JsonObject]
) -> None:
    assert prepared(name, arguments) == expected


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("send_message", {"session_id": "s2", "message": "yes"}, {"target": "s2", "text": "yes"}),
        ("stop", {"agent": "Coder"}, {"target": "Coder"}),
        ("read", {"target": ["s1"]}, {"target": "s1"}),
        (
            "start_coding_terminal",
            {"cli": "Claude Code", "prompt": "fix", "workdir": "C:\\work", "n": 2},
            {"program": "claude", "task": "fix", "folder": "C:\\work", "count": 2},
        ),
        (
            "start_coding_terminal",
            {"program": "codex", "count": "2"},
            {"program": "codex", "count": 2},
        ),
        (
            "start_agent_session",
            {"agent": "coder", "task": "x", "project": "null"},
            {"agent": "coder", "task": "x"},
        ),
        (
            "start_agent_session",
            {"agent": "coder", "task": "x", "project": ""},
            {"agent": "coder", "task": "x"},
        ),
        (
            "terminal",
            {"action": "key", "terminal": "t1", "key": "Esc"},
            {"action": "key", "target": "t1", "key": "escape"},
        ),
        (
            "terminal",
            {"action": "Key", "target": "t1", "key": "ArrowDown"},
            {"action": "key", "target": "t1", "key": "down"},
        ),
        ("terminal", {"op": "maximize", "id": "t1"}, {"action": "maximize", "target": "t1"}),
        ("open", {"view": "Chat"}, {"view": "chat"}),
    ],
)
def test_repairs_field_names_and_values_of_other_harnesses(
    name: str, arguments: JsonObject, expected: JsonObject
) -> None:
    assert prepared(name, arguments)[1] == expected


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"action": "send", "agent_id": "coder", "session_id": "ses_1", "text": "go"},
            ("send_message", {"target": "ses_1", "text": "go"}),
        ),
        ({"action": "sessions", "agent_id": "coder"}, ("overview", {"agent": "coder"})),
        ({"action": "open", "view": "terminals"}, ("open", {"view": "terminals"})),
    ],
)
def test_maps_exact_calls_of_the_former_app_tool(
    arguments: JsonObject, expected: tuple[str, JsonObject]
) -> None:
    assert prepared("vbot_app", arguments) == expected


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"action": "start", "program": "codex", "count": 2, "workdir": "C:\\work"},
            ("start_coding_terminal", {"program": "codex", "count": 2, "folder": "C:\\work"}),
        ),
        (
            {"action": "input", "terminal_id": "term_1", "key": "enter"},
            ("terminal", {"action": "key", "target": "term_1", "key": "enter"}),
        ),
        (
            {"action": "input", "terminal_id": "term_1", "text": "go"},
            ("send_message", {"target": "term_1", "text": "go"}),
        ),
        ({"action": "restore"}, ("terminal", {"action": "restore"})),
    ],
)
def test_maps_exact_calls_of_the_former_terminal_tool(
    arguments: JsonObject, expected: tuple[str, JsonObject]
) -> None:
    assert prepared("vbot_terminal", arguments) == expected


def test_refuses_unknown_tools_and_names_the_offered_ones() -> None:
    offered = (
        "Call one of: overview, start_agent_session, start_coding_terminal, send_message, read, "
        "stop, open, terminal."
    )
    assert refused("shell", {"command": "ls"}) == (
        "unknown_tool",
        f'There is no Tool called "shell". {offered}',
    )
    assert refused("vbot_app", {"action": "bogus"}) == (
        "unknown_tool",
        f"vbot_app is no longer available. {offered}",
    )


@pytest.mark.parametrize("arguments", ["not json", ["s1"], "[1, 2]"])
def test_refuses_arguments_that_are_not_one_object(arguments: Any) -> None:
    assert refused("send_message", arguments) == (
        "invalid_arguments",
        "The arguments are not one JSON object. Call send_message again with an object such as "
        '{"target": "s2", "text": "<the message>"}.',
    )


@pytest.mark.parametrize(
    ("name", "arguments", "problem"),
    [
        (
            "start_coding_terminal",
            {"program": "bash"},
            '"program" must be one of "codex", "claude"',
        ),
        ("start_coding_terminal", {"program": "codex", "count": 11}, '"count" must be at most 10'),
        ("start_agent_session", {"agent": "coder"}, '"task" is required'),
        # Two spellings of one field disagree; neither is guessed.
        (
            "send_message",
            {"target": "s1", "text": "a", "message": "b"},
            '"message" is not a parameter',
        ),
    ],
)
def test_refuses_invalid_calls_with_an_example_of_a_valid_one(
    name: str, arguments: JsonObject, problem: str
) -> None:
    code, message = refused(name, arguments)
    assert code == "invalid_arguments"
    assert problem in message
    assert f"Call {name} again, for example with {{" in message
