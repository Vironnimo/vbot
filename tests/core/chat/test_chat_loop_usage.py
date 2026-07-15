"""Chat-loop tests grouped by usage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatLoop,
)
from core.tools import (
    ToolRegistry,
    tool_success,
)
from core.utils.tokens import estimate_message_tokens
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_non_streaming_response_with_usage_produces_assistant_with_usage(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": None,
                "tool_calls": None,
                "usage": {"input_tokens": 150, "output_tokens": 12},
            }
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.usage == {"input_tokens": 150, "output_tokens": 12}
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage == {"input_tokens": 150, "output_tokens": 12}
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload["status"] == "completed"
    assert completed[0].payload["usage"] == {"input_tokens": 150, "output_tokens": 12}
    assert completed[0].payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_run_completed_payload_carries_whole_session_usage_totals(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": None,
                "tool_calls": None,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 40,
                    "cache_read_tokens": 700,
                    "cache_write_tokens": 200,
                },
            }
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload["session_usage"] == {
        "measured_turns": 1,
        "estimated_turns": 0,
        "cache_turns": 1,
        "input_tokens": 1000,
        "output_tokens": 40,
        "cache_read_tokens": 700,
        "cache_write_tokens": 200,
    }


@pytest.mark.asyncio
async def test_streaming_response_with_usage_delta_produces_assistant_with_usage(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "usage", "input_tokens": 200, "output_tokens": 25},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    assert assistant.content == "Hello"
    assert assistant.usage == {"input_tokens": 200, "output_tokens": 25}
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage == {"input_tokens": 200, "output_tokens": 25}
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload["status"] == "completed"
    assert completed[0].payload["usage"] == {"input_tokens": 200, "output_tokens": 25}
    assert completed[0].payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_response_without_usage_applies_estimation(
    tmp_path: Path,
) -> None:
    """When the provider doesn't supply usage, the chat loop estimates tokens."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello world", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.usage is not None
    assert assistant.usage["estimated"] is True
    assert assistant.usage == {
        "input_tokens": assistant.usage["input_tokens"],
        "output_tokens": assistant.usage["output_tokens"],
        "estimated": True,
    }
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage is not None
    assert persisted[1].usage["estimated"] is True
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload["usage"]["estimated"] is True


@pytest.mark.asyncio
async def test_estimation_computes_from_request_message_contents(
    tmp_path: Path,
) -> None:
    """Estimation derives token counts from structured request and response messages."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello world", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    # Reconstruct expected estimation from the actual request messages
    request_messages = adapter.requests[0]["messages"]
    expected_input = sum(estimate_message_tokens(message)[0] for message in request_messages)
    expected_output, _ = estimate_message_tokens({"role": "assistant", "content": "Hello world"})

    assert assistant.usage == {
        "input_tokens": expected_input,
        "output_tokens": expected_output,
        "estimated": True,
    }


@pytest.mark.asyncio
async def test_provider_usage_preserved_without_estimated_flag(
    tmp_path: Path,
) -> None:
    """When the provider supplies usage, it is kept as-is with no estimated flag."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": None,
                "tool_calls": None,
                "usage": {"input_tokens": 150, "output_tokens": 12},
            }
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.usage == {"input_tokens": 150, "output_tokens": 12}
    assert "estimated" not in assistant.usage
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage == {"input_tokens": 150, "output_tokens": 12}
    assert "estimated" not in persisted[1].usage


@pytest.mark.asyncio
async def test_streaming_without_usage_applies_estimation(
    tmp_path: Path,
) -> None:
    """Streaming mode also applies estimation when no usage delta is received."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    assert assistant.content == "Hello"
    assert assistant.usage is not None
    assert assistant.usage["estimated"] is True
    assert isinstance(assistant.usage["input_tokens"], int)
    assert isinstance(assistant.usage["output_tokens"], int)
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload["usage"]["estimated"] is True


@pytest.mark.asyncio
async def test_estimation_with_tool_calls_in_history(
    tmp_path: Path,
) -> None:
    """Estimation includes tool call content from previous turns in input tokens."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Sunny", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime).send("coder", "Weather?", session_id="session-one")

    assert assistant.content == "Sunny"
    assert assistant.usage is not None
    assert assistant.usage["estimated"] is True
    # The second request includes previous assistant + tool messages, so
    # input_tokens should be larger than the first request alone.
    assert assistant.usage["input_tokens"] > 0
