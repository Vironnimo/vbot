"""Chat-loop tests grouped by streaming."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat.continuation import (
    recover_continuation,
)
from core.chat.streaming import StreamingChunkTimeoutError
from core.providers.errors import (
    NetworkError,
    ProviderStreamingUnsupportedError,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    MODEL_STEP_USAGE_EVENT,
    RunCancelledError,
    RunStatus,
)
from core.tools import (
    ToolRegistry,
    tool_success,
)
from core.utils.errors import ProviderError
from tests.core.chat.chat_loop_support import (
    BlockingReasoningStreamingStubAdapter,
    BlockingStreamingStubAdapter,
    MidStreamCancelledStubAdapter,
    SlowStreamingStubAdapter,
    StalledStreamingStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_streaming_mode_falls_back_before_usable_streamed_output(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"content": "Fallback answer", "tool_calls": None}],
        stream_responses=[ProviderStreamingUnsupportedError("streaming is not supported")],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    assert assistant.content == "Fallback answer"
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_completed",
    ]
    assert len(adapter.stream_requests) == 1
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_streaming_mode_does_not_fallback_on_generic_provider_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"content": "Should not use", "tool_calls": None}],
        stream_responses=[ProviderError("provider failed", retryable=False)],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderError, match="provider failed"):
        await build_chat_loop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert messages[1].error_kind == "provider_fatal"
    # No non-streaming fallback request was issued for a generic provider error.
    assert len(adapter.stream_requests) == 1
    assert len(adapter.requests) == 0


@pytest.mark.asyncio
async def test_streaming_mode_preserves_partial_instead_of_fallback_after_visible_delta(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"content": "Should not use", "tool_calls": None}],
        stream_responses=[
            [
                {"type": "content_delta", "text": "partial"},
                ProviderStreamingUnsupportedError("streaming is not supported"),
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # Once visible output escaped, the break preserves the partial answer rather
    # than silently re-issuing the request as a non-streaming call.
    assert assistant.content == "partial"
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
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_streaming_mode_chunk_timeout_preserves_partial_after_visible_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.chat.chat.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StalledStreamingStubAdapter([])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # A remote provider that stalls after streaming visible content has its
    # partial answer preserved as an interrupted turn (no timeout failure).
    assert assistant.content == "partial"
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
async def test_streaming_mode_cancellation_closes_adapter_and_preserves_visible_partial(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert adapter.closed is True
    assert run.status == RunStatus.CANCELLED
    # The already-shown partial answer is preserved as an interrupted turn
    # (GLOSSARY → Cancel); the never-released late delta stays suppressed.
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].content == "before"
    assert messages[1].interrupted is True
    summaries = [message for message in messages if message.role == "run_summary"]
    assert summaries[-1].status == "cancelled"
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_cancelled",
    ]


@pytest.mark.asyncio
async def test_streaming_cancellation_with_reasoning_retains_continuation(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = MidStreamCancelledStubAdapter([])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    loop = build_chat_loop(runtime, streaming=True)
    with pytest.raises(RunCancelledError):
        await loop.send("coder", "Hi", session_id="session-one")

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["user"]
    state = recover_continuation(runtime.chat_sessions.get("coder", "session-one"))
    assert state is not None
    assert state.reasoning == "Need network."
    assert state.cause == "internal"


@pytest.mark.asyncio
async def test_streaming_network_error_with_reasoning_retains_continuation(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Need network."},
                NetworkError("offline"),
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    loop = build_chat_loop(runtime, streaming=True)
    with pytest.raises(NetworkError, match="offline"):
        await loop.send("coder", "Hi", session_id="session-one")

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted_roles(messages) == ["user", "error"]
    state = recover_continuation(runtime.chat_sessions.get("coder", "session-one"))
    assert state is not None
    assert state.reasoning == "Need network."
    assert state.cause == "network"
    assert messages[1].error_kind == "network_error"


@pytest.mark.asyncio
async def test_streaming_network_error_after_visible_content_preserves_partial(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "partial"},
                NetworkError("offline"),
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # Visible content present at the drop → preserved as an interrupted turn,
    # not discarded; the Continuation Checkpoint retains the readable state.
    assert assistant.content == "partial"
    assert assistant.interrupted is True
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].interrupted is True


@pytest.mark.asyncio
async def test_streaming_mode_restarts_after_transient_drop_before_visible_output(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            # First attempt receives bytes (non-visible reasoning_meta) then drops.
            [
                {"type": "reasoning_meta", "reasoning_meta": {"sig": "x"}},
                NetworkError("dropped after first byte"),
            ],
            # Restart re-issues the whole request and completes cleanly.
            [
                {"type": "content_delta", "text": "Recovered"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert assistant.content == "Recovered"
    assert len(adapter.stream_requests) == 2
    assert run.status == RunStatus.COMPLETED
    # The discarded attempt leaves no error or durable readable state.
    assert persisted_roles(messages) == ["user", "assistant"]


@pytest.mark.asyncio
async def test_streaming_mode_does_not_restart_after_visible_delta(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Visible"},
                NetworkError("dropped mid-stream"),
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # A drop after visible output is not replayed — exactly one stream attempt —
    # and the visible answer is preserved instead of failing the run.
    assert len(adapter.stream_requests) == 1
    assert assistant.content == "Visible"
    assert assistant.interrupted is True
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].interrupted is True


@pytest.mark.asyncio
async def test_streaming_mode_restart_exhaustion_persists_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            NetworkError("drop 1"),
            NetworkError("drop 2"),
            NetworkError("drop 3"),
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(NetworkError, match="drop 3"):
        await build_chat_loop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # Initial attempt plus MAX_STREAM_RESTARTS replays, then the error surfaces.
    assert len(adapter.stream_requests) == 3
    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert messages[1].error_kind == "network_error"


@pytest.mark.asyncio
async def test_streaming_mode_restarts_after_chunk_stall_before_visible_output(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            # First attempt receives non-visible bytes then the provider goes
            # silent — surfaced as a chunk-stall timeout, not a ProviderError.
            [
                {"type": "reasoning_meta", "reasoning_meta": {"sig": "x"}},
                StreamingChunkTimeoutError("provider stream stalled"),
            ],
            # Restart re-issues the whole request and completes cleanly.
            [
                {"type": "content_delta", "text": "Recovered"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert assistant.content == "Recovered"
    assert len(adapter.stream_requests) == 2
    assert run.status == RunStatus.COMPLETED
    # The discarded attempt leaves no error or durable readable state.
    assert persisted_roles(messages) == ["user", "assistant"]


@pytest.mark.asyncio
async def test_streaming_mode_does_not_restart_after_chunk_stall_with_visible_output(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Visible"},
                StreamingChunkTimeoutError("provider stream stalled"),
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # A stall after visible output is not replayed — exactly one stream attempt —
    # and the visible answer is preserved as an interrupted turn.
    assert len(adapter.stream_requests) == 1
    assert assistant.content == "Visible"
    assert assistant.interrupted is True
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].interrupted is True


@pytest.mark.asyncio
async def test_streaming_interrupted_partial_discards_in_flight_tool_call(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Let me check"},
                # A tool call started streaming but the connection dropped before
                # the arguments completed — it was never executed.
                {
                    "type": "tool_call_delta",
                    "id": "call_abc",
                    "name_delta": "get_weather",
                    "arguments_delta": '{"city":"Ber',
                },
                NetworkError("dropped mid tool-call"),
            ]
        ],
    )
    executed: list[str] = []
    tools = ToolRegistry()

    def _run_weather(_context: Any, _arguments: Any) -> JsonObject:
        executed.append("ran")
        return tool_success({"ok": True})

    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        _run_weather,
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Weather?", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # The half-streamed tool call leaves no tool_calls on the preserved turn and
    # never runs (side-effect-free), so no tool result is persisted.
    assert assistant.content == "Let me check"
    assert assistant.interrupted is True
    assert assistant.tool_calls is None
    assert executed == []
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].tool_calls is None


@pytest.mark.asyncio
async def test_interrupted_turn_partial_text_replays_into_next_request(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Continued answer", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Long question"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="The first half of the answer",
            interrupted=True,
        )
    )

    await build_chat_loop(runtime).send("coder", "continue", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assistant_entries = [m for m in request_messages if m["role"] == "assistant"]
    # The truncated turn is in the request history so the model can continue it —
    # but the internal interrupted flag never reaches the provider.
    assert any(m["content"] == "The first half of the answer" for m in assistant_entries)
    assert all("interrupted" not in m for m in request_messages)


@pytest.mark.asyncio
async def test_local_provider_stream_not_aborted_by_chunk_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.chat.chat.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = SlowStreamingStubAdapter(delay=0.05)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        provider_base_url="http://localhost:11434/v1",
    )

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # The local provider's silence exceeds the chunk timeout but is not aborted:
    # the stream completes normally instead of being cut off mid-stream.
    assert assistant.content == "partial done"
    assert assistant.interrupted is False
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant"]


@pytest.mark.asyncio
async def test_remote_provider_stream_aborted_by_chunk_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.chat.chat.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = SlowStreamingStubAdapter(delay=0.05)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        provider_base_url="https://api.openai.com/v1",
    )

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    # A remote provider keeps the stall guard: the silence trips a chunk stall
    # after visible output, so only the pre-stall content is preserved.
    assert assistant.content == "partial"
    assert assistant.interrupted is True
    assert run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_user_cancel_after_visible_stream_preserves_partial_and_stays_cancelled(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    # A user cancel mid visible stream still ends as cancelled — never
    # reclassified as a transient error or a completed run — but the answer the
    # user already saw is preserved as an interrupted assistant turn.
    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].content == "before"
    assert messages[1].interrupted is True
    assert not any(message.error_kind for message in messages if message.role == "error")


@pytest.mark.asyncio
async def test_user_cancel_without_visible_output_retains_checkpoint_without_continue(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingReasoningStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.stream_started.wait()
    run.request_cancel(reason="user")
    await asyncio.sleep(0)

    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    # Reasoning-only cancel: no assistant text or recovery note is persisted in
    # canonical history; the durable checkpoint owns the readable working state.
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user"]
    state = recover_continuation(runtime.chat_sessions.get("coder", "session-one"))
    assert state is not None
    assert state.reasoning == "Thinking hard."
    summary = state.public_summary()
    assert summary is not None
    assert summary["can_continue"] is False
    summaries = [message for message in messages if message.role == "run_summary"]
    assert summaries[-1].status == "cancelled"
