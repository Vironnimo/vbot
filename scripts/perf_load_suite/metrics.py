"""Client-side measurements and their per-level analysis.

All timestamps are wall-clock ``time.time()`` seconds. The harness, the fake
Provider and vBot run on the same machine, so client and fake-Provider times
share one clock and can be subtracted directly.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from scripts.perf_load_suite.directive import PerfDirective
from scripts.perf_load_suite.fake_provider import MARKER_PATTERN

MAX_REPORTED_ERRORS = 10


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Linear-interpolated percentile (``quantile`` in 0..100); ``None`` when empty."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * quantile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    """Summarize samples as count/mean/p50/p95/p99/max (milliseconds or ratios)."""
    samples = [float(value) for value in values]
    if not samples:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(samples),
        "mean": _round(sum(samples) / len(samples)),
        "p50": _round(percentile(samples, 50)),
        "p95": _round(percentile(samples, 95)),
        "p99": _round(percentile(samples, 99)),
        "max": _round(max(samples)),
    }


def marker_latencies_ms(text: str, received_at: float) -> list[float]:
    """Latency of every timing marker in one received delta."""
    return [
        (received_at - float(match.group(1))) * 1000.0 for match in MARKER_PATTERN.finditer(text)
    ]


@dataclass(frozen=True)
class ToolTiming:
    """One Tool execution reported by vBot's ``tool_call_result`` event."""

    call_id: str
    name: str
    duration_ms: float | None
    ok: bool


@dataclass
class RunRecord:
    """Client-side view of one turn (one Run) sent by the load driver."""

    tag: str
    session_index: int
    turn_index: int
    agent_id: str
    session_id: str
    directive: PerfDirective
    sent_at: float
    accepted_at: float | None = None
    run_id: str | None = None
    first_event_at: float | None = None
    first_delta_at: float | None = None
    finished_at: float | None = None
    status: str = "pending"
    error: str | None = None
    delta_events: int = 0
    delta_latencies_ms: list[float] = field(default_factory=list)
    tool_timings: list[ToolTiming] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    @property
    def tool_errors(self) -> int:
        return sum(1 for timing in self.tool_timings if not timing.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "session_index": self.session_index,
            "turn_index": self.turn_index,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "status": self.status,
            "error": self.error,
            "sent_at": self.sent_at,
            "accepted_at": self.accepted_at,
            "first_event_at": self.first_event_at,
            "first_delta_at": self.first_delta_at,
            "finished_at": self.finished_at,
            "delta_events": self.delta_events,
            "markers": len(self.delta_latencies_ms),
            "tool_calls": len(self.tool_timings),
            "tool_errors": self.tool_errors,
        }


@dataclass(frozen=True)
class StepGap:
    """vBot's gap between finishing a Tool-call response and the next request."""

    tag: str
    round_index: int
    tools: tuple[str, ...]
    gap_ms: float
    tool_exec_ms: float | None


def step_gaps(
    requests: Sequence[Mapping[str, Any]],
    tool_durations: Mapping[str, float | None] | None = None,
) -> list[StepGap]:
    """Compute per-round step overhead from fake-Provider request records.

    For every Tool-call response ``k`` of one tag, the gap is the arrival of
    request ``k + 1`` minus the completion of response ``k``: Tool execution,
    persistence, request building and scheduling inside vBot. When vBot's own
    Tool durations are known (keyed by Tool-call id), the slowest parallel call
    of that round is attached so callers can separate Tool time from the rest.
    """
    durations = tool_durations or {}
    by_tag: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for request in requests:
        tag = request.get("tag")
        if isinstance(tag, str) and request.get("kind") in {"tool_calls", "text"}:
            by_tag[tag].append(request)
    gaps: list[StepGap] = []
    for tag, tagged in by_tag.items():
        ordered = sorted(tagged, key=lambda request: float(request["arrival"]))
        for current, following in zip(ordered, ordered[1:], strict=False):
            completed = current.get("completed")
            if current.get("kind") != "tool_calls" or completed is None:
                continue
            call_times = [durations.get(call_id) for call_id in current.get("tool_call_ids", [])]
            known = [value for value in call_times if value is not None]
            gaps.append(
                StepGap(
                    tag=tag,
                    round_index=int(current.get("round", 0)),
                    tools=tuple(current.get("tool_names", [])),
                    gap_ms=(float(following["arrival"]) - float(completed)) * 1000.0,
                    tool_exec_ms=max(known) if known and len(known) == len(call_times) else None,
                )
            )
    return gaps


