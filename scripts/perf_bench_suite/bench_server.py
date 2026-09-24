"""Server SSE frame benchmarks.

A completed Run holding the benchmark's events is replayed through the real
``server._streams._sse_run_events`` generator, which the Run event SSE route
streams: per event it awaits the Run subscription in a task, projects the
payload with ``remove_opaque_provider_metadata`` (through ``FileDelivery``) and
serializes the frame. Replaying a completed Run keeps every operation identical;
a live subscriber runs the same per-event code.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.runs import ASSISTANT_OUTPUT_DELTA_EVENT, TOOL_CALL_RESULT_EVENT, Run
from core.tools import tool_success
from scripts.perf_bench_suite.fixtures import AGENT_ID, TextFactory
from scripts.perf_bench_suite.runner import BenchContext, Benchmark, Prepared
from server._streams import _sse_run_events
from server.file_delivery import FileDelivery

DELTA_EVENTS = 1000
DELTA_CHARS = 64
TOOL_RESULT_EVENTS = 20
TOOL_RESULT_OUTPUT_CHARS = 64 * 1024

EventSpec = tuple[str, dict[str, Any]]


def _delta_events() -> list[EventSpec]:
    text = TextFactory(29)
    return [
        (ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": text.words(DELTA_CHARS)})
        for _ in range(DELTA_EVENTS)
    ]


def _tool_result_events() -> list[EventSpec]:
    text = TextFactory(31)
    events: list[EventSpec] = []
    for index in range(TOOL_RESULT_EVENTS):
        events.append(
            (
                TOOL_CALL_RESULT_EVENT,
                {
                    "assistant_message_id": f"msg-assistant-{index:03d}",
                    "tool_call": {"id": f"call-{index:03d}", "index": 0, "name": "bash"},
                    "result": tool_success(
                        {"exit_code": 0, "output": text.code(TOOL_RESULT_OUTPUT_CHARS)}
                    ),
                    "display": {"title": "Run tests", "subtitle": "python -m pytest -q"},
                    "timing": {
                        "started_at": "2026-01-05T09:00:00+00:00",
                        "completed_at": "2026-01-05T09:00:02+00:00",
                        "duration_ms": 2000,
                    },
                    "schema_fingerprint": "0" * 64,
                    "error_code": None,
                },
            )
        )
    return events


def _replay_setup(
    build_events: Callable[[], list[EventSpec]], item_unit: str
) -> Callable[[BenchContext], Prepared]:
    def setup(context: BenchContext) -> Prepared:
        events = build_events()
        delivery = FileDelivery(secret=b"perf-bench-file-delivery")

        async def completed_run() -> Run:
            run = Run(run_id="run-sse", agent_id=AGENT_ID, session_id="perf-session")
            for event_type, payload in events:
                run.emit(event_type, payload)
            run.mark_completed(None)
            return run

        run = context.run(completed_run())

        async def frames() -> tuple[int, int]:
            count = 0
            size = 0
            async for frame in _sse_run_events(run, file_delivery=delivery):
                count += 1
                size += len(frame)
            return count, size

        async def replay() -> None:
            async for _frame in _sse_run_events(run, file_delivery=delivery):
                pass

        frame_count, frame_chars = context.run(frames())
        if frame_count != len(events) + 1:
            raise RuntimeError(f"expected {len(events) + 1} SSE frames, got {frame_count}")
        return Prepared(
            replay,
            params={"frames": frame_count, "frame_chars": frame_chars},
            items=frame_count,
            item_unit=item_unit,
        )

    return setup


BENCHMARKS = (
    Benchmark(
        name="server.sse_frame[delta]",
        description=(
            f"_sse_run_events replay of {DELTA_EVENTS} assistant_output_delta events "
            f"({DELTA_CHARS} chars each) plus run_completed."
        ),
        setup=_replay_setup(_delta_events, "frame"),
    ),
    Benchmark(
        name="server.sse_frame[tool_result_64k]",
        description=(
            f"_sse_run_events replay of {TOOL_RESULT_EVENTS} tool_call_result events, each "
            "carrying a 64 KB command output, plus run_completed."
        ),
        setup=_replay_setup(_tool_result_events, "frame"),
    ),
)
