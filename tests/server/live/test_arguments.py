"""Live Tool arguments as voice and backend Models actually write them."""

from __future__ import annotations

from typing import Any

import pytest

from server.live._arguments import PreparedLiveCall, prepare_live_call

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
    "arguments",
    [
        {"target": "s2", "text": "yes"},
        # Arguments as JSON text, as realtime voice wires deliver them.
        '{"target": "s2", "text": "yes"}',
    ],
)
def test_accepts_canonical_calls_as_objects_or_json_text(arguments: Any) -> None:
    assert prepared("send_message", arguments) == ("send_message", {"target": "s2", "text": "yes"})


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("send_message", {"session_id": "s2", "message": "yes"}, {"target": "s2", "text": "yes"}),
        ("stop", {"agent": "Coder"}, {"target": "Coder"}),
        ("read_output", {"target": ["s1"]}, {"target": "s1"}),
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
            "manage_terminals",
            {"action": "key", "terminal": "t1", "key": "Esc"},
            {"action": "key", "target": "t1", "key": "escape"},
        ),
        (
            "manage_terminals",
            {"action": "Key", "target": "t1", "key": "ArrowDown"},
            {"action": "key", "target": "t1", "key": "down"},
        ),
        (
            "manage_terminals",
            {"op": "maximize", "id": "t1"},
            {"action": "maximize", "target": "t1"},
        ),
        ("open", {"view": "Chat"}, {"view": "chat"}),
    ],
)
def test_repairs_field_names_and_values_of_other_harnesses(
    name: str, arguments: JsonObject, expected: JsonObject
) -> None:
    assert prepared(name, arguments)[1] == expected


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("send_message", {"target": "s2", "text": "None"}, {"target": "s2", "text": "None"}),
        ("send_message", {"session_id": "s2", "message": "n/a"}, {"target": "s2", "text": "n/a"}),
        (
            "start_agent_session",
            {"agent": "coder", "task": "Empty", "project": "none"},
            {"agent": "coder", "task": "Empty"},
        ),
        (
            "start_coding_terminal",
            {"program": "codex", "prompt": "???", "name": "None", "folder": "n/a"},
            {"program": "codex", "task": "???", "name": "None"},
        ),
        (
            "manage_terminals",
            {"action": "rename_group", "target": "Backend", "new_name": "TBD"},
            {"action": "rename_group", "target": "Backend", "name": "TBD"},
        ),
    ],
)
def test_passes_task_message_and_name_text_on_unchanged(
    name: str, arguments: JsonObject, expected: JsonObject
) -> None:
    # Words that stand for "not used" in a lookup field are the user's text here.
    assert prepared(name, arguments)[1] == expected


@pytest.mark.parametrize(
    ("name", "arguments", "problem"),
    [
        ("start_agent_session", {"agent": "none", "task": "x"}, '"agent" is required'),
        ("send_message", {"target": "s2", "text": "  \n"}, '"text" is required'),
        ("read_output", {"target": "null"}, '"target" is required'),
    ],
)
def test_a_stand_in_or_blank_value_leaves_the_field_out(
    name: str, arguments: JsonObject, problem: str
) -> None:
    code, message = refused(name, arguments)
    assert code == "invalid_arguments"
    assert problem in message


def test_refuses_unknown_tools_and_names_the_live_tools() -> None:
    offered = (
        "Call one of: overview, start_agent_session, start_coding_terminal, send_message, "
        "read_output, stop, open, manage_terminals, end_call."
    )
    assert refused("shell", {"command": "ls"}) == (
        "unknown_tool",
        f'There is no Live Tool called "shell". {offered}',
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
