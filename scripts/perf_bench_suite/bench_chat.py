"""Chat benchmarks: request building and streaming delta handling.

Request building is split into the stages Chat runs for every Model request
against an ``openai_compatible`` Model:

* ``chat.request_transform`` - canonical history to request messages, the CPU
  steps ``RequestBuilder.build_request_state`` runs in its transform workers
  (``_prepare_request_messages``, ``_request_content_resolution_inputs`` and
  ``limit_request_images``).
* ``chat.request_wire`` - request messages plus Tool definitions to the
  provider payload, as ``OpenAICompatibleAdapter.stream()`` builds it on the
  Event Loop without a prepared Context budget (``_build_payload`` including
  its full-request token estimate, then ``_prepare_stream_payload``).
* ``chat.request_wire_scoped`` - the same payload construction with Chat's
  already-prepared Context budget, as ordinary Agentic requests send it.
* ``chat.request_encode`` - that payload to the HTTP request body with httpx,
  the same encoding the client performs before sending.
* ``chat.estimate_tokens`` - the Adapter's request token estimate alone, with
  a warm and a cleared token-count cache.

Streaming benchmarks feed ``StreamingDeltaBatcher`` and
``StreamingAccumulator`` with deltas in the shapes the Adapter produces.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

import core.utils.tokens as token_estimation
from core.chat._request_history import (
    _prepare_request_messages,
    _request_content_resolution_inputs,
)
from core.chat.streaming import (
    StreamingAccumulator,
    StreamingDeltaBatcher,
    StreamingVisibleDelta,
)
from core.chat.wire_shaping import limit_request_images
from core.providers.adapter import request_input_budget
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.runs import ASSISTANT_OUTPUT_DELTA_EVENT
from scripts.perf_bench_suite.fixtures import (
    AGENT_MODEL,
    BASE_URL,
    HISTORY_SHAPES,
    MODEL_ID,
    STREAM_CHUNKS,
    HistoryShape,
    TextFactory,
    content_stream_events,
    loaded_history,
    offline_adapter,
    shared_adapter,
    system_prompt,
    tool_definitions,
)
from scripts.perf_bench_suite.runner import BenchContext, Benchmark, Prepared

_CHAT_COMPLETIONS_URL = f"{BASE_URL}/chat/completions"


@dataclass(frozen=True)
class _RequestInputs:
    adapter: OpenAICompatibleAdapter
    system_prompt: str
    tools: list[dict[str, Any]]
    request_messages: list[dict[str, Any]]
    history_messages: int
    history_chars: int

    def build_payload(self) -> dict[str, Any]:
        payload = self.adapter._build_payload(
            self.request_messages,
            MODEL_ID,
            temperature=None,
            top_p=None,
            thinking_effort=None,
            tools=self.tools,
        )
        self.adapter._prepare_stream_payload(payload)
        return payload

    def params(self) -> dict[str, Any]:
        return {
            "messages": self.history_messages,
            "content_chars": self.history_chars,
            "tools": len(self.tools),
        }


def _transform(
    adapter: OpenAICompatibleAdapter, prompt: str, messages: list[Any]
) -> list[dict[str, Any]]:
    prepared = _prepare_request_messages(
        system_prompt=prompt,
        agent_model=AGENT_MODEL,
        session_messages=messages,
        replay_policy=adapter.reasoning_replay_policy(MODEL_ID),
        reasoning_scope_model=AGENT_MODEL,
    )
    _request_content_resolution_inputs(prepared.effective_messages, messages)
    return limit_request_images(prepared.messages, budget=None)


def _request_inputs(context: BenchContext, shape: HistoryShape) -> _RequestInputs:
    def build() -> _RequestInputs:
        adapter = shared_adapter(context)
        history = loaded_history(context, shape)
        prompt = context.fixture("system-prompt", system_prompt)
        tools = context.fixture("tool-definitions", tool_definitions)
        return _RequestInputs(
            adapter=adapter,
            system_prompt=prompt,
            tools=tools,
            request_messages=_transform(adapter, prompt, history.messages),
            history_messages=len(history.messages),
            history_chars=history.content_chars,
        )

    return context.fixture(f"request-inputs:{shape.label}", build)


def _transform_setup(shape: HistoryShape) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        inputs = _request_inputs(context, shape)
        messages = loaded_history(context, shape).messages

        def transform() -> None:
            _transform(inputs.adapter, inputs.system_prompt, messages)

        return Prepared(
            transform,
            params={**inputs.params(), "system_prompt_chars": len(inputs.system_prompt)},
        )

    return setup


def _wire_setup(shape: HistoryShape) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        inputs = _request_inputs(context, shape)
        inputs.build_payload()
        return Prepared(inputs.build_payload, params=inputs.params())

    return setup


def _scoped_wire_setup(shape: HistoryShape) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        inputs = _request_inputs(context, shape)
        tokens = inputs.adapter.estimate_request_input_tokens(
            inputs.request_messages, model_id=MODEL_ID, tools=inputs.tools
        )
        standalone_payload = inputs.build_payload()

        def build_with_budget() -> dict[str, Any]:
            with request_input_budget(MODEL_ID, tokens):
                return inputs.build_payload()

        if build_with_budget() != standalone_payload:
            raise AssertionError("A prepared Context budget must preserve the request payload")
        return Prepared(
            build_with_budget,
            params={**inputs.params(), "input_budget_tokens": tokens},
        )

    return setup


def _encode_setup(shape: HistoryShape) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        inputs = _request_inputs(context, shape)
        payload = inputs.build_payload()
        body = httpx.Request("POST", _CHAT_COMPLETIONS_URL, json=payload).content

        def encode() -> None:
            httpx.Request("POST", _CHAT_COMPLETIONS_URL, json=payload).content  # noqa: B018

        return Prepared(encode, params={**inputs.params(), "body_bytes": len(body)})

    return setup


def _clear_token_count_cache() -> None:
    with token_estimation._CACHE_LOCK:
        token_estimation._COUNT_CACHE.clear()


def _estimate_setup(shape: HistoryShape, *, cold: bool) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        inputs = _request_inputs(context, shape)
        adapter = inputs.adapter

        def estimate() -> None:
            if cold:
                _clear_token_count_cache()
            adapter.estimate_request_input_tokens(
                inputs.request_messages, model_id=MODEL_ID, tools=inputs.tools
            )

        estimate()
        tokens = adapter.estimate_request_input_tokens(
            inputs.request_messages, model_id=MODEL_ID, tools=inputs.tools
        )
        return Prepared(
            estimate,
            params={
                **inputs.params(),
                "estimated_tokens": tokens,
                "cache": "cold" if cold else "warm",
            },
        )

    return setup


# --- streaming deltas -------------------------------------------------------


def _visible_deltas(count: int) -> tuple[StreamingVisibleDelta, ...]:
    text = TextFactory(23)
    return tuple(
        StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": text.words(text.randint(3, 9)) + " "},
        )
        for _ in range(count)
    )


def _batching_setup(step_seconds: float) -> Callable[[BenchContext], Prepared]:
    """Feed deltas ``step_seconds`` apart on a synthetic clock (0 = one burst)."""

    def setup(_context: BenchContext) -> Prepared:
        deltas = _visible_deltas(STREAM_CHUNKS)
        start = 1000.0

        def batch() -> int:
            batcher = StreamingDeltaBatcher()
            emitted = 0
            now = start
            for delta in deltas:
                emitted += len(batcher.add(delta, now=now))
                now += step_seconds
            return emitted + len(batcher.flush(now=now))

        return Prepared(
            batch,
            params={
                "deltas": STREAM_CHUNKS,
                "emitted_events": batch(),
                "step_ms": step_seconds * 1000,
            },
            items=STREAM_CHUNKS,
            item_unit="delta",
        )

    return setup


def _accumulate_setup(context: BenchContext) -> Prepared:
    async def collect() -> list[dict[str, Any]]:
        events = content_stream_events()
        adapter = await offline_adapter(lambda: events)
        try:
            return [
                delta
                async for delta in adapter.stream(
                    [{"role": "user", "content": "Write the plan."}], model_id=MODEL_ID
                )
            ]
        finally:
            await adapter.aclose()

    deltas = context.run(collect())

    def accumulate() -> None:
        accumulator = StreamingAccumulator()
        for delta in deltas:
            accumulator.add_delta(delta)
        accumulator.finalize_assistant_fields()

    return Prepared(
        accumulate,
        params={"provider_deltas": len(deltas)},
        items=len(deltas),
        item_unit="delta",
    )


BENCHMARKS = (
    *(
        Benchmark(
            name=f"chat.request_transform[{shape.label}]",
            description=(
                "Canonical history to request messages: _prepare_request_messages, "
                "_request_content_resolution_inputs and limit_request_images."
            ),
            setup=_transform_setup(shape),
        )
        for shape in HISTORY_SHAPES
    ),
    *(
        Benchmark(
            name=f"chat.request_wire[{shape.label}]",
            description=(
                "Request messages and Tool definitions to the OpenAI-compatible stream "
                "payload: _build_payload (with its request token estimate) and "
                "_prepare_stream_payload."
            ),
            setup=_wire_setup(shape),
        )
        for shape in HISTORY_SHAPES
    ),
    *(
        Benchmark(
            name=f"chat.request_wire_scoped[{shape.label}]",
            description=(
                "OpenAI-compatible stream payload with Chat's already-prepared Context "
                "budget; the full estimate is prepared outside the timed operation."
            ),
            setup=_scoped_wire_setup(shape),
        )
        for shape in HISTORY_SHAPES
    ),
    *(
        Benchmark(
            name=f"chat.request_encode[{shape.label}]",
            description="Stream payload to the HTTP request body via httpx.Request(json=...).",
            setup=_encode_setup(shape),
        )
        for shape in HISTORY_SHAPES
    ),
    *(
        Benchmark(
            name=f"chat.estimate_tokens[{cache},{shape.label}]",
            description=(
                "OpenAICompatibleAdapter.estimate_request_input_tokens over the request; "
                + (
                    "the process-wide token-count cache is cleared before every call "
                    "(what an evicted history costs)."
                    if cache == "cold"
                    else "every per-message count is already cached."
                )
            ),
            setup=_estimate_setup(shape, cold=cache == "cold"),
        )
        for cache in ("warm", "cold")
        for shape in HISTORY_SHAPES
    ),
    Benchmark(
        name="chat.delta_batching[paced]",
        description=(
            "StreamingDeltaBatcher.add for 2000 content deltas arriving 2 ms apart "
            "(40 ms emit window), then flush."
        ),
        setup=_batching_setup(0.002),
    ),
    Benchmark(
        name="chat.delta_batching[burst]",
        description=(
            "StreamingDeltaBatcher.add for 2000 content deltas inside one emit window "
            "(a blocked Event Loop catching up), merged into one pending delta, then flush."
        ),
        setup=_batching_setup(0.0),
    ),
    Benchmark(
        name="chat.stream_accumulate[content_2000]",
        description=(
            "StreamingAccumulator.add_delta for the Adapter's normalized deltas of the "
            "content_2000 stream, then finalize_assistant_fields."
        ),
        setup=_accumulate_setup,
    ),
)
