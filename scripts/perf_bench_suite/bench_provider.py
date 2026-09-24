"""Provider stream parsing benchmarks for the OpenAI-compatible Adapter.

Each operation runs the real ``OpenAICompatibleAdapter.stream()`` end to end
(payload build, httpx request, SSE line splitting, JSON parsing and chunk
normalization) against an in-memory ``httpx.MockTransport`` whose body yields
one SSE event per network chunk. The request itself is one short user message,
so the cost is dominated by the ~2000 streamed chunks.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from core.chat.streaming import iter_with_chunk_timeout
from scripts.perf_bench_suite.fixtures import (
    MODEL_ID,
    STREAM_CHUNKS,
    content_stream_events,
    offline_adapter,
    tool_call_stream_events,
)
from scripts.perf_bench_suite.runner import BenchContext, Benchmark, Prepared

_REQUEST_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "Write the plan."}]


def _stream_setup(
    build_events: Callable[[], Sequence[bytes]],
    expected_type: str,
    *,
    chunk_timeout: bool = False,
) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        events = tuple(build_events())
        adapter = context.run(offline_adapter(lambda: events))
        context.add_cleanup(adapter.aclose)

        def open_stream() -> AsyncIterator[dict[str, Any]]:
            stream = adapter.stream(_REQUEST_MESSAGES, model_id=MODEL_ID)
            return iter_with_chunk_timeout(stream) if chunk_timeout else stream

        async def consume() -> None:
            async for _delta in open_stream():
                pass

        async def count_types() -> Counter[str]:
            return Counter([str(delta.get("type")) async for delta in open_stream()])

        counts = context.run(count_types())
        if counts[expected_type] < STREAM_CHUNKS:
            raise RuntimeError(
                f"expected at least {STREAM_CHUNKS} {expected_type} deltas from the fixture "
                f"stream, got {dict(counts)}"
            )
        return Prepared(
            consume,
            params={
                "chunks": STREAM_CHUNKS,
                "sse_events": len(events),
                "body_bytes": sum(len(event) for event in events),
                "chunk_timeout": chunk_timeout,
            },
            items=STREAM_CHUNKS,
            item_unit="chunk",
        )

    return setup


BENCHMARKS = (
    Benchmark(
        name="provider.stream_parse[content_2000]",
        description=(
            "OpenAICompatibleAdapter.stream over an in-memory SSE body of 2000 content "
            "chunks plus role, finish, usage and [DONE] events."
        ),
        setup=_stream_setup(content_stream_events, "content_delta"),
    ),
    Benchmark(
        name="provider.stream_parse[content_2000,chunk_timeout]",
        description=(
            "The content_2000 stream consumed through Chat's iter_with_chunk_timeout "
            "wrapper (asyncio.wait_for per chunk), as the Chat request runner does."
        ),
        setup=_stream_setup(content_stream_events, "content_delta", chunk_timeout=True),
    ),
    Benchmark(
        name="provider.stream_parse[tool_args_2000]",
        description=(
            "OpenAICompatibleAdapter.stream over one Tool Call whose JSON arguments "
            "arrive in 2000 fragments."
        ),
        setup=_stream_setup(tool_call_stream_events, "tool_call_delta"),
    ),
)
