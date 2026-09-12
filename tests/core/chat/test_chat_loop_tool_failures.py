"""Tests for chat loop tool failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.runs import (
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    RunStatus,
)
from core.tools import JsonObject as ToolJsonObject
from core.tools import (
    ToolContext,
    ToolRegistry,
    register_glob_tool,
    register_grep_tool,
    tool_failure,
    tool_success,
)
from core.utils.errors import ProviderError
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
    session_address,
)
from tests.core.chat.chat_loop_tools_test_support import (
    JsonObject,
)


@pytest.mark.asyncio
async def test_disallowed_tool_call_is_blocked_and_persisted_before_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Weather?", session_id="session-one")

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert persisted_roles(messages) == ["user", "assistant", "tool", "assistant"]
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == tool_failure(
        "tool_not_allowed",
        "Tool not allowed: get_weather",
    )


@pytest.mark.asyncio
async def test_registered_search_tools_execute_and_persist_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.tools.grep.shutil.which", lambda _command: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "code.py").write_text("print('alpha')\n", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["glob", "grep"],
        workspace=workspace,
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_glob", "name": "glob", "arguments": {"pattern": "**/*.txt"}},
                    {"id": "call_grep", "name": "grep", "arguments": {"pattern": "alpha"}},
                ],
            },
            {"content": "Search complete", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_glob_tool(tools)
    register_grep_tool(tools)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send(
        "coder", "Search files", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    tool_messages = [message for message in messages if message.role == "tool"]
    glob_content = tool_messages[0].content
    grep_content = tool_messages[1].content
    assert isinstance(glob_content, str)
    assert isinstance(grep_content, str)
    glob_result = json.loads(glob_content)
    grep_result = json.loads(grep_content)
    assert assistant.content == "Search complete"
    assert [message.name for message in tool_messages] == ["glob", "grep"]
    assert glob_result == tool_success({"content": "notes.txt"})
    assert grep_result == tool_success(
        {"content": "notes.txt:1: alpha\nsrc/code.py:1: print('alpha')"}
    )
    assert [
        event.payload["tool_call"]["name"]
        for event in run.events
        if event.type == TOOL_CALL_STARTED_EVENT
    ] == ["glob", "grep"]
    for event in run.events:
        if event.type not in {TOOL_CALL_STARTED_EVENT, TOOL_CALL_RESULT_EVENT}:
            continue
        tool_name = event.payload["tool_call"]["name"]
        assert event.payload["schema_fingerprint"] == tools.schema_fingerprint(tool_name)
    results_by_tool = {
        event.payload["tool_call"]["name"]: event.payload["result"]
        for event in run.events
        if event.type == TOOL_CALL_RESULT_EVENT
    }
    assert results_by_tool == {"glob": glob_result, "grep": grep_result}


@pytest.mark.asyncio
async def test_registered_search_tools_respect_agent_allowlist(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\n", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["glob"],
        workspace=workspace,
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_grep", "name": "grep", "arguments": {"pattern": "alpha"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_glob_tool(tools)
    register_grep_tool(tools)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Search files", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    failure = tool_failure("tool_not_allowed", "Tool not allowed: grep")
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == failure
    result_payload = next(
        event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ).payload
    assert result_payload["tool_call"] == {"id": "call_grep", "index": 0, "name": "grep"}
    assert result_payload["result"] == failure
    assert result_payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_output_truncated_tool_calls_persist_failures_without_handler_side_effects(
    tmp_path: Path,
) -> None:
    invocations: list[ToolJsonObject] = []

    def write_handler(
        _context: ToolContext,
        arguments: ToolJsonObject,
    ) -> ToolJsonObject:
        invocations.append(arguments)
        return tool_success({"written": arguments["path"]})

    tools = ToolRegistry()
    tools.register(
        "write_probe",
        "Write a probe file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        write_handler,
    )
    adapter = StubAdapter(
        [
            {
                "content": "I started preparing both writes.",
                "terminal_outcome": "output_truncated",
                "tool_calls": [
                    {
                        "id": "call_first",
                        "name": "write_probe",
                        "arguments": {"path": "first.txt"},
                    },
                    {
                        "id": "call_second",
                        "name": "write_probe",
                        "arguments": {"path": "second.txt", "content": ""},
                    },
                ],
            },
            {"content": "I will reissue complete calls if needed.", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(
            id="coder",
            model="openai/gpt-5.2",
            allowed_tools=["write_probe"],
        ),
        adapter=adapter,
        tools=tools,
    )

    assistant = await build_chat_loop(runtime).send(
        "coder",
        "Write both files",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    tool_messages = [message for message in messages if message.role == "tool"]
    assert assistant.content == "I will reissue complete calls if needed."
    assert invocations == []
    assert [message.tool_call_id for message in tool_messages] == [
        "call_first",
        "call_second",
    ]
    assert [
        json.loads(cast(str, message.content))["error"]["code"] for message in tool_messages
    ] == [
        "tool_call_truncated",
        "tool_call_truncated",
    ]
    assert [message["tool_call_id"] for message in adapter.requests[1]["messages"][-2:]] == [
        "call_first",
        "call_second",
    ]
    assert messages[1].content == "I started preparing both writes."
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_outcome", ["content_filtered", "error", "unknown"])
async def test_unsafe_terminal_outcome_fails_closed_before_tool_handler(
    tmp_path: Path,
    terminal_outcome: str,
) -> None:
    invocation_count = 0

    def handler(_context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        nonlocal invocation_count
        invocation_count += 1
        return tool_success({})

    tools = ToolRegistry()
    tools.register("probe", "Probe.", {"type": "object"}, handler)
    adapter = StubAdapter(
        [
            {
                "content": "unsafe partial",
                "terminal_outcome": terminal_outcome,
                "tool_calls": [{"id": "call_probe", "name": "probe", "arguments": {}}],
            }
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(
            id="coder",
            model="openai/gpt-5.2",
            allowed_tools=["probe"],
        ),
        adapter=adapter,
        tools=tools,
    )

    with pytest.raises(ProviderError, match="unsafe terminal outcome"):
        await build_chat_loop(runtime).send(
            "coder",
            "Run probe",
            session_id="session-one",
        )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert invocation_count == 0
    assert persisted_roles(messages) == ["user", "assistant", "tool", "error"]
    assert json.loads(cast(str, messages[2].content))["error"]["code"] == "tool_call_rejected"


@pytest.mark.asyncio
async def test_tool_non_envelope_result_is_failure_envelope(tmp_path: Path) -> None:
    async def invalid_handler(
        _context: ToolContext,
        _arguments: ToolJsonObject,
    ) -> JsonObject:
        return {"content": "not enveloped"}

    tools = ToolRegistry()
    tools.register("invalid", "Invalid tool.", {"type": "object"}, invalid_handler)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["invalid"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "invalid", "arguments": {}}],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
    )

    assistant = await build_chat_loop(runtime).send(
        "coder", "Run invalid", session_id="session-one"
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    failure = tool_failure(
        "invalid_tool_result",
        "Tool handler must return a valid result envelope: invalid",
    )
    assert assistant.content == "Recovered"
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == failure
    run = next(iter(runtime.chat_runs._runs.values()))
    result_payload = next(
        event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ).payload
    assert result_payload["tool_call"] == {"id": "call_1", "index": 0, "name": "invalid"}
    assert result_payload["result"] == failure
    assert result_payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_malformed_non_streaming_call_fails_without_blocking_valid_sibling(
    tmp_path: Path,
) -> None:
    executed_arguments: list[JsonObject] = []

    def echo_handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        executed_arguments.append(arguments)
        return tool_success({"value": arguments["value"]})

    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_bad", "name": "echo", "arguments": "{not json"},
                    {"id": "call_ok", "name": "echo", "arguments": {"value": "kept"}},
                ],
            },
            {"content": "Recovered after the rejected sibling.", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "echo",
        "Echo one value.",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        echo_handler,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["echo"]),
        adapter=adapter,
        tools=tools,
    )

    result = await build_chat_loop(runtime).send(
        "coder",
        "Run both calls",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert result.content == "Recovered after the rejected sibling."
    assert executed_arguments == [{"value": "kept"}]
    assert persisted_roles(messages) == ["user", "assistant", "tool", "tool", "assistant"]
    assert messages[1].tool_calls is not None
    assert messages[1].tool_calls[0].rejection is not None
    assert messages[1].tool_calls[1].rejection is None
    assert isinstance(messages[2].content, str)
    assert isinstance(messages[3].content, str)
    assert json.loads(messages[2].content)["error"]["code"] == "malformed_tool_arguments"
    assert json.loads(messages[3].content) == tool_success({"value": "kept"})
    run = next(iter(runtime.chat_runs._runs.values()))
    assert run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_malformed_tool_calls_container_becomes_rejected_call(tmp_path: Path) -> None:
    adapter = StubAdapter(
        [
            {"content": None, "tool_calls": "broken-container"},
            {"content": "Recovered from the malformed Tool Call container.", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"]),
        adapter=adapter,
    )

    result = await build_chat_loop(runtime).send(
        "coder",
        "Try the malformed call",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert result.content == "Recovered from the malformed Tool Call container."
    assert persisted_roles(messages) == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls is not None
    assert messages[1].tool_calls[0].name == "invalid_tool_call"
    assert messages[1].tool_calls[0].rejection is not None
    assert messages[1].tool_calls[0].rejection.code == "malformed_tool_call"
    assert isinstance(messages[2].content, str)
    assert json.loads(messages[2].content)["error"]["code"] == "malformed_tool_call"
