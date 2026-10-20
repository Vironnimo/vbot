"""The Compaction Model call: canonical stream acceptance, usage accounting and stall bounds."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

import core.compaction._model_request as model_request
from core.chat.streaming import (
    StreamingChunkTimeoutError,
    StreamingProgressTimeoutError,
    iter_with_chunk_timeout,
)
from core.compaction import CompactionError, CompactionService, CompactionSettings
from core.compaction._model_request import _send_streaming_model_request
from core.compaction.compaction import COMPACTION_REFERENCE_PREFIX, COMPACTION_SUMMARY_END_MARKER
from core.providers.adapter import resolve_request_input_budget
from core.providers.anthropic import AnthropicAdapter
from core.providers.errors import NetworkError
from core.providers.ollama import OllamaAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import RequestInput
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


_PARTIAL = "Partial summary"
# Delimiters and framing copied from the request, without any summary between them.
_MARKERS_ONLY = (
    f"<system-reminder>\n{COMPACTION_REFERENCE_PREFIX}\n{COMPACTION_SUMMARY_END_MARKER}\n"
    "</system-reminder>"
)

_TOOL_CALL = {
    "type": "tool_call_delta",
    "id": "call-1",
    "name_delta": "read",
    "arguments_delta": '{"path":"notes.txt"}',
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "text", "tool_call", "finish"),
    [
        ("summary_tail", _PARTIAL, False, "output_truncated"),
        ("continuation", _PARTIAL, False, "content_filtered"),
        ("summary_tail", _PARTIAL, False, None),
        ("continuation", _PARTIAL, False, "tool_calls"),
        ("summary_tail", _PARTIAL, True, "stop"),
        ("summary_tail", _MARKERS_ONLY, False, "stop"),
        ("continuation", _MARKERS_ONLY, False, "stop"),
    ],
    ids=[
        "truncated",
        "filtered",
        "missing-finish",
        "tool-calls-finish",
        "tool-attempt-with-stop",
        "summary-tail-markers-only",
        "continuation-markers-only",
    ],
)
async def test_compaction_accepts_only_a_completed_text_summary(
    strategy: str, text: str, tool_call: bool, finish: str | None
) -> None:
    class IncompleteAdapter(StubAdapter):
        @override
        async def stream(
            self, messages: list[dict], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            self.requests.append({"messages": messages, **kwargs})
            yield {"type": "content_delta", "text": text}
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


class _BudgetAdapter(StubAdapter):
    """Counts 10 tokens per request message and 5 per Tool on its wire.

    Its response reports the measured input *input_tokens*, or only output.
    """

    def __init__(self, input_tokens: int | None) -> None:
        super().__init__("Summary")
        self.budgets: list[RequestInput] = []
        self.input_tokens = input_tokens

    def estimate_request_input_tokens(
        self, messages: list[dict[str, Any]], *, model_id: str, tools: Any = None
    ) -> int:
        return 10 * len(messages) + 5 * len(tools or [])

    @override
    async def stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.budgets.append(resolve_request_input_budget(kwargs["model_id"], -1))
        async for delta in super().stream(messages, **kwargs):
            yield delta
        if self.input_tokens is None:
            yield {"type": "usage", "output_tokens": 3}
        else:
            yield {"type": "usage", "input_tokens": self.input_tokens}


class _SummaryCalibration(RecordingUsageRecorder):
    @override
    def input_estimate_factor(self, model: str) -> float:
        return 1.5 if model == "openai/summary" else 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "context_tokens", "measured"),
    [
        ("summary_tail", None, 4_321),
        ("continuation", None, 4_321),
        ("continuation", 50_000, 4_321),
        ("summary_tail", None, None),
    ],
    ids=["summary-model", "continuation-estimated", "continuation-measured", "unmeasured"],
)
async def test_compaction_request_reads_and_teaches_its_input_estimate_calibration(
    strategy: str, context_tokens: int | None, measured: int | None
) -> None:
    summary, active = _BudgetAdapter(measured), _BudgetAdapter(measured)
    messages = [user("u1", "old"), assistant("a1", "old answer"), user("u2", "now")]
    recorder = _SummaryCalibration()

    await compact(
        messages,
        service=CompactionService(usage_recorder=cast(Any, recorder)),
        summary_adapter=summary,
        summary_model_id="summary",
        summary_model_reference="openai/summary",
        active_adapter=active,
        active_model_id="active",
        active_model_reference="openai/active",
        active_tools=[{"type": "function", "function": {"name": "read", "parameters": {}}}],
        settings=CompactionSettings(strategy=strategy, tail_tokens=1),
        request_messages=provider_request(messages),
        estimate_factor=2.0,
        active_context=None if context_tokens is None else RequestInput.measured(context_tokens),
    )

    [target] = [adapter for adapter in (summary, active) if adapter.requests]
    sent = len(target.requests[0]["messages"])
    if strategy == "summary_tail":
        # The Summary Model's own correction applies to its whole request.
        assert target.budgets == [RequestInput.estimate(round((10 * sent + 5) * 1.5))]
    elif context_tokens is None:
        assert target.budgets == [RequestInput.estimate((10 * sent + 5) * 2)]
    else:
        # Chat's measured Context of the active request plus the appended
        # reminder, whose corrected estimate alone carries the margin.
        assert target.budgets == [RequestInput(context_tokens + 10 * 2, True, 10 * 2)]
    # A measured input teaches the target Model's calibration against the
    # uncorrected estimate of exactly the sent request, whatever the budget built on.
    reference = "openai/summary" if strategy == "summary_tail" else "openai/active"
    assert recorder.input_estimates == (
        [] if measured is None else [(reference, measured, 10 * sent + 5)]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "statuses", "failures"),
    [
        (["output_truncated"], ["failed"], ["incomplete"]),
        (["cancelled"], ["cancelled"], [None]),
        (["rejected"], ["failed"], ["provider"]),
        (["dropped", "stop"], ["failed", "completed"], ["network", None]),
        (["dropped", "dropped"], ["failed", "failed"], ["network", "network"]),
    ],
    ids=["truncated", "cancelled", "fatal-provider-error", "dropped-once", "dropped-twice"],
)
async def test_compaction_attempts_retain_usage_and_retry_a_transient_failure_once(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[str],
    statuses: list[str],
    failures: list[str | None],
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
    assert [call["failure_class"] for call in recorder.calls] == failures
    assert all(
        call["usage"] == {"input_tokens": 75, "usage_call_id": call["id"]}
        for call in recorder.calls
    )
    # Every attempt's measured input teaches the calibration, failed or not.
    assert [sample[:2] for sample in recorder.input_estimates] == [("openai/active", 75)] * len(
        statuses
    )
    assert len({sample[2] for sample in recorder.input_estimates}) == 1
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "content",
        "reasoning",
        "retry",
        "backoff",
        "suppressed-backoff",
        "suppressed-cancellation",
        "hot-content",
        "hot-finish",
    ],
)
async def test_compaction_deadline_covers_active_output_and_recovery(monkeypatch, phase):
    budgets: list[asyncio.Timeout] = []
    durations: list[float] = []
    attempts = 0
    closed = 0
    emitted = 0

    def controlled_timeout(seconds: float) -> asyncio.Timeout:
        durations.append(seconds)
        budget = asyncio.timeout(seconds)
        budgets.append(budget)
        return budget

    def expire() -> None:
        budgets[0].reschedule(asyncio.get_running_loop().time())

    async def backoff(delay: float) -> None:
        if phase in {"backoff", "suppressed-backoff"}:
            expire()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if phase != "suppressed-backoff":
                    raise

    class ActiveAdapter(StubAdapter):
        @override
        async def stream(self, messages, **kwargs):
            nonlocal attempts, closed, emitted
            attempts += 1
            try:
                yield {"type": "usage", "input_tokens": 75}
                if phase in {"retry", "backoff", "suppressed-backoff"} and attempts == 1:
                    raise NetworkError("stream dropped")
                try:
                    while True:
                        emitted += 1
                        yield {
                            "type": "reasoning_delta" if phase == "reasoning" else "content_delta",
                            "text": "Working",
                        }
                        if emitted == 2:
                            if phase == "hot-finish":
                                yield {"type": "finish", "reason": "stop"}
                            expire()
                            if phase == "hot-finish":
                                return
                        if not phase.startswith("hot-"):
                            await asyncio.sleep(0)
                except asyncio.CancelledError:
                    if phase != "suppressed-cancellation":
                        raise
                    yield {"type": "finish", "reason": "stop"}
            finally:
                closed += 1

    # Keep the real timeout mechanism, but fire its timer from the controlled
    # stream/backoff instead of waiting seven minutes or changing a global clock.
    monkeypatch.setattr(
        model_request,
        "asyncio",
        SimpleNamespace(
            timeout=controlled_timeout,
            CancelledError=asyncio.CancelledError,
            get_running_loop=asyncio.get_running_loop,
        ),
    )
    monkeypatch.setattr(model_request, "_sleep", backoff)
    recorder = RecordingUsageRecorder()
    adapter = ActiveAdapter()
    messages = [user("u1", "Request"), assistant("a1", "Answer")]
    with pytest.raises(CompactionError) as failure:
        await asyncio.wait_for(
            compact(
                messages,
                service=CompactionService(usage_recorder=cast(Any, recorder)),
                summary_adapter=adapter,
                summary_model_id="active",
                active_adapter=adapter,
                active_model_id="active",
                active_model_reference="openai/active",
                settings=CompactionSettings(strategy="continuation"),
                request_messages=provider_request(messages),
            ),
            2,
        )
    assert failure.value.model == "openai/active"
    assert durations == [420]
    if phase.startswith("hot-"):
        # An immediately-ready iterator never gives the timeout callback a
        # turn. Check the deadline itself, including after its last delta.
        deadline = budgets[0].when()
        assert deadline is not None and deadline <= asyncio.get_running_loop().time()
        assert emitted <= 3
    else:
        assert budgets[0].expired()
    assert closed == attempts == (2 if phase == "retry" else 1)
    assert emitted >= 2 or phase in {"backoff", "suppressed-backoff"}
    assert [call["status"] for call in recorder.calls] == ["failed"] * attempts
    # A dropped stream fails before the deadline; the deadline stops the rest.
    dropped = ["network"] if phase in {"retry", "backoff", "suppressed-backoff"} else []
    limited = [] if phase in {"backoff", "suppressed-backoff"} else ["limit"]
    assert [call["failure_class"] for call in recorder.calls] == dropped + limited
    assert all(call["usage"]["input_tokens"] == 75 for call in recorder.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("characters", "close_failure"), [(100_000, False), (100_001, False), (100_001, True)]
)
async def test_compaction_summary_limit_rejects_without_retry_and_closes(
    monkeypatch, characters, close_failure
):
    closed = 0
    attempts = 0
    waits = []

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    class LargeSummaryAdapter(StubAdapter):
        @override
        async def stream(self, messages, **kwargs):
            nonlocal attempts, closed
            attempts += 1
            try:
                yield {"type": "usage", "input_tokens": 75}
                yield {"type": "content_delta", "text": "S" * 50_000}
                yield {"type": "content_delta", "text": "S" * (characters - 50_000)}
                yield {"type": "finish", "reason": "stop"}
            finally:
                closed += 1
                if close_failure:
                    raise NetworkError("cleanup failed")

    monkeypatch.setattr(model_request, "_sleep", record_wait)
    recorder = RecordingUsageRecorder()
    adapter = LargeSummaryAdapter()
    messages = [user("u1", "Request"), assistant("a1", "Answer")]
    operation = compact(
        messages,
        service=CompactionService(usage_recorder=cast(Any, recorder)),
        summary_adapter=adapter,
        summary_model_id="active",
        active_adapter=adapter,
        active_model_id="active",
        settings=CompactionSettings(strategy="continuation"),
        request_messages=provider_request(messages),
    )
    if characters == 100_000:
        checkpoint = await operation
        assert "S" * characters in str(checkpoint.content)
        expected_status = "completed"
    else:
        with pytest.raises(CompactionError):
            await operation
        expected_status = "failed"
    assert attempts == closed == 1
    assert waits == []
    assert [call["status"] for call in recorder.calls] == [expected_status]
    assert recorder.calls[0]["failure_class"] == (None if characters == 100_000 else "limit")
    assert recorder.calls[0]["usage"]["input_tokens"] == 75
