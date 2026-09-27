"""Streaming Model steps that complete: deltas, Run events, persisted turns and Tool loops."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.chat.continuation import (
    recover_continuation,
)
from core.chat.request_runner import _StreamingRunDeltaEmitter
from core.chat.streaming import StreamingVisibleDelta
from core.providers.errors import (
    NetworkError,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    MODEL_STEP_USAGE_EVENT,
    PROVIDER_HEARTBEAT_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    Run,
    RunStatus,
)
from core.tools import (
    ToolDisplay,
    ToolRegistry,
    tool_success,
)
from tests.core.chat.chat_loop_streaming_test_support import history, last_run, stream_runtime
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_dict_roles,
    persisted_roles,
    session_address,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_stream_delta_emitter_flushes_a_quiet_pending_fragment() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    emitter = _StreamingRunDeltaEmitter(run)

    emitter.add(
        StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": "first"},
        )
    )
    emitter.add(
        StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": " second"},
        )
    )
    await asyncio.sleep(0.06)
    emitter.close()

    assert [event.payload for event in run.events] == [
        {"content_delta": "first"},
        {"content_delta": " second"},
    ]


@pytest.mark.asyncio
async def test_streaming_mode_emits_deltas_then_final_authoritative_message(
    tmp_path: Path,
) -> None:
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
    runtime = stream_runtime(tmp_path, adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = last_run(runtime)
    messages = history(runtime)
    assert assistant.content == "Hello world"
    assert assistant.reasoning == "Think"
    assert persisted_roles(messages) == ["user", "assistant"]
    assert [event.type for event in run.events if event.type != "provider_request_status"] == [
        "run_started",
        "user_message_persisted",
        REASONING_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "reasoning",
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_completed",
    ]
    assert next(event for event in run.events if event.type == "reasoning_delta").payload == {
        "reasoning_delta": "Think"
    }
    assert next(
        event for event in run.events if event.type == "assistant_output_delta"
    ).payload == {"content_delta": "Hello world"}
    assert (
        next(event for event in run.events if event.type == "assistant_output").payload["message"][
            "content"
        ]
        == "Hello world"
    )
    assert (
        "reasoning_meta"
        not in next(event for event in run.events if event.type == "assistant_output").payload[
            "message"
        ]
    )
    assert (
        "reasoning_scope"
        not in next(event for event in run.events if event.type == "assistant_output").payload[
            "message"
        ]
    )
    # Reasoning followed by a visible answer is complete: no recovery request follows.
    assert adapter.requests == []
    assert len(adapter.stream_requests) == 1
    assert adapter.stream_requests[0]["kwargs"]["thinking_effort"] == "high"


@pytest.mark.asyncio
async def test_streaming_mode_emits_provider_heartbeat_without_model_output(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Writing the file now."},
                {"type": "heartbeat"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime = stream_runtime(tmp_path, adapter)

    await build_chat_loop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = last_run(runtime)
    heartbeat = next(event for event in run.events if event.type == PROVIDER_HEARTBEAT_EVENT)
    assert heartbeat.payload["state"] == "waiting_for_model_delta"
    assert heartbeat.payload["idle_seconds"] >= 0
    assert persisted_roles(history(runtime)) == [
        "user",
        "assistant",
    ]


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

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder",
        "Weather?",
        session_id="session-one",
    )

    run = last_run(runtime)
    persisted = [message.to_dict() for message in history(runtime)]
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
        TOOL_CALL_STARTED_EVENT,
    ]
    tool_delta = next(event for event in run.events if event.type == TOOL_CALL_DELTA_EVENT)
    assert tool_delta.payload == {
        "tool_call_id": "call_abc",
        "name_delta": "get_weather",
        "arguments_delta": '{"city":"Berlin"}',
    }
    tool_started = next(event for event in run.events if event.type == TOOL_CALL_STARTED_EVENT)
    assert tool_started.payload["tool_call"]["arguments"] == {"city": "Berlin"}
    assert tool_started.payload["display"] == {
        "version": 1,
        "summary": "Berlin",
        "hidden_argument_keys": [],
        "primary": [
            {
                "kind": "text",
                "value": "Berlin",
                "full_value": "Berlin",
                "truncate": "end",
                "tooltip": "truncated",
                "max_characters": 64,
                "quote": False,
                "copyable": False,
            }
        ],
        "facts": [],
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
        and "reasoning_scope" not in event.payload.get("message", {})
        for event in run.events
        if isinstance(event.payload, dict)
    )


@pytest.mark.asyncio
async def test_streaming_mode_malformed_tool_arguments_return_tool_failure_and_continue(
    tmp_path: Path,
) -> None:
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
            ],
            [
                {"type": "content_delta", "text": "The Tool Call was malformed, so I stopped."},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime = stream_runtime(tmp_path, adapter)

    loop = build_chat_loop(runtime, streaming=True)
    result = await loop.send("coder", "Build it", session_id="session-one")

    run = last_run(runtime)
    messages = history(runtime)

    assert result.content == "The Tool Call was malformed, so I stopped."
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls is not None
    assert messages[1].tool_calls[0].arguments == {}
    assert messages[1].tool_calls[0].rejection is not None
    assert messages[1].tool_calls[0].rejection.code == "malformed_tool_arguments"
    assert isinstance(messages[2].content, str)
    failure = json.loads(messages[2].content)
    assert failure["error"]["code"] == "malformed_tool_arguments"
    assert failure["error"]["retryable"] is False
    assert not (tmp_path / "todo.html").exists()
    assert (
        await recover_continuation(
            runtime.chat_sessions.get(session_address("coder", "session-one"))
        )
        is None
    )
    assert [event.type for event in run.events][-1] == "run_completed"


@pytest.mark.asyncio
async def test_transport_error_after_a_finish_delta_keeps_the_completed_step(
    tmp_path: Path,
) -> None:
    late_error = NetworkError("missing transport terminator")
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
                late_error,
            ],
            [
                {"type": "content_delta", "text": "Sunny"},
                {"type": "finish", "reason": "stop"},
                late_error,
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
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Weather?", session_id="session-one"
    )

    messages = history(runtime)
    assert (assistant.content, assistant.interrupted) == ("Sunny", False)
    assert persisted_roles(messages) == ["user", "assistant", "tool", "assistant"]
    assert not any(message.interrupted for message in messages if message.role == "assistant")
    assert last_run(runtime).status == RunStatus.COMPLETED
    assert len(adapter.stream_requests) == 2