def retried_requests(requests: Sequence[Mapping[str, Any]]) -> int:
    """Count scripted requests vBot sent more than once for the same round."""
    seen: dict[tuple[str, int], int] = defaultdict(int)
    for request in requests:
        tag = request.get("tag")
        if isinstance(tag, str) and request.get("kind") in {"tool_calls", "text"}:
            seen[(tag, int(request.get("round", 0)))] += 1
    return sum(count - 1 for count in seen.values() if count > 1)


def analyze_level(
    runs: Sequence[RunRecord],
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate one concurrency level's client and fake-Provider measurements."""
    ok_runs = [run for run in runs if run.ok]
    first_arrival: dict[str, float] = {}
    for request in requests:
        tag = request.get("tag")
        if isinstance(tag, str) and request.get("kind") in {"tool_calls", "text"}:
            arrival = float(request["arrival"])
            first_arrival[tag] = min(arrival, first_arrival.get(tag, arrival))

    tool_durations = {
        timing.call_id: timing.duration_ms for run in runs for timing in run.tool_timings
    }
    gaps = step_gaps(requests, tool_durations)
    gaps_by_tool: dict[str, list[StepGap]] = defaultdict(list)
    for gap in gaps:
        gaps_by_tool["+".join(gap.tools) or "?"].append(gap)

    ttft = [
        (run.first_delta_at - run.sent_at) * 1000.0
        for run in ok_runs
        if run.first_delta_at is not None
    ]
    ttft_overhead = [
        (run.first_delta_at - run.sent_at) * 1000.0 - run.directive.think_ms
        for run in ok_runs
        if run.first_delta_at is not None
    ]
    admission = [
        (first_arrival[run.tag] - run.sent_at) * 1000.0 for run in runs if run.tag in first_arrival
    ]
    durations = [
        (run.finished_at - run.sent_at) * 1000.0 for run in ok_runs if run.finished_at is not None
    ]
    excess = [
        (run.finished_at - run.sent_at) * 1000.0 - run.directive.scripted_text_ms()
        for run in ok_runs
        if run.finished_at is not None
    ]
    ratio = [
        (run.finished_at - run.sent_at) * 1000.0 / run.directive.scripted_text_ms()
        for run in ok_runs
        if run.finished_at is not None and run.directive.scripted_text_ms() > 0
    ]
    status_counts: dict[str, int] = defaultdict(int)
    for run in runs:
        status_counts[run.status] += 1

    scripted = [request for request in requests if request.get("kind") in {"tool_calls", "text"}]
    run_errors = sorted({f"{run.status}: {run.error}" for run in runs if not run.ok})
    return {
        "runs": {
            "total": len(runs),
            "ok": len(ok_runs),
            "failed": len(runs) - len(ok_runs),
            "by_status": dict(sorted(status_counts.items())),
            "tool_calls": sum(len(run.tool_timings) for run in runs),
            "tool_errors": sum(run.tool_errors for run in runs),
        },
        "ttft_ms": distribution(ttft),
        "ttft_overhead_ms": distribution(ttft_overhead),
        "admission_ms": distribution(admission),
        "step_overhead_ms": distribution(gap.gap_ms for gap in gaps),
        "step_overhead_excl_tool_ms": distribution(
            gap.gap_ms - gap.tool_exec_ms for gap in gaps if gap.tool_exec_ms is not None
        ),
        "step_overhead_by_tool_ms": {
            tools: {
                "gap": distribution(gap.gap_ms for gap in tool_gaps),
                "tool_exec": distribution(
                    gap.tool_exec_ms for gap in tool_gaps if gap.tool_exec_ms is not None
                ),
            }
            for tools, tool_gaps in sorted(gaps_by_tool.items())
        },
        "errors": run_errors[:MAX_REPORTED_ERRORS],
        "delta_latency_ms": distribution(
            latency for run in runs for latency in run.delta_latencies_ms
        ),
        "delta_events_per_run": distribution(float(run.delta_events) for run in ok_runs),
        "run_duration_ms": distribution(durations),
        "run_ideal_ms": distribution(run.directive.scripted_text_ms() for run in ok_runs),
        "run_excess_ms": distribution(excess),
        "run_duration_ratio": distribution(ratio),
        "provider": {
            "requests": len(requests),
            "scripted_requests": len(scripted),
            "aux_requests": sum(1 for request in requests if request.get("kind") == "aux"),
            "error_requests": sum(1 for request in requests if request.get("kind") == "error"),
            "disconnected": sum(1 for request in requests if request.get("disconnected")),
            "retried_requests": retried_requests(requests),
            "max_emit_lag_ms": _round(
                max(
                    (float(request.get("max_emit_lag_ms") or 0.0) for request in requests),
                    default=0.0,
                )
            ),
            "errors": sorted(
                {str(request["error"]) for request in requests if request.get("error")}
            )[:MAX_REPORTED_ERRORS],
        },
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
