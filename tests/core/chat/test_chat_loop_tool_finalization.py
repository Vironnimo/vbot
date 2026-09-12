"""Tests for chat loop tool finalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat._step_outcomes import (
    MAX_IDENTICAL_FAILED_TOOL_CALLS,
    TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    TOOL_ITERATION_LIMIT_FAILURE_CODE,
    _FailedToolCallCircuitBreaker,
)
from core.chat.messages import ToolCall, ToolCallRejection
from core.runs import (
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
    persisted_roles,
    session_address,
)
from tests.core.chat.chat_loop_tools_test_support import (
    JsonObject,
)


@pytest.mark.asyncio
async def test_max_tool_iteration_limit_returns_failure_then_finalizes_without_tools(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {
                "content": "I cannot call more Tools, so this is my final answer.",
                "tool_calls": None,
            },
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

    result = await build_chat_loop(runtime, max_tool_iterations=0).send(
        "coder",
        "Weather?",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert result.content == "I cannot call more Tools, so this is my final answer."
    assert persisted_roles(messages) == ["user", "assistant", "tool", "note", "assistant"]
    assert isinstance(messages[2].content, str)
    assert json.loads(messages[2].content)["error"]["code"] == TOOL_ITERATION_LIMIT_FAILURE_CODE
    assert adapter.requests[-1]["kwargs"]["tools"] == []


@pytest.mark.asyncio
async def test_tool_call_during_no_tool_finalization_is_rejected_and_run_completes(
    tmp_path: Path,
) -> None:
    invocation_count = 0

    def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        nonlocal invocation_count
        invocation_count += 1
        return tool_success({})

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_first", "name": "get_weather", "arguments": {}}],
            },
            {
                "content": None,
                "tool_calls": [{"id": "call_ignored", "name": "get_weather", "arguments": {}}],
            },
            {
                "content": "I received the disabled-Tool Result and will answer without Tools.",
                "tool_calls": None,
            },
        ]
    )
    tools = ToolRegistry()
    tools.register("get_weather", "Get weather.", {"type": "object"}, handler)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    result = await build_chat_loop(runtime, max_tool_iterations=0).send(
        "coder",
        "Weather?",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert result.content == "I received the disabled-Tool Result and will answer without Tools."
    assert invocation_count == 0
    assert len(adapter.requests) == 3
    assert all(request["kwargs"]["tools"] == [] for request in adapter.requests[1:])
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "tool",
        "note",
        "assistant",
        "tool",
        "assistant",
    ]
    final_tool_message = next(message for message in reversed(messages) if message.role == "tool")
    assert isinstance(final_tool_message.content, str)
    assert json.loads(final_tool_message.content)["error"]["code"] == (
        TOOL_FINALIZATION_DISABLED_FAILURE_CODE
    )
    run = next(iter(runtime.chat_runs._runs.values()))
    assert run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_repeated_no_tool_finalization_violations_complete_without_unbounded_loop(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_limit", "name": "get_weather", "arguments": {}}],
            },
            {
                "content": None,
                "tool_calls": [{"id": "call_violation_1", "name": "get_weather", "arguments": {}}],
            },
            {
                "content": None,
                "tool_calls": [{"id": "call_violation_2", "name": "get_weather", "arguments": {}}],
            },
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

    result = await build_chat_loop(runtime, max_tool_iterations=0).send(
        "coder",
        "Weather?",
        session_id="session-one",
    )

    assert result.tool_calls is not None
    assert result.tool_calls[0].id == "call_violation_2"
    assert len(adapter.requests) == 3
    assert all(request["kwargs"]["tools"] == [] for request in adapter.requests[1:])
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    disabled_results = [
        json.loads(message.content)
        for message in messages
        if message.role == "tool" and isinstance(message.content, str)
    ][1:]
    assert [tool_result["error"]["code"] for tool_result in disabled_results] == [
        TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
        TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    ]
    run = next(iter(runtime.chat_runs._runs.values()))
    assert run.status == RunStatus.COMPLETED


def test_failed_tool_call_circuit_breaker_resets_on_change_and_success() -> None:
    breaker = _FailedToolCallCircuitBreaker()
    berlin = ToolCall(id="call-berlin", name="weather", arguments={"city": "Berlin"})
    paris = ToolCall(id="call-paris", name="weather", arguments={"city": "Paris"})

    def result_message(tool_call: ToolCall, *, ok: bool) -> ChatMessage:
        result = tool_success({}) if ok else tool_failure("weather_error", "unavailable")
        return ChatMessage.tool(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=json.dumps(result),
        )

    for _ in range(MAX_IDENTICAL_FAILED_TOOL_CALLS - 1):
        assert breaker.observe([berlin], [result_message(berlin, ok=False)]) is None

    assert breaker.observe([paris], [result_message(paris, ok=False)]) is None
    assert breaker.observe([paris], [result_message(paris, ok=True)]) is None

    for _ in range(MAX_IDENTICAL_FAILED_TOOL_CALLS - 1):
        assert breaker.observe([paris], [result_message(paris, ok=False)]) is None
    assert breaker.observe([paris], [result_message(paris, ok=False)]) == "weather"


def test_failed_tool_call_circuit_breaker_keys_error_class_and_schema_version() -> None:
    breaker = _FailedToolCallCircuitBreaker(limit=2)
    call = ToolCall(id="call", name="weather", arguments={"city": "Berlin"})

    class Registry:
        fingerprint = "schema-v1"

        def schema_fingerprint(self, name: str) -> str:
            assert name == "weather"
            return self.fingerprint

    registry = Registry()

    def failed(code: str) -> ChatMessage:
        return ChatMessage.tool(
            tool_call_id=call.id,
            name=call.name,
            content=json.dumps(tool_failure(code, "failed")),
        )

    assert breaker.observe([call], [failed("timeout")], registry) is None
    assert breaker.observe([call], [failed("permission_denied")], registry) is None
    registry.fingerprint = "schema-v2"
    assert breaker.observe([call], [failed("permission_denied")], registry) is None
    assert breaker.observe([call], [failed("permission_denied")], registry) == "weather"


def test_failed_tool_call_circuit_breaker_keys_rejection_fingerprint() -> None:
    breaker = _FailedToolCallCircuitBreaker(limit=2)

    def rejected(fingerprint: str) -> ToolCall:
        return ToolCall(
            id=f"call-{fingerprint}",
            name="write",
            arguments={},
            rejection=ToolCallRejection(
                code="malformed_tool_arguments",
                message="Arguments were malformed.",
                fingerprint=fingerprint,
            ),
        )

    def failed(call: ToolCall) -> ChatMessage:
        return ChatMessage.tool(
            tool_call_id=call.id,
            name=call.name,
            content=json.dumps(tool_failure("malformed_tool_arguments", "rejected")),
        )

    first = rejected("raw-a")
    second = rejected("raw-b")
    assert breaker.observe([first], [failed(first)]) is None
    assert breaker.observe([second], [failed(second)]) is None
    assert breaker.observe([second], [failed(second)]) == "write"


@pytest.mark.asyncio
async def test_identical_failed_tool_call_finalizes_without_tools_after_eighth_call(
    tmp_path: Path,
) -> None:
    invocation_count = 0

    def failing_handler(
        _context: ToolContext,
        _arguments: ToolJsonObject,
    ) -> JsonObject:
        nonlocal invocation_count
        invocation_count += 1
        return tool_failure("invalid_arguments", "name is required")

    repeated_call = {
        "name": "skill_manage",
        "arguments": {"action": "create", "name": "demo"},
    }
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": f"call_{index}", **repeated_call}],
            }
            for index in range(MAX_IDENTICAL_FAILED_TOOL_CALLS)
        ]
        + [{"content": "The Tool remains blocked; I cannot complete it.", "tool_calls": None}]
    )
    tools = ToolRegistry()
    tools.register(
        "skill_manage",
        "Manage Skills.",
        {"type": "object"},
        failing_handler,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(
            id="coder",
            model="openai/gpt-5.2",
            allowed_tools=["skill_manage"],
        ),
        adapter=adapter,
        tools=tools,
    )

    result = await build_chat_loop(runtime).send(
        "coder",
        "Create a Skill",
        session_id="session-one",
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert result.content == "The Tool remains blocked; I cannot complete it."
    assert invocation_count == MAX_IDENTICAL_FAILED_TOOL_CALLS
    assert len(adapter.requests) == MAX_IDENTICAL_FAILED_TOOL_CALLS + 1
    assert adapter.requests[-1]["kwargs"]["tools"] == []
    assert persisted_roles(messages) == [
        "user",
        *(["assistant", "tool"] * MAX_IDENTICAL_FAILED_TOOL_CALLS),
        "note",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_tool_iteration_limit_is_scoped_to_current_run(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_2", "name": "get_weather", "arguments": {"city": "Paris"}}
                ],
            },
            {"content": "First run done", "tool_calls": None},
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_3", "name": "get_weather", "arguments": {"city": "Rome"}}
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_4", "name": "get_weather", "arguments": {"city": "Madrid"}}
                ],
            },
            {"content": "Second run done", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"ok": True}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    chat_loop = build_chat_loop(runtime, max_tool_iterations=2)

    first = await chat_loop.send("coder", "Weather batch one", session_id="session-one")
    second = await chat_loop.send("coder", "Weather batch two", session_id="session-one")

    assert first.content == "First run done"
    assert second.content == "Second run done"

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert all(message.role != "error" for message in messages)
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
