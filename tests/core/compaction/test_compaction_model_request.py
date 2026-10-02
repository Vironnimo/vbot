"""The Compaction Model call: canonical stream acceptance, usage accounting and stall bounds."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

from core.chat.streaming import (
    StreamingChunkTimeoutError,
    StreamingProgressTimeoutError,
    iter_with_chunk_timeout,
)
from core.compaction import CompactionError, CompactionService, CompactionSettings
from core.compaction._model_request import _send_streaming_model_request
from core.providers.anthropic import AnthropicAdapter
from core.providers.errors import NetworkError
from core.providers.ollama import OllamaAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.sessions import SessionAddress
from core.utils.errors import ProviderError
from tests.core.chat.usage_recorder_support import RecordingUsageRecorder
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    assistant,
    compact,
    provider_request,
    user,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_class", "strategy"),
    [
        (OpenAICompatibleAdapter, "summary_tail"),
        (AnthropicAdapter, "continuation"),
        (OllamaAdapter, "summary_tail"),
    ],
)
async def test_compaction_consumes_canonical_stream_without_raw_wire_normalization(
    adapter_class: type[Any], strategy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = object.__new__(adapter_class)
    adapter._model_lookup = None
    requests: list[list[dict[str, Any]]] = []

    async def stream(
        messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        requests.append(messages)
        yield {"type": "heartbeat"}
        yield {"type": "content_delta", "text": "SUMMARY SENTINEL"}
        yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr(adapter, "stream", stream)
    messages = [
        user("u1", "old request"),
        assistant("a1", "old answer"),
        user("u2", "current request"),
        assistant("a2", "current answer"),
    ]

    result = await compact(
        messages,
        summary_adapter=adapter,
        summary_model_id="summary",
        settings=CompactionSettings(strategy=strategy, tail_tokens=1),
        request_messages=provider_request(messages),
        active_adapter=adapter,
        active_model_id="summary",
    )

    assert result.role == "compaction_checkpoint"
    assert "SUMMARY SENTINEL" in str(result.content)
    assert len(requests) == 1


_TOOL_CALL = {
    "type": "tool_call_delta",
    "id": "call-1",
    "name_delta": "read",
    "arguments_delta": '{"path":"notes.txt"}',
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "tool_call", "finish"),
    [
        ("summary_tail", False, "output_truncated"),
        ("continuation", False, "content_filtered"),
        ("summary_tail", False, None),
        ("continuation", False, "tool_calls"),
        ("summary_tail", True, "stop"),
    ],
    ids=["truncated", "filtered", "missing-finish", "tool-calls-finish", "tool-attempt-with-stop"],
)
async def test_compaction_accepts_only_a_completed_text_summary(
    strategy: str, tool_call: bool, finish: str | None
) -> None:
    class IncompleteAdapter(StubAdapter):
        @override
        async def stream(
            self, messages: list[dict], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            self.requests.append({"messages": messages, **kwargs})
            yield {"type": "content_delta", "text": "Partial summary"}
            if tool_call:
                yield _TOOL_CALL
            if finish is not None:
                yield {"type": "finish", "reason": finish}

    adapter = IncompleteAdapter()
    messages = [user("u1", "request"), assistant("a1", "answer")]
    original_messages = [message.to_dict() for message in messages]

    with pytest.raises(CompactionError):
        await compact(
            messages,
            summary_adapter=adapter,
            summary_model_id="summary",
            settings=CompactionSettings(strategy=strategy),
            request_messages=provider_request(messages),
            active_adapter=adapter,
            active_model_id="summary",
        )

    assert len(adapter.requests) == 1
    assert [message.to_dict() for message in messages] == original_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True], ids=["accepted", "rejected"])
async def test_compaction_attempt_records_usage_before_checkpoint_acceptance(rejected):
    class UsageAdapter(StubAdapter):
        @override
        async def stream(self, messages, **kwargs):
            # Providers split counters around the finish; every part is recorded.
            yield {"type": "content_delta", "text": "Summary"}
            yield {"type": "usage", "input_tokens": 100, "cache_read_tokens": 20}
            yield {"type": "finish", "reason": "stop"}
            yield {"type": "usage", "output_tokens": 6, "reasoning_tokens": 10}

    recorder = RecordingUsageRecorder()
    adapter = UsageAdapter()
    messages = [user("u1", "Request"), assistant("a1", "Answer")]
    operation = compact(
        messages,
        service=CompactionService(usage_recorder=cast(Any, recorder)),
        session_address=SessionAddress(project_id="project", agent_id="coder", session_id="one"),
        prompt_cache_affinity_id="affinity",
        summary_adapter=adapter,
        summary_model_id="summary",
        summary_model_reference="openai/summary",
        active_adapter=adapter,
        active_model_id="active",
        active_model_reference="openai/active",
        run_id="run-one",
        owner_name="example",
        group_id="group-one",
        settings=CompactionSettings(strategy="continuation"),
        request_messages=provider_request(messages),
        minimum_reclaim_tokens=100_000 if rejected else 0,
    )
    checkpoint = None
    if rejected:
        with pytest.raises(CompactionError):
            await operation
    else:
        checkpoint = await operation

    [call] = recorder.calls
    assert {key: call[key] for key in ("model", "kind", "run_id", "owner_name", "group_id")} == {
        "model": "openai/active",
        "kind": "compaction",
        "run_id": "run-one",
        "owner_name": "example",
        "group_id": "group-one",
    }
    assert (call["agent_id"], call["session_id"], call["project_id"]) == ("coder", "one", "project")
    assert call["usage"] == {
        "input_tokens": 100,
        "output_tokens": 6,
        "cache_read_tokens": 20,
        "reasoning_tokens": 10,
        "usage_call_id": call["id"],
    }
    if checkpoint is not None:
        assert checkpoint.usage is not None
        assert set(checkpoint.usage) == {"compacted_token_count", "model_call"}
        assert checkpoint.usage["model_call"]["usage"]["usage_call_id"] == call["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "statuses"),
    [
        (["output_truncated"], ["failed"]),
        (["cancelled"], ["cancelled"]),
        (["rejected"], ["failed"]),
        (["dropped", "stop"], ["failed", "completed"]),
        (["dropped", "dropped"], ["failed", "failed"]),
    ],
    ids=["truncated", "cancelled", "fatal-provider-error", "dropped-once", "dropped-twice"],
)
async def test_compaction_attempts_retain_usage_and_retry_a_transient_failure_once(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[str], statuses: list[str]
) -> None:
    remaining = list(outcomes)
    waits: list[float] = []

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    class FlakyAdapter(StubAdapter):
        @override
        async def stream(self, messages, **kwargs):
            yield {"type": "usage", "input_tokens": 75}
            outcome = remaining.pop(0)
            if outcome == "cancelled":
                raise asyncio.CancelledError
            if outcome == "rejected":
                raise ProviderError("bad request", retryable=False)
            if outcome == "dropped":
                # A connection lost mid-stream surfaces as a retryable error.
                raise NetworkError("stream dropped")
            yield {"type": "content_delta", "text": "Summary"}
            yield {"type": "finish", "reason": outcome}

    monkeypatch.setattr("core.compaction._model_request._sleep", record_wait)
    recorder = RecordingUsageRecorder()
    adapter = FlakyAdapter()
    messages = [user("u1", "Request"), assistant("a1", "Answer")]
    operation = compact(
        messages,
        service=CompactionService(usage_recorder=cast(Any, recorder)),
        summary_adapter=adapter,
        summary_model_id="summary",
        active_adapter=adapter,
        active_model_id="active",
        active_model_reference="openai/active",
        settings=CompactionSettings(strategy="continuation"),
        request_messages=provider_request(messages),
    )
    if outcomes[-1] == "stop":
        checkpoint = await operation
        assert checkpoint.usage is not None
        assert checkpoint.usage["model_call"]["usage"]["usage_call_id"] == recorder.calls[-1]["id"]
    elif outcomes[-1] == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        with pytest.raises(CompactionError) as failure:
            await operation
        # The failure names the Model whose call failed for the caller's log.
        assert failure.value.model == "openai/active"

    assert remaining == []
    # Every attempt, failed or not, keeps its own Usage row.
    assert [call["status"] for call in recorder.calls] == statuses
    assert all(
        call["usage"] == {"input_tokens": 75, "usage_call_id": call["id"]}
        for call in recorder.calls
    )
    # Only a retryable Provider failure waits the shared backoff and retries once.
    assert len(waits) == (1 if outcomes[0] == "dropped" else 0)
    assert all(1.0 <= wait <= 1.5 for wait in waits)


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeats", [False, True])
async def test_compaction_stalled_stream_is_bounded_and_closed(monkeypatch, heartbeats):
    closed = asyncio.Event()
    clock = 0.0

    class StalledAdapter:
        async def stream(self, *args, **kwargs):
            nonlocal clock
            try:
                while True:
                    if heartbeats:
                        yield {"type": "heartbeat"}
                        clock += 0.05
                    else:
                        await asyncio.Event().wait()
            finally:
                closed.set()

    def bounded(source):
        return iter_with_chunk_timeout(source, timeout_seconds=0.05, progress_timeout_seconds=0.15)

    # The controlled clock decides the timeout branch deterministically: a
    # heartbeat source that advances it past the progress window must report a
    # progress timeout, independent of how fast workers are scheduled.
    monkeypatch.setattr("core.chat.streaming.time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr("core.compaction._model_request.iter_with_chunk_timeout", bounded)
    expected = StreamingProgressTimeoutError if heartbeats else StreamingChunkTimeoutError
    with pytest.raises(expected):
        await asyncio.wait_for(_send_streaming_model_request(StalledAdapter(), [], {}), 2)
    assert closed.is_set()
