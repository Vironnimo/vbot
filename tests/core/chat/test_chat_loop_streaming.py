"""Chat-loop tests grouped by streaming."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatLoop,
)
from core.chat.continuation import (
    recover_continuation,
)
from core.chat.streaming import StreamingDeltaError
from core.providers.errors import (
    NetworkError,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    RunStatus,
)
from core.tools import (
    ToolDisplay,
    ToolRegistry,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    persisted_dict_roles,
    persisted_roles,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_streaming_mode_emits_deltas_then_final_authoritative_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Think"},
                {"type": "content_delta", "text": "Hello"},
                {"type": "content_delta", "text": " world"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert assistant.content == "Hello world"
    assert assistant.reasoning == "Think"
    assert persisted_roles(messages) == ["user", "assistant"]
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        REASONING_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "reasoning",
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_completed",
    ]
    assert run.events[2].payload == {"reasoning_delta": "Think"}
    assert run.events[3].payload == {"content_delta": "Hello"}
    assert run.events[6].payload["message"]["content"] == "Hello world"
    assert "reasoning_meta" not in run.events[6].payload["message"]
    assert adapter.requests == []
    assert adapter.stream_requests[0]["kwargs"]["thinking_effort"] == "high"


@pytest.mark.asyncio
async def test_streaming_mode_persists_only_final_messages_and_continues_tool_loop(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="anthropic/claude-sonnet-4", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Need weather."},
                {"type": "reasoning_meta", "reasoning_meta": {"signature": "opaque"}},
                {
                    "type": "tool_call_delta",
                    "id": "call_abc",
                    "name_delta": "get_weather",
                    "arguments_delta": '{"city":"Ber',
                },
                {
                    "type": "tool_call_delta",
                    "id": "call_abc",
                    "arguments_delta": 'lin"}',
                },
                {"type": "finish", "reason": "tool_calls"},
            ],
            [
                {"type": "content_delta", "text": "Sunny"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
        display=ToolDisplay(summary_fields=("city",)),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder",
        "Weather?",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    persisted = [
        message.to_dict() for message in runtime.chat_sessions.get("coder", "session-one").load()
    ]
    assert assistant.content == "Sunny"
    assert persisted_dict_roles(persisted) == ["user", "assistant", "tool", "assistant"]
    assert persisted[1]["reasoning_meta"] == {"signature": "opaque"}
    assert persisted[1]["tool_calls"] == [
        {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]
    assert json.loads(persisted[2]["content"]) == tool_success({"temp": 22, "city": "Berlin"})
    assert adapter.stream_requests[1]["messages"][2]["reasoning_meta"] == {"signature": "opaque"}
    assert [
        event.type
        for event in run.events
        if event.type in {TOOL_CALL_DELTA_EVENT, TOOL_CALL_STARTED_EVENT}
    ] == [
        TOOL_CALL_DELTA_EVENT,
        TOOL_CALL_DELTA_EVENT,
        TOOL_CALL_STARTED_EVENT,
    ]
    tool_started = next(event for event in run.events if event.type == TOOL_CALL_STARTED_EVENT)
    assert tool_started.payload["tool_call"]["arguments"] == {"city": "Berlin"}
    assert tool_started.payload["display"] == {
        "summary": "Berlin",
        "hidden_argument_keys": [],
    }
    assert tool_started.payload["tool_call"] == {
        "id": "call_abc",
        "index": 0,
        "name": "get_weather",
        "arguments": {"city": "Berlin"},
    }
    tool_result = next(event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT)
    assert tool_result.payload["tool_call"] == {
        "id": "call_abc",
        "index": 0,
        "name": "get_weather",
    }
    assert tool_result.payload["result"] == tool_success({"temp": 22, "city": "Berlin"})
    assert tool_result.payload["timing"]["duration_ms"] >= 0
    assert all(
        "reasoning_meta" not in event.payload.get("message", {})
        for event in run.events
        if isinstance(event.payload, dict)
    )


@pytest.mark.asyncio
async def test_streaming_mode_malformed_tool_arguments_persist_provider_error(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Need to write the file."},
                {
                    "type": "tool_call_delta",
                    "id": "call_write",
                    "name_delta": "write",
                    "arguments_delta": '{"path":"todo.html","content":"<html>',
                },
                {"type": "finish", "reason": "tool_calls"},
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    loop = ChatLoop(runtime, streaming=True)
    with pytest.raises(StreamingDeltaError, match="malformed or incomplete arguments"):
        await loop.send("coder", "Build it", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()

    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    continuation = loop.continuation_summary("coder", "session-one")
    assert continuation is not None
    assert continuation["cause"] == "internal"
    state = recover_continuation(runtime.chat_sessions.get("coder", "session-one"))
    assert state is not None
    assert state.reasoning == "Need to write the file."
    assert messages[1].error_kind == "provider_error"
    assert "malformed or incomplete arguments" in (messages[1].content or "")
    assert [event.type for event in run.events][-2:] == [
        ERROR_MESSAGE_PERSISTED_EVENT,
        "run_failed",
    ]


@pytest.mark.asyncio
async def test_streaming_mode_missing_finish_delta_preserves_visible_partial(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Partial answer"},
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()

    # A stream that ends without a finish delta but already streamed visible
    # content is an interrupted turn: the partial answer is preserved, not lost.
    assert assistant.content == "Partial answer"
    assert assistant.interrupted is True
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].interrupted is True
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_completed",
    ]


@pytest.mark.asyncio
async def test_streaming_transport_error_after_finish_keeps_completed_answer(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Complete answer"},
                {"type": "finish", "reason": "stop"},
                NetworkError("missing transport terminator"),
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    persisted_assistant = next(message for message in messages if message.role == "assistant")

    assert assistant.content == "Complete answer"
    assert assistant.interrupted is False
    assert persisted_assistant.interrupted is False
    assert run.status == RunStatus.COMPLETED
    assert not any(message.role == "error" for message in messages)
    assert len(adapter.stream_requests) == 1


@pytest.mark.asyncio
async def test_streaming_tool_finish_survives_late_transport_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {
                    "type": "tool_call_delta",
                    "id": "call_abc",
                    "name_delta": "get_weather",
                    "arguments_delta": '{"city":"Berlin"}',
                },
                {"type": "finish", "reason": "tool_calls"},
                NetworkError("missing transport terminator"),
            ],
            [
                {"type": "content_delta", "text": "Sunny"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Weather?", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    persisted = runtime.chat_sessions.get("coder", "session-one").load()

    assert assistant.content == "Sunny"
    assert persisted_roles(persisted) == ["user", "assistant", "tool", "assistant"]
    assert run.status == RunStatus.COMPLETED
    assert len(adapter.stream_requests) == 2
