"""Tests for chat loop stream recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat.streaming import StreamingChunkTimeoutError
from core.providers.errors import (
    ProviderStreamingUnsupportedError,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    MODEL_STEP_USAGE_EVENT,
    RunCancelledError,
    RunInterruptedError,
    RunStatus,
)
from core.utils.errors import ProviderError
from tests.core.chat.chat_loop_support import (
    BlockingStreamingStubAdapter,
    SlowStreamingStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
    session_address,
)


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
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
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
        [{"content": "Continued answer", "tool_calls": None}],
        stream_responses=[
            [
                {"type": "content_delta", "text": "partial"},
                ProviderStreamingUnsupportedError("streaming is not supported"),
            ],
            ProviderStreamingUnsupportedError("streaming is not supported"),
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    # Once visible output escaped, the break preserves the partial answer rather
    # than silently re-issuing the request as a non-streaming call.
    assert assistant.content == "Continued answer"
    assert assistant.interrupted is False
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert messages[1].interrupted is True
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_completed",
    ]
    assert len(adapter.stream_requests) == 2
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_reasoning_only_stop_recovers_with_visible_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_sentinel = "test-owned output-integrity recovery"
    monkeypatch.setattr(
        "core.chat.request_runner.OUTPUT_INTEGRITY_RECOVERY_NOTE",
        recovery_sentinel,
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Answer text routed as reasoning."},
                {"type": "finish", "reason": "stop"},
            ],
            [
                {"type": "content_delta", "text": "Recovered visible answer."},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert assistant.content == "Recovered visible answer."
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert messages[1].reasoning == "Answer text routed as reasoning."
    assert messages[1].interrupted is True
    assert messages[1].interruption_cause == "provider"
    assert messages[2].content == recovery_sentinel
    assert len(adapter.stream_requests) == 2
    second_request = adapter.stream_requests[1]["messages"]
    assert recovery_sentinel in str(second_request)
    assert all(message.get("role") != "assistant" for message in second_request)


@pytest.mark.asyncio
async def test_stream_switching_back_to_reasoning_recovers_visible_tail(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Partial visible answer"},
                {"type": "reasoning_delta", "text": " tail routed as reasoning."},
                {"type": "finish", "reason": "stop"},
            ],
            [
                {"type": "content_delta", "text": " with a recovered ending."},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert assistant.content == " with a recovered ending."
    assert messages[1].content == "Partial visible answer"
    assert messages[1].reasoning == " tail routed as reasoning."
    assert messages[1].interrupted is True
    assert messages[3].content == " with a recovered ending."
    assert len(adapter.stream_requests) == 2
    replayed_assistant = next(
        message
        for message in adapter.stream_requests[1]["messages"]
        if message.get("role") == "assistant"
    )
    assert replayed_assistant["content"] == "Partial visible answer"
    assert not replayed_assistant.get("reasoning")


@pytest.mark.asyncio
async def test_normal_reasoning_then_visible_answer_needs_no_recovery(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Plan first."},
                {"type": "content_delta", "text": "Complete visible answer."},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert assistant.content == "Complete visible answer."
    assert assistant.interrupted is False
    assert persisted_roles(messages) == ["user", "assistant"]
    assert len(adapter.stream_requests) == 1


@pytest.mark.asyncio
async def test_non_streaming_reasoning_only_stop_recovers_visible_answer(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {"content": None, "reasoning": "Answer text routed as reasoning."},
            {"content": "Recovered visible answer.", "reasoning": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=False).send(
        "coder", "Hi", session_id="session-one"
    )

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert assistant.content == "Recovered visible answer."
    assert messages[1].interrupted is True
    assert messages[1].interruption_cause == "provider"
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert len(adapter.requests) == 2
    assert all(message.get("role") != "assistant" for message in adapter.requests[1]["messages"])


@pytest.mark.asyncio
async def test_streaming_mode_chunk_timeout_preserves_partial_after_visible_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.chat.request_runner.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "partial"},
                StreamingChunkTimeoutError("provider stream stalled"),
            ],
            [
                {"type": "content_delta", "text": " continued"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    # A remote provider that stalls after streaming visible content has its
    # partial answer preserved as an interrupted turn (no timeout failure).
    assert assistant.content == " continued"
    assert assistant.interrupted is False
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert messages[1].interrupted is True
    assert messages[1].interruption_cause == "timeout"
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
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

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
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
async def test_local_provider_stream_not_aborted_by_chunk_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.chat.request_runner.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
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
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
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
    monkeypatch.setattr("core.chat.request_runner.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = SlowStreamingStubAdapter(delay=0.05)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        provider_base_url="https://api.openai.com/v1",
    )

    with pytest.raises(RunInterruptedError, match="timeout") as exc_info:
        await build_chat_loop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    # A remote provider keeps the stall guard. Consecutive visible partials are
    # continued twice, then the bounded recovery ends explicitly.
    assert len(adapter.stream_requests) == 3
    assert run.status == RunStatus.INTERRUPTED
    assert isinstance(exc_info.value.result, ChatMessage)
    assert exc_info.value.result.content == "partialpartialpartial"
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "note",
        "assistant",
        "note",
        "assistant",
    ]
    assert messages[-1].role == "run_summary"
    assert messages[-1].status == "interrupted"
