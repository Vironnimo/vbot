"""The Compaction Model call: canonical stream acceptance, usage accounting and stall bounds."""

from __future__ import annotations

import asyncio
import copy
import json
import random
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast, override

import httpx
import pytest
import respx

import core.compaction._model_request as model_request
from core.chat.streaming import (
    StreamingChunkTimeoutError,
    StreamingProgressTimeoutError,
    iter_with_chunk_timeout,
)
from core.chat.wire_shaping import system_reminder_request_message
from core.compaction import CompactionError, CompactionService, CompactionSettings
from core.compaction._model_request import _send_streaming_model_request
from core.compaction.compaction import COMPACTION_REFERENCE_PREFIX, COMPACTION_SUMMARY_END_MARKER
from core.providers.adapter import is_request_context_message, resolve_request_input_budget
from core.providers.anthropic import AnthropicAdapter
from core.providers.errors import NetworkError, ProviderContextOverflowError
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
    tool_step,
    user,
)
from tests.core.providers import openai_compatible_test_support as compatible


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
        (["overflow"], ["failed"], ["rejected"]),
        (["dropped", "stop"], ["failed", "completed"], ["network", None]),
        (["dropped", "dropped"], ["failed", "failed"], ["network", "network"]),
    ],
    ids=[
        "truncated",
        "cancelled",
        "fatal-provider-error",
        "unreducible-context-overflow",
        "dropped-once",
        "dropped-twice",
    ],
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
            if outcome == "overflow":
                raise ProviderContextOverflowError("context rejected")
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
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
@pytest.mark.parametrize(
    ("outcomes", "earlier_user_characters"),
    [
        (["stop"], 0),
        (["overflow", "stop"], 0),
        (["overflow", "stop"], 80_000),
        (["overflow", "overflow", "overflow", "stop"], 0),
        (["overflow", "overflow", "overflow", "overflow"], 0),
        (["overflow", "dropped", "overflow", "stop"], 0),
        (["overflow", "dropped", "dropped"], 0),
    ],
    ids=[
        "unchanged",
        "reduced",
        "observations-before-earlier-user-input",
        "three-reductions",
        "bounded",
        "shared-recovery",
        "transient-budget",
    ],
)
async def test_compaction_reduces_only_rejected_requests_and_preserves_tool_cycles(
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    outcomes: list[str],
    earlier_user_characters: int,
) -> None:
    waits = []

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    class RejectedAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__("SUMMARY")
            self.budgets: list[RequestInput] = []
            self.estimates: list[int] = []

        def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
            return sum(len(str(item.get("content", ""))) // 4 + 1 for item in messages) + 5 * len(
                tools or []
            )

        @override
        async def stream(self, messages, **kwargs):
            self.requests.append({"messages": messages, **kwargs})
            self.budgets.append(resolve_request_input_budget(kwargs["model_id"], -1))
            self.estimates.append(
                self.estimate_request_input_tokens(
                    messages, model_id=kwargs["model_id"], tools=kwargs.get("tools")
                )
            )
            yield {"type": "usage", "input_tokens": self.estimates[-1]}
            outcome = outcomes[len(self.requests) - 1]
            if outcome == "overflow":
                raise ProviderContextOverflowError("context rejected")
            if outcome == "dropped":
                raise NetworkError("stream dropped")
            yield {"type": "content_delta", "text": self.text}
            yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr(model_request, "_sleep", record_wait)
    adapter = RejectedAdapter()
    recorder = RecordingUsageRecorder()
    messages = [
        user("u1", "USER SENTINEL" + "u" * earlier_user_characters),
        *tool_step("large", "HEAD SENTINEL\n" + "x" * 20_000 + "\nTAIL SENTINEL"),
        assistant("answer", "ANSWER SENTINEL"),
        user("u2", "LATEST USER SENTINEL"),
    ]
    original = copy.deepcopy([item.to_dict() for item in messages])
    request = provider_request(messages)
    request_before = copy.deepcopy(request)
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    operation = compact(
        messages,
        service=CompactionService(usage_recorder=cast(Any, recorder)),
        summary_adapter=adapter,
        summary_model_id="model",
        summary_model_reference="openai/model",
        active_adapter=adapter,
        active_model_id="model",
        active_model_reference="openai/model",
        active_tools=tools,
        estimate_factor=1.5,
        settings=CompactionSettings(strategy=strategy, tail_tokens=1),
        request_messages=request,
    )
    if outcomes[-1] == "stop":
        checkpoint = await operation
        assert checkpoint.usage is not None
        assert checkpoint.usage["model_call"]["usage"]["usage_call_id"] == recorder.calls[-1]["id"]
    else:
        with pytest.raises(CompactionError):
            await operation

    assert len(adapter.requests) == len(outcomes)
    assert request == request_before
    assert [item.to_dict() for item in messages] == original
    first = adapter.requests[0]["messages"]
    # The initial request keeps the cache-friendly native prefix unchanged.
    assert first[:-1] == request_before[: len(first) - 1]
    latest = next(
        item
        for item in reversed(first)
        if item["role"] == "user" and not is_request_context_message(item)
    )
    for sent in adapter.requests:
        assert latest in sent["messages"]
        if earlier_user_characters:
            # Even when clipping observations alone misses a sizing target,
            # do not discard earlier User requirements at the first level.
            assert first[1] in sent["messages"]
        assert sent["tools"] == tools
        [call] = [item for item in sent["messages"] if item.get("tool_calls")]
        [result] = [item for item in sent["messages"] if item["role"] == "tool"]
        assert result["tool_call_id"] == call["tool_calls"][0]["id"] == "call-large"
        assert result["name"] == "read"
        assert "HEAD SENTINEL" in result["content"] and "TAIL SENTINEL" in result["content"]
    assert adapter.budgets == [
        RequestInput.estimate(round(count * 1.5)) for count in adapter.estimates
    ]
    expected_statuses = ["completed" if outcome == "stop" else "failed" for outcome in outcomes]
    assert [call["status"] for call in recorder.calls] == expected_statuses
    assert recorder.input_estimates == [
        ("openai/model", count, count) for count in adapter.estimates
    ]
    for previous, current, outcome in zip(
        adapter.estimates, adapter.estimates[1:], outcomes, strict=False
    ):
        assert current < previous if outcome == "overflow" else current == previous
    assert len(waits) == (1 if "dropped" in outcomes else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
@pytest.mark.parametrize(
    "text", ["Summary", "", _MARKERS_ONLY], ids=["summary", "empty", "framing-only"]
)
async def test_compaction_omits_only_old_complete_exchanges_after_context_rejection(strategy, text):
    class RejectedAdapter(StubAdapter):
        @override
        async def stream(self, messages, **kwargs):
            self.requests.append({"messages": messages, **kwargs})
            if len(self.requests) == 1:
                raise ProviderContextOverflowError("context rejected")
            yield {"type": "content_delta", "text": text}
            yield {"type": "finish", "reason": "stop"}

    messages = [
        user("old", "OLD USER SENTINEL " + "a" * 30_000),
        *tool_step("old", "old observation"),
        assistant("old-answer", "old answer"),
        user("latest", "LATEST USER SENTINEL"),
        *tool_step("latest", "LATEST OBSERVATION SENTINEL"),
        assistant("latest-answer", "latest answer"),
    ]
    request = provider_request(messages)
    prior_checkpoint = system_reminder_request_message("[compaction-summary] PRIOR SENTINEL")
    request.insert(1, prior_checkpoint)
    before = copy.deepcopy(request)
    adapter = RejectedAdapter()
    operation = compact(
        messages,
        summary_adapter=adapter,
        summary_model_id="model",
        active_adapter=adapter,
        active_model_id="model",
        request_messages=request,
        settings=CompactionSettings(strategy=strategy, tail_tokens=1),
    )
    if text == "Summary":
        checkpoint = await operation
        # The limitation is deterministic checkpoint context, not just a suggestion
        # the summary Model could forget to carry forward.
        assert str(checkpoint.content).index("Summary") > str(checkpoint.content).index("Session")
    else:
        with pytest.raises(CompactionError):
            await operation
    assert len(adapter.requests) == 2
    first, reduced = [item["messages"] for item in adapter.requests]
    assert reduced[:2] == first[:2] == before[:2]
    assert [item["id"] for item in reduced if "id" in item] == [
        "system-1",
        *(
            item["id"]
            for item in first
            if item.get("id") in {"latest", "a-latest", "t-latest", "latest-answer"}
        ),
    ]
    assert reduced[-1] == first[-1]
    assert is_request_context_message(reduced[-2])
    assert request == before


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
@pytest.mark.parametrize("context_window", [4096, 1024, 128])
async def test_compaction_recovers_remote_overflow_after_the_adapter_learns_a_smaller_window(
    strategy,
    context_window,
):
    adapter = compatible.make_adapter(model=compatible.catalog_model(reasoning=False))
    text = "".join(random.Random(0).choices("abcdefghijklmnopqrstuvwxyz", k=30_000))
    messages = [user("u1", "REQUEST"), *tool_step("large", text), assistant("a1", "answer")]
    try:
        with respx.mock:
            route = respx.post(compatible.OPENAI_URL).mock(
                side_effect=[
                    httpx.Response(
                        400,
                        json={
                            "error": {
                                "code": "context_length_exceeded",
                                "message": "This model's maximum context length is "
                                f"{context_window} tokens. "
                                "However, you requested 20000 tokens.",
                            }
                        },
                    ),
                    compatible.sse_response(
                        compatible.sse(
                            {
                                "choices": [
                                    {
                                        "delta": {"content": "SUMMARY"},
                                        "finish_reason": "stop",
                                    }
                                ]
                            }
                        )
                    ),
                ]
            )
            operation = compact(
                messages,
                summary_adapter=adapter,
                summary_model_id=compatible.MODEL_ID,
                active_adapter=adapter,
                active_model_id=compatible.MODEL_ID,
                request_messages=provider_request(messages),
                settings=CompactionSettings(strategy=strategy, tail_tokens=1),
            )
            if context_window == 128:
                # No candidate fits even after exhausting all observation caps.
                # Do not make another doomed Adapter/HTTP attempt.
                with pytest.raises(CompactionError) as failure:
                    await operation
                assert isinstance(failure.value.__cause__, ProviderContextOverflowError)
                assert route.call_count == 1
                return
            checkpoint = await operation
        assert checkpoint.role == "compaction_checkpoint"
        assert route.call_count == 2
        assert adapter.reported_context_window(compatible.MODEL_ID) == context_window
        first, second = [json.loads(call.request.content) for call in route.calls]
        [original] = [item["content"] for item in first["messages"] if item["role"] == "tool"]
        [reduced] = [item["content"] for item in second["messages"] if item["role"] == "tool"]
        assert len(reduced) < len(original)
        assert messages[2].content == text
    finally:
        await adapter.aclose()


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
        "overflow",
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
                if phase == "overflow" and attempts == 1:
                    raise ProviderContextOverflowError("context rejected")
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
    if phase == "overflow":
        messages[1:1] = tool_step("large", "observation " * 1000)
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
    assert closed == attempts == (2 if phase in {"retry", "overflow"} else 1)
    assert emitted >= 2 or phase in {"backoff", "suppressed-backoff"}
    assert [call["status"] for call in recorder.calls] == ["failed"] * attempts
    # A dropped stream fails before the deadline; the deadline stops the rest.
    dropped = ["network"] if phase in {"retry", "backoff", "suppressed-backoff"} else []
    if phase == "overflow":
        dropped = ["rejected"]
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
    text = "".join(random.Random(0).choices("abcdefghijklmnopqrstuvwxyz", k=characters))

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    class LargeSummaryAdapter(StubAdapter):
        @override
        async def stream(self, messages, **kwargs):
            nonlocal attempts, closed
            attempts += 1
            try:
                yield {"type": "usage", "input_tokens": 75}
                yield {"type": "content_delta", "text": text[:50_000]}
                yield {"type": "content_delta", "text": text[50_000:]}
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
        assert text in str(checkpoint.content)
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
