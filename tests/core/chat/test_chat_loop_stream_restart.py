"""Tests for chat loop stream restart."""

from __future__ import annotations

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
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    normalize_responses_stream_event,
)
from core.runs import (
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
from tests.core.chat.chat_loop_stream_recovery_test_support import (
    JsonObject,
)
from tests.core.chat.chat_loop_support import (
    MidStreamCancelledStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
    session_address,
)


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
