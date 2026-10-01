"""Live Tool calls as voice and backend Models actually write them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.model_tasks.live import LiveToolRun, live_success
from server.live._arguments import PreparedLiveCall, prepare_live_call, run_live_call

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
        ("navigate", {"view": "cronjobs"}, ("open", {"view": "cron"})),
        ("hangup", {}, ("end_call", {})),
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
            "terminal",
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
        ("read", {"target": "null"}, '"target" is required'),
    ],
)
def test_a_stand_in_or_blank_value_leaves_the_field_out(
    name: str, arguments: JsonObject, problem: str
) -> None:
    code, message = refused(name, arguments)
    assert code == "invalid_arguments"
    assert problem in message


def test_refuses_unknown_tools_and_names_the_offered_ones() -> None:
    offered = (
        "Call one of: overview, start_agent_session, start_coding_terminal, send_message, read, "
        "stop, open, terminal, end_call."
    )
    assert refused("shell", {"command": "ls"}) == (
        "unknown_tool",
        f'There is no Tool called "shell". {offered}',
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


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        self.now += 0.25
        return self.now


async def run(
    name: Any, arguments: Any, *, rejection: JsonObject | None = None, execute: Any = None
) -> tuple[LiveToolRun, list[tuple[str, JsonObject]], list[JsonObject]]:
    executed: list[tuple[str, JsonObject]] = []
    records: list[JsonObject] = []

    async def succeed(tool: str, tool_arguments: JsonObject) -> JsonObject:
        executed.append((tool, tool_arguments))
        return live_success("Done.")

    result = await run_live_call(
        name,
        arguments,
        execute=execute or succeed,
        rejection=rejection,
        record=records.append,
        mode="direct",
        clock=Clock(),
    )
    return result, executed, records


@pytest.mark.asyncio
async def test_a_call_runs_once_as_prepared_and_is_recorded_as_written_and_as_run() -> None:
    result, executed, records = await run("functions.show", '{"terminal": "t1"}')

    assert executed == [("open", {"target": "t1"})]
    assert result == LiveToolRun(live_success("Done."), changed="open")
    assert records == [
        {
            "type": "tool",
            "mode": "direct",
            "called": "functions.show",
            "tool": "open",
            "arguments": '{"terminal": "t1"}',
            "run_arguments": {"target": "t1"},
            "ok": True,
            "result": "Done.",
            "stopped": False,
            "duration_ms": 250,
        }
    ]


@pytest.mark.asyncio
async def test_a_read_only_call_changes_nothing() -> None:
    result, _executed, _records = await run("overview", {})
    assert result.changed == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "rejection", "code"),
    [
        ("shell", {"command": "ls"}, None, "unknown_tool"),
        ("send_message", "not json", None, "invalid_arguments"),
        ("send_message", {"target": "s1"}, None, "invalid_arguments"),
        (
            "send_message",
            "{broken",
            {"code": "invalid_arguments", "message": "Not JSON."},
            "invalid_arguments",
        ),
    ],
    ids=["unknown-tool", "undecodable", "missing-field", "adapter-rejection"],
)
async def test_a_refused_call_does_not_run_and_is_recorded(
    name: str, arguments: Any, rejection: JsonObject | None, code: str
) -> None:
    result, executed, records = await run(name, arguments, rejection=rejection)

    assert executed == []
    assert result.changed == ""
    assert result.result["error"]["code"] == code
    assert [(record["tool"], record["ok"], record["stopped"]) for record in records] == [
        (None, False, False)
    ]


@pytest.mark.asyncio
async def test_a_stopped_call_is_still_recorded_without_a_result() -> None:
    records: list[JsonObject] = []
    started = asyncio.Event()

    async def hang(tool: str, tool_arguments: JsonObject) -> JsonObject:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        run_live_call("stop", {"target": "s1"}, execute=hang, record=records.append, clock=Clock())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [(r["tool"], r["result"], r["stopped"]) for r in records] == [("stop", None, True)]
