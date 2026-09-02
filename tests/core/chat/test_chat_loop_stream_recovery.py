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
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    normalize_responses_stream_event,
)
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    MODEL_STEP_USAGE_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    RunCancelledError,
    RunInterruptedError,
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
    PolicyStubAdapter,
    SilentBlockingStreamingStubAdapter,
    SlowStreamingStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
    session_address,
)

JsonObject = dict[str, Any]


class CompletedStreamingStubAdapter(StubAdapter):
    """Finish a visible stream, then let the test arrange the persist-lock race."""

    def __init__(self) -> None:
        super().__init__([])
        self.finish_emitted = asyncio.Event()
        self.release_stream = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        del messages, model_id, kwargs
        yield {"type": "content_delta", "text": "Complete answer"}
        yield {"type": "finish", "reason": "stop"}
        self.finish_emitted.set()
        await self.release_stream.wait()


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
async def test_streaming_cancellation_with_reasoning_retains_continuation(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = MidStreamCancelledStubAdapter([])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    loop = build_chat_loop(runtime, streaming=True)
    with pytest.raises(RunCancelledError):
        await loop.send("coder", "Hi", session_id="session-one")

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert persisted_roles(messages) == ["user"]
    state = await recover_continuation(
        runtime.chat_sessions.get(session_address("coder", "session-one"))
    )
    assert state is not None
    assert state.reasoning == "Need network."
    assert state.cause == "internal"


@pytest.mark.asyncio
async def test_streaming_network_error_with_reasoning_restarts_cleanly(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Discarded plan."},
                NetworkError("offline"),
            ],
            [
                {"type": "reasoning_delta", "text": "Recovered plan."},
                {"type": "content_delta", "text": "Recovered"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    assert assistant.content == "Recovered"
    assert assistant.reasoning == "Recovered plan."
    assert len(adapter.stream_requests) == 2
    assert await recover_continuation(session) is None


@pytest.mark.asyncio
async def test_streaming_empty_native_network_error_restarts_instead_of_empty_assistant(
    tmp_path: Path,
) -> None:
    """An empty stop concealed as network_error must restart, not fail validation."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                NetworkError("Provider stream ended with native_finish_reason=network_error"),
            ],
            [
                {"type": "content_delta", "text": "Recovered"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    assert assistant.content == "Recovered"
    assert len(adapter.stream_requests) == 2
    assert persisted_roles(session.load()) == ["user", "assistant"]
    assert await recover_continuation(session) is None


@pytest.mark.asyncio
async def test_reasoning_only_restart_exhaustion_keeps_only_final_attempt_checkpoint(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": f"Attempt {attempt}"},
                NetworkError(f"drop {attempt}"),
            ]
            for attempt in range(1, 4)
        ],
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(RunInterruptedError, match="network"):
        await build_chat_loop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    run = next(iter(runtime.chat_runs._runs.values()))
    state = await recover_continuation(session)
    assert state is not None
    assert state.reasoning == "Attempt 3"
    assert state.cause == "network"
    assert len(adapter.stream_requests) == 3
    assert run.status == RunStatus.INTERRUPTED
    messages = session.load()
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].reasoning == "Attempt 3"
    assert messages[1].interrupted is True
    assert messages[-1].role == "run_summary"
    assert messages[-1].status == "interrupted"


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
    # Visible content present at the drop → preserved as an interrupted turn,
    # not discarded; the Continuation Checkpoint retains the readable state.
    assert assistant.content == " continued"
    assert assistant.interrupted is False
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert messages[1].interrupted is True
    continuation_request = adapter.stream_requests[1]["messages"]
    assert continuation_request[-2]["role"] == "assistant"
    assert continuation_request[-2]["content"] == "partial"
    assert "Continue the same task" in continuation_request[-1]["content"]


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
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert assistant.content == "Recovered"
    assert len(adapter.stream_requests) == 2
    assert adapter.stream_requests[0]["messages"] == adapter.stream_requests[1]["messages"]
    assert run.status == RunStatus.COMPLETED
    # The discarded attempt leaves no error or durable readable state.
    assert persisted_roles(messages) == ["user", "assistant"]


@pytest.mark.asyncio
async def test_streaming_mode_restarts_after_classified_responses_error_before_output(
    tmp_path: Path,
) -> None:
    classified_error = _classified_responses_error(
        "response.failed",
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {"code": "server_error", "message": "Provider overloaded."},
                "error_type": "provider_overloaded",
            },
        },
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            classified_error,
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

    assert assistant.content == "Recovered"
    assert len(adapter.stream_requests) == 2
    assert adapter.stream_requests[0]["messages"] == adapter.stream_requests[1]["messages"]


@pytest.mark.asyncio
async def test_streaming_mode_restarts_after_reasoning_only_responses_error(
    tmp_path: Path,
) -> None:
    classified_error = _classified_responses_error(
        "error",
        {
            "type": "error",
            "error": {"code": "server_error", "message": "Provider overloaded."},
        },
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.6-terra", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Discarded reasoning"},
                classified_error,
            ],
            [
                {"type": "reasoning_delta", "text": "Recovered reasoning"},
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

    assert assistant.content == "Recovered"
    assert assistant.reasoning == "Recovered reasoning"
    assert len(adapter.stream_requests) == 2
    assert adapter.stream_requests[0]["messages"] == adapter.stream_requests[1]["messages"]


@pytest.mark.asyncio
async def test_streaming_mode_continues_same_run_after_visible_delta(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Visible"},
                NetworkError("dropped mid-stream"),
            ],
            [
                {"type": "content_delta", "text": " continuation"},
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
    # The original request is not replayed. Its durable partial plus an internal
    # recovery reminder form a new Model step inside the same Run.
    assert len(adapter.stream_requests) == 2
    assert assistant.content == " continuation"
    assert assistant.interrupted is False
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert messages[1].interrupted is True
    assert adapter.stream_requests[0]["messages"] != adapter.stream_requests[1]["messages"]


@pytest.mark.asyncio
async def test_streaming_mode_preserves_partial_after_classified_responses_error(
    tmp_path: Path,
) -> None:
    classified_error = _classified_responses_error(
        "response.failed",
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {"code": "server_error", "message": "Provider overloaded."},
                "error_type": "provider_overloaded",
            },
        },
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Visible"},
                classified_error,
            ],
            [
                {"type": "content_delta", "text": " continued"},
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

    assert len(adapter.stream_requests) == 2
    assert assistant.content == " continued"
    assert assistant.interrupted is False


@pytest.mark.asyncio
async def test_streaming_mode_restarts_after_unexecuted_tool_call_delta(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.6-terra", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Discarded plan."},
                {
                    "type": "tool_call_delta",
                    "id": "call_1",
                    "name_delta": "read",
                    "arguments_delta": '{"path":"note.txt"}',
                },
                NetworkError("dropped after Tool Call"),
            ],
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
    assert assistant.content == "Recovered"
    assert len(adapter.stream_requests) == 2
    assert persisted_roles(
        runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    ) == [
        "user",
        "assistant",
    ]
    assert any(event.type == STREAM_ATTEMPT_RESTARTED_EVENT for event in run.events)


def _classified_responses_error(
    event_name: str,
    event_data: JsonObject,
) -> ProviderError:
    with pytest.raises(ProviderError) as exc_info:
        normalize_responses_stream_event(
            event_name,
            event_data,
            ResponsesStreamState(),
        )
    return exc_info.value


@pytest.mark.asyncio
async def test_streaming_mode_restart_exhaustion_marks_run_interrupted(tmp_path: Path) -> None:
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

    with pytest.raises(RunInterruptedError, match="network"):
        await build_chat_loop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    # Initial attempt plus MAX_STREAM_RESTARTS replays, then recovery ends with
    # an explicit interruption instead of a fabricated normal completion/error.
    assert len(adapter.stream_requests) == 3
    assert run.status == RunStatus.INTERRUPTED
    assert persisted_roles(messages) == ["user"]
    assert messages[-1].role == "run_summary"
    assert messages[-1].status == "interrupted"


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
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert assistant.content == "Recovered"
    assert len(adapter.stream_requests) == 2
    assert run.status == RunStatus.COMPLETED
    # The discarded attempt leaves no error or durable readable state.
    assert persisted_roles(messages) == ["user", "assistant"]


@pytest.mark.asyncio
async def test_streaming_mode_continues_after_chunk_stall_with_visible_output(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Visible"},
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
    assert len(adapter.stream_requests) == 2
    assert assistant.content == " continued"
    assert assistant.interrupted is False
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
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
            ],
            [
                {
                    "type": "tool_call_delta",
                    "id": "call_full",
                    "name_delta": "get_weather",
                    "arguments_delta": '{"city":"Berlin"}',
                },
                {"type": "finish", "reason": "tool_calls"},
            ],
            [
                {"type": "content_delta", "text": "Weather checked"},
                {"type": "finish", "reason": "stop"},
            ],
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
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    # The half-streamed Tool Call is dropped. The continuation must regenerate a
    # complete call before dispatch, so the Tool runs exactly once.
    assert assistant.content == "Weather checked"
    assert assistant.interrupted is False
    assert assistant.tool_calls is None
    assert executed == ["ran"]
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "note",
        "assistant",
        "tool",
        "assistant",
    ]
    assert messages[1].tool_calls is None
    assert messages[3].tool_calls is not None
    assert messages[3].tool_calls[0].arguments == {"city": "Berlin"}


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
            interruption_cause="timeout",
        )
    )

    await build_chat_loop(runtime).send("coder", "continue", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assistant_entries = [m for m in request_messages if m["role"] == "assistant"]
    # The truncated turn is in the request history so the model can continue it —
    # but the internal interrupted flag never reaches the provider.
    assert any(m["content"] == "The first half of the answer" for m in assistant_entries)
    assert all("interrupted" not in m for m in request_messages)
    assert all("interruption_cause" not in m for m in request_messages)


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

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].content == "before"
    assert messages[1].interrupted is True
    assert not any(message.error_kind for message in messages if message.role == "error")


@pytest.mark.asyncio
async def test_user_cancel_while_complete_stream_waits_to_persist_preserves_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = CompletedStreamingStubAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Hi", session_id="session-one"
    )
    await adapter.finish_emitted.wait()

    target_lock = runtime.chat_sessions.write_lock(session_address("coder", "session-one"))
    holder_acquired = asyncio.Event()
    release_holder = asyncio.Event()
    persist_wait_started = asyncio.Event()

    async def hold_write_lock() -> None:
        async with target_lock:
            holder_acquired.set()
            await release_holder.wait()

    class SignallingWriteLock:
        async def __aenter__(self) -> Any:
            persist_wait_started.set()
            return await target_lock.__aenter__()

        async def __aexit__(self, *exc_info: object) -> None:
            await target_lock.__aexit__(*exc_info)

    holder_task = asyncio.create_task(hold_write_lock())
    await holder_acquired.wait()
    monkeypatch.setattr(
        runtime.chat_sessions,
        "write_lock",
        lambda *_args, **_kwargs: SignallingWriteLock(),
    )
    adapter.release_stream.set()
    await persist_wait_started.wait()

    run.request_cancel(reason="user")
    await asyncio.sleep(0)
    release_holder.set()
    await holder_task

    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.CANCELLED
    assert [message.role for message in messages] == ["user", "assistant", "run_summary"]
    assert messages[1].content == "Complete answer"
    assert messages[1].interrupted is False


@pytest.mark.asyncio
async def test_user_cancel_replays_interrupted_reasoning_only_through_checkpoint(
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

    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user", "assistant"]
    assert messages[1].content is None
    assert messages[1].reasoning == "Thinking hard."
    assert messages[1].reasoning_meta == {"signature": "interrupted-signed-state"}
    assert messages[1].interrupted is True

    reasoning_events = [event for event in run.events if event.type == "reasoning"]
    assistant_events = [event for event in run.events if event.type == "assistant_output"]
    assert reasoning_events[-1].payload["message"]["reasoning"] == "Thinking hard."
    assert "reasoning_meta" not in reasoning_events[-1].payload["message"]
    assert assistant_events[-1].payload["message"]["interrupted"] is True

    state = await recover_continuation(
        runtime.chat_sessions.get(session_address("coder", "session-one"))
    )
    assert state is not None
    assert state.reasoning == "Thinking hard."
    summaries = [message for message in messages if message.role == "run_summary"]
    assert summaries[-1].status == "cancelled"

    followup_adapter = PolicyStubAdapter(
        [{"content": "Recovered safely.", "tool_calls": None}],
        policy=REASONING_REPLAY_FULL_HISTORY,
    )
    runtime.adapter = followup_adapter
    await build_chat_loop(runtime).send("coder", "Continue safely", session_id="session-one")

    request_messages = followup_adapter.requests[0]["messages"]
    reminder = next(
        str(message["content"])
        for message in request_messages
        if "<continuation-checkpoint" in str(message.get("content") or "")
    )
    assert "Thinking hard." in reminder
    assert not [message for message in request_messages if message["role"] == "assistant"]
    persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert persisted[1].reasoning == "Thinking hard."
    assert persisted[1].reasoning_meta == {"signature": "interrupted-signed-state"}


@pytest.mark.asyncio
async def test_user_cancel_before_visible_output_does_not_persist_assistant(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = SilentBlockingStreamingStubAdapter()
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
    assert run.status == RunStatus.CANCELLED
    assert persisted_roles(messages) == ["user"]
    assert not any(event.type == "assistant_output" for event in run.events)
