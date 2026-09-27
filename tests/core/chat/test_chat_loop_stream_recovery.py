"""Streaming Model steps that break: restart, continuation from a durable partial, fallback."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.continuation import recover_continuation
from core.chat.streaming import StreamingChunkTimeoutError
from core.providers.errors import NetworkError, ProviderStreamingUnsupportedError
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    normalize_responses_stream_event,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    MODEL_STEP_USAGE_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    RunInterruptedError,
    RunStatus,
)
from core.tools import ToolRegistry, tool_success
from core.utils.errors import ProviderError
from tests.core.chat.chat_loop_streaming_test_support import (
    JsonObject,
    answer,
    send_streaming,
    stream_runtime,
)
from tests.core.chat.chat_loop_support import (
    SlowStreamingStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    event_types,
    history,
    last_run,
    persisted_roles,
    session_address,
)

pytestmark = pytest.mark.usefixtures("recovery_waits")


def _classified_responses_error() -> ProviderError:
    event = {
        "type": "response.failed",
        "response": {
            "status": "failed",
            "error": {"code": "server_error", "message": "Provider overloaded."},
            "error_type": "provider_overloaded",
        },
    }
    with pytest.raises(ProviderError) as exc_info:
        normalize_responses_stream_event("response.failed", event, ResponsesStreamState())
    return exc_info.value


def _tool_call_preview(call_id: str, arguments: str) -> JsonObject:
    return {
        "type": "tool_call_delta",
        "id": call_id,
        "name_delta": "get_weather",
        "arguments_delta": arguments,
    }


_META: JsonObject = {"type": "reasoning_meta", "reasoning_meta": {"sig": "x"}}
_DISCARDED_REASONING: JsonObject = {"type": "reasoning_delta", "text": "Discarded plan."}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_attempt",
    [
        NetworkError("Provider stream ended with native_finish_reason=network_error"),
        [_META, NetworkError("dropped after first byte")],
        [_DISCARDED_REASONING, NetworkError("offline")],
        [
            _DISCARDED_REASONING,
            _tool_call_preview("call_1", '{"city":"Berlin"}'),
            NetworkError("x"),
        ],
        [_META, StreamingChunkTimeoutError("provider stream stalled")],
        "classified",
    ],
    ids=[
        "error-before-any-delta",
        "after-reasoning-meta",
        "after-discarded-reasoning",
        "after-unexecuted-tool-call",
        "chunk-stall",
        "classified-responses-error",
    ],
)
async def test_failure_before_answer_text_restarts_the_identical_request(
    tmp_path: Path, failed_attempt: Any
) -> None:
    if failed_attempt == "classified":
        failed_attempt = _classified_responses_error()
    adapter = StubAdapter(
        [], stream_responses=[failed_attempt, answer(reasoning="Recovered plan.")]
    )
    runtime = stream_runtime(tmp_path, adapter)

    assistant = await send_streaming(runtime)

    run = last_run(runtime)
    assert (assistant.content, assistant.reasoning) == ("Recovered", "Recovered plan.")
    assert len(adapter.stream_requests) == 2
    assert adapter.stream_requests[0]["messages"] == adapter.stream_requests[1]["messages"]
    assert run.status == RunStatus.COMPLETED
    assert STREAM_ATTEMPT_RESTARTED_EVENT in event_types(run)
    # The discarded attempt leaves no error, partial or Continuation state behind.
    assert persisted_roles(history(runtime)) == ["user", "assistant"]
    assert (
        await recover_continuation(
            runtime.chat_sessions.get(session_address("coder", "session-one"))
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        (NetworkError("dropped mid-stream"), "network"),
        (StreamingChunkTimeoutError("provider stream stalled"), "timeout"),
        ("classified", "provider"),
        (None, "network"),
    ],
    ids=["network-drop", "chunk-stall", "classified-responses-error", "missing-finish"],
)
async def test_failure_after_answer_text_continues_from_the_durable_partial(
    tmp_path: Path, failure: Any, cause: str
) -> None:
    if failure == "classified":
        failure = _classified_responses_error()
    partial: list[Any] = [{"type": "content_delta", "text": "partial"}]
    if failure is not None:
        partial.append(failure)
    adapter = StubAdapter([], stream_responses=[partial, answer(" continued")])
    runtime = stream_runtime(tmp_path, adapter)

    assistant = await send_streaming(runtime)

    run = last_run(runtime)
    messages = history(runtime)
    # The original request is not replayed: the durable partial plus an internal
    # recovery reminder form a new Model step inside the same Run.
    assert (assistant.content, assistant.interrupted) == (" continued", False)
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert (messages[1].content, messages[1].interrupted) == ("partial", True)
    assert messages[1].interruption_cause == cause
    continuation_request = adapter.stream_requests[1]["messages"]
    assert (continuation_request[-2]["role"], continuation_request[-2]["content"]) == (
        "assistant",
        "partial",
    )
    assert "Continue the same task" in continuation_request[-1]["content"]
    assert event_types(run) == [
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
async def test_streaming_unsupported_before_output_falls_back_to_one_plain_request(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(
        [{"content": "Fallback answer", "tool_calls": None}],
        stream_responses=[ProviderStreamingUnsupportedError("streaming is not supported")],
    )
    runtime = stream_runtime(tmp_path, adapter)

    assistant = await send_streaming(runtime)

    assert assistant.content == "Fallback answer"
    assert event_types(last_run(runtime)) == [
        "run_started",
        "user_message_persisted",
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_completed",
    ]
    assert (len(adapter.stream_requests), len(adapter.requests)) == (1, 1)


@pytest.mark.asyncio
async def test_streaming_unsupported_after_output_continues_the_partial_without_replaying_it(
    tmp_path: Path,
) -> None:
    unsupported = ProviderStreamingUnsupportedError("streaming is not supported")
    adapter = StubAdapter(
        [{"content": "Continued answer", "tool_calls": None}],
        stream_responses=[[{"type": "content_delta", "text": "partial"}, unsupported], unsupported],
    )
    runtime = stream_runtime(tmp_path, adapter)

    assistant = await send_streaming(runtime)

    run = last_run(runtime)
    messages = history(runtime)
    assert (assistant.content, assistant.interrupted) == ("Continued answer", False)
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert messages[1].interrupted is True
    assert event_types(run) == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "assistant_output",
        MODEL_STEP_USAGE_EVENT,
        "run_completed",
    ]
    assert (len(adapter.stream_requests), len(adapter.requests)) == (2, 1)


@pytest.mark.asyncio
async def test_generic_provider_error_fails_without_a_plain_request(tmp_path: Path) -> None:
    adapter = StubAdapter(
        [{"content": "Should not use", "tool_calls": None}],
        stream_responses=[ProviderError("provider failed", retryable=False)],
    )
    runtime = stream_runtime(tmp_path, adapter)

    with pytest.raises(ProviderError, match="provider failed"):
        await send_streaming(runtime)

    messages = history(runtime)
    assert last_run(runtime).status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert messages[1].error_kind == "provider_fatal"
    assert (len(adapter.stream_requests), len(adapter.requests)) == (1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streaming", "plain"])
async def test_reasoning_only_stop_recovers_with_a_visible_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    recovery_sentinel = "test-owned output-integrity recovery"
    monkeypatch.setattr(
        "core.chat.request_runner.OUTPUT_INTEGRITY_RECOVERY_NOTE", recovery_sentinel
    )
    misrouted = "Answer text routed as reasoning."
    adapter = StubAdapter(
        [
            {"content": None, "reasoning": misrouted},
            {"content": "Recovered visible answer.", "reasoning": None},
        ],
        stream_responses=[
            [{"type": "reasoning_delta", "text": misrouted}, {"type": "finish", "reason": "stop"}],
            answer("Recovered visible answer."),
        ],
    )
    runtime = stream_runtime(tmp_path, adapter)

    assistant = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Hi", session_id="session-one"
    )

    messages = history(runtime)
    requests = adapter.stream_requests if streaming else adapter.requests
    assert assistant.content == "Recovered visible answer."
    assert last_run(runtime).status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "note", "assistant"]
    assert (messages[1].reasoning, messages[1].interrupted) == (misrouted, True)
    assert messages[1].interruption_cause == "provider"
    assert messages[2].content == recovery_sentinel
    assert len(requests) == 2
    assert recovery_sentinel in str(requests[1]["messages"])
    assert all(message.get("role") != "assistant" for message in requests[1]["messages"])


@pytest.mark.asyncio
async def test_stream_switching_back_to_reasoning_recovers_the_visible_tail(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Partial visible answer"},
                {"type": "reasoning_delta", "text": " tail routed as reasoning."},
                {"type": "finish", "reason": "stop"},
            ],
            answer(" with a recovered ending."),
        ],
    )
    runtime = stream_runtime(tmp_path, adapter)

    assistant = await send_streaming(runtime)

    messages = history(runtime)
    assert assistant.content == " with a recovered ending."
    assert messages[1].content == "Partial visible answer"
    assert messages[1].reasoning == " tail routed as reasoning."
    assert messages[1].interrupted is True
    assert messages[3].content == " with a recovered ending."
    replayed_assistant = next(
        message
        for message in adapter.stream_requests[1]["messages"]
        if message.get("role") == "assistant"
    )
    assert replayed_assistant["content"] == "Partial visible answer"
    assert not replayed_assistant.get("reasoning")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("partial", "expected_entries"),
    [
        (
            [
                {"type": "reasoning_meta", "reasoning_meta": {"signature": "partial-sig"}},
                {"type": "reasoning_delta", "text": "Thinking."},
                {"type": "content_delta", "text": "Visible"},
            ],
            [{"content": "Visible"}],
        ),
        # An unclosed inline thinking block leaves no visible answer text.
        ([{"type": "content_delta", "text": "<think>partial inline thought"}], []),
    ],
    ids=["native-reasoning", "unclosed-inline-thinking"],
)
async def test_partial_continuation_never_replays_interrupted_reasoning(
    tmp_path: Path, partial: list[JsonObject], expected_entries: list[JsonObject]
) -> None:
    adapter = StubAdapter(
        [], stream_responses=[[*partial, NetworkError("dropped mid-stream")], answer("Answer")]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )

    await send_streaming(runtime)

    replayed = [
        {
            key: message[key]
            for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
            if key in message
        }
        for message in adapter.stream_requests[1]["messages"]
        if message["role"] == "assistant"
    ]
    assert replayed == expected_entries
    partial_message = next(m for m in history(runtime) if m.role == "assistant")
    assert partial_message.interrupted
    assert partial_message.reasoning is not None


@pytest.mark.asyncio
async def test_interrupted_partial_drops_its_in_flight_tool_call_so_the_tool_runs_once(
    tmp_path: Path,
) -> None:
    executed: list[str] = []

    def run_weather(_context: Any, _arguments: Any) -> JsonObject:
        executed.append("ran")
        return tool_success({"ok": True})

    tools = ToolRegistry()
    tools.register("get_weather", "Get weather.", {"type": "object"}, run_weather)
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Let me check"},
                _tool_call_preview("call_abc", '{"city":"Ber'),
                NetworkError("dropped mid tool-call"),
            ],
            [
                _tool_call_preview("call_full", '{"city":"Berlin"}'),
                {"type": "finish", "reason": "tool_calls"},
            ],
            answer("Weather checked"),
        ],
    )
    runtime = stream_runtime(tmp_path, adapter, tools=tools)

    assistant = await send_streaming(runtime, "Weather?")

    messages = history(runtime)
    assert (assistant.content, assistant.interrupted, assistant.tool_calls) == (
        "Weather checked",
        False,
        None,
    )
    assert executed == ["ran"]
    assert last_run(runtime).status == RunStatus.COMPLETED
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
async def test_interrupted_turn_partial_text_replays_into_the_next_request(tmp_path: Path) -> None:
    adapter = StubAdapter([{"content": "Continued answer", "tool_calls": None}])
    runtime = stream_runtime(tmp_path, adapter)
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
    # The truncated turn stays in the request history so the Model can continue it,
    # but the internal interruption fields never reach the Provider.
    assert any(
        m["role"] == "assistant" and m["content"] == "The first half of the answer"
        for m in request_messages
    )
    assert all("interrupted" not in m and "interruption_cause" not in m for m in request_messages)


@pytest.mark.asyncio
async def test_local_provider_stream_is_not_aborted_by_a_chunk_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.chat.request_runner.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    adapter = SlowStreamingStubAdapter(delay=0.05)
    runtime = stream_runtime(tmp_path, adapter, provider_base_url="http://localhost:11434/v1")

    assistant = await send_streaming(runtime)

    # The local Provider's silence exceeds the chunk timeout but the stream completes.
    assert (assistant.content, assistant.interrupted) == ("partial done", False)
    assert last_run(runtime).status == RunStatus.COMPLETED
    assert persisted_roles(history(runtime)) == ["user", "assistant"]


@pytest.mark.asyncio
async def test_remote_provider_chunk_stalls_end_in_a_bounded_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("core.chat.request_runner.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    adapter = SlowStreamingStubAdapter(delay=0.05)
    runtime = stream_runtime(tmp_path, adapter, provider_base_url="https://api.openai.com/v1")

    with pytest.raises(RunInterruptedError, match="timeout") as exc_info:
        await send_streaming(runtime)

    run = last_run(runtime)
    messages = history(runtime)
    # Consecutive visible partials are continued eight times, then recovery ends.
    assert len(adapter.stream_requests) == 9
    assert run.status == RunStatus.INTERRUPTED
    run_diagnostics = [
        record
        for record in caplog.records
        if record.name == "vbot.runs"
        and record.levelno >= logging.WARNING
        and isinstance(record.args, tuple)
        and record.args[:1] == (run.id,)
    ]
    assert [record.levelno for record in run_diagnostics] == [logging.WARNING]
    assert not any(
        record.name == "vbot.chat"
        and record.levelno >= logging.INFO
        and isinstance(record.args, tuple)
        and record.args[:2] == (run.id, "interrupted")
        for record in caplog.records
    )
    assert isinstance(exc_info.value.result, ChatMessage)
    assert exc_info.value.result.content == "partial" * 9
    assert persisted_roles(messages) == ["user", "assistant"] + ["note", "assistant"] * 8
    assert messages[-1].role == "run_summary"
    assert messages[-1].status == "interrupted"
