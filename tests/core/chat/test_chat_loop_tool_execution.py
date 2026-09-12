"""Tests for chat loop tool execution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.runs import (
    TOOL_CALL_RESULT_EVENT,
    RunStatus,
)
from core.tools import JsonObject as ToolJsonObject
from core.tools import (
    ToolContext,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)


@pytest.mark.asyncio
async def test_same_turn_tool_calls_run_concurrently_and_persist_in_call_order(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["slow"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "slow", "arguments": {"value": "first"}},
                    {"id": "call_2", "name": "slow", "arguments": {"value": "second"}},
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    second_started = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def slow_handler(context: ToolContext, arguments: ToolJsonObject) -> ToolJsonObject:
        if context.tool_call_id == "call_1":
            await second_started.wait()
            first_can_finish.set()
        else:
            second_started.set()
        return tool_success({"value": arguments["value"], "id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register(
        "slow",
        "Slow tool.",
        {"type": "object"},
        slow_handler,
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Run tools", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    result_events = [event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT]
    assert assistant.content == "Done"
    assert first_can_finish.is_set()
    assert [message.tool_call_id for message in messages if message.role == "tool"] == [
        "call_1",
        "call_2",
    ]
    assert [event.payload["tool_call"]["id"] for event in result_events] == ["call_2", "call_1"]
    tool_result_ids: list[str] = []
    for message in messages:
        if message.role != "tool":
            continue
        assert isinstance(message.content, str)
        tool_result_ids.append(json.loads(message.content)["data"]["id"])
    assert tool_result_ids == [
        "call_1",
        "call_2",
    ]


@pytest.mark.asyncio
async def test_same_tool_sibling_calls_run_in_parallel(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["same"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "same", "arguments": {}},
                    {"id": "call_2", "name": "same", "arguments": {}},
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    active_count = 0
    max_active_count = 0
    release = asyncio.Event()

    async def same_handler(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if max_active_count == 2:
            release.set()
        await release.wait()
        active_count -= 1
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register(
        "same",
        "Same tool.",
        {"type": "object"},
        same_handler,
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Run tools", session_id="session-one")

    assert max_active_count == 2


@pytest.mark.asyncio
async def test_tool_handler_exception_continues_with_failure_envelope(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["explode"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "explode", "arguments": {}}],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )

    def failing_handler(_context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        raise RuntimeError("boom")

    tools = ToolRegistry()
    tools.register("explode", "Explode.", {"type": "object"}, failing_handler)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Run tool", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert assistant.content == "Recovered"
    assert run.status == RunStatus.COMPLETED
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == tool_failure("tool_execution_error", "boom")
    result_payload = next(
        event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ).payload
    assert result_payload["tool_call"] == {"id": "call_1", "index": 0, "name": "explode"}
    assert result_payload["result"] == tool_failure("tool_execution_error", "boom")
    assert result_payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_invalid_arguments_can_be_corrected_without_running_handler_twice(
    tmp_path: Path,
) -> None:
    invocations: list[ToolJsonObject] = []

    def handler(_context: ToolContext, arguments: ToolJsonObject) -> ToolJsonObject:
        invocations.append(arguments)
        return tool_success({"city": arguments["city"]})

    tools = ToolRegistry()
    tools.register(
        "weather",
        "Read weather for one city.",
        {
            "type": "object",
            "properties": {"city": {"type": "string", "minLength": 1}},
            "required": ["city"],
            "additionalProperties": False,
        },
        handler,
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "weather", "arguments": {"city": 7}}],
            },
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_2", "name": "weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send(
        "coder",
        "Check the weather",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    tool_results = [
        json.loads(cast(str, message.content)) for message in messages if message.role == "tool"
    ]
    assert assistant.content == "Recovered"
    assert invocations == [{"city": "Berlin"}]
    assert tool_results[0]["error"]["code"] == "invalid_arguments"
    assert "arguments/city" in tool_results[0]["error"]["message"]
    assert tool_results[1] == tool_success({"city": "Berlin"})
