"""Latency math: percentiles, marker latency, step overhead and level analysis."""

import pytest

from scripts.perf_load_suite.directive import PerfDirective
from scripts.perf_load_suite.fake_provider import format_marker
from scripts.perf_load_suite.metrics import (
    RunRecord,
    ToolTiming,
    analyze_level,
    distribution,
    marker_latencies_ms,
    percentile,
    retried_requests,
    step_gaps,
)


def test_percentile_interpolates_linearly():
    values = [10.0, 20.0, 30.0, 40.0]

    assert percentile(values, 0) == 10.0
    assert percentile(values, 50) == 25.0
    assert percentile(values, 100) == 40.0
    assert percentile([7.0], 99) == 7.0
    assert percentile([], 50) is None


def test_distribution_of_nothing_has_no_figures():
    assert distribution([]) == {
        "count": 0,
        "mean": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
    }
    assert distribution([1, 2, 3])["mean"] == 2.0


def test_marker_latency_is_receipt_minus_emission():
    text = f"{format_marker(100.0)}alpha beta {format_marker(100.25)}gamma"

    assert marker_latencies_ms(text, 100.5) == pytest.approx([500.0, 250.0])
    assert marker_latencies_ms("no markers", 100.0) == []


def _request(tag, kind, round_index, arrival, completed, call_ids=(), tools=()):
    return {
        "tag": tag,
        "kind": kind,
        "round": round_index,
        "arrival": arrival,
        "completed": completed,
        "tool_call_ids": list(call_ids),
        "tool_names": list(tools),
    }


def test_step_gap_is_next_arrival_minus_tool_response_completion():
    requests = [
        _request("t", "tool_calls", 0, 10.0, 10.1, ["a", "b"], ["read", "read"]),
        _request("t", "tool_calls", 1, 10.6, 10.65, ["c"], ["bash"]),
        _request("t", "text", 2, 11.65, 13.0),
        _request(None, "aux", 0, 10.2, 10.3),
    ]

    gaps = step_gaps(requests, {"a": 100.0, "b": 300.0, "c": None})

    assert [(gap.round_index, gap.tools) for gap in gaps] == [(0, ("read", "read")), (1, ("bash",))]
    assert gaps[0].gap_ms == pytest.approx(500.0)
    # Parallel calls: the slowest one bounds the round's Tool time.
    assert gaps[0].tool_exec_ms == 300.0
    assert gaps[1].gap_ms == pytest.approx(1000.0)
    # An unknown Tool duration leaves the Tool share unknown instead of zero.
    assert gaps[1].tool_exec_ms is None


def test_repeated_rounds_count_as_retries():
    requests = [
        _request("t", "tool_calls", 0, 1.0, 1.1),
        _request("t", "tool_calls", 0, 2.0, 2.1),
        _request("t", "text", 1, 3.0, 3.1),
    ]

    assert retried_requests(requests) == 1


def _run(tag, *, sent, first_delta, finished, status="completed", timings=()):
    return RunRecord(
        tag=tag,
        session_index=0,
        turn_index=0,
        agent_id="perf-agent-01",
        session_id="s",
        directive=PerfDirective(tag=tag, steps=2, tokens=80, rate=80, think_ms=600),
        sent_at=sent,
        first_delta_at=first_delta,
        finished_at=finished,
        status=status,
        error=None if status == "completed" else "boom",
        delta_latencies_ms=[40.0, 60.0],
        tool_timings=list(timings),
    )


def test_level_analysis_derives_overheads_from_client_and_provider_times():
    runs = [
        _run(
            "a",
            sent=100.0,
            first_delta=101.0,
            finished=102.0,
            timings=[ToolTiming("c1", "read", 50.0, True)],
        ),
        _run("b", sent=100.0, first_delta=None, finished=None, status="timeout"),
    ]
    requests = [
        _request("a", "tool_calls", 0, 100.2, 100.25, ["c1"], ["read"]),
        _request("a", "text", 1, 100.35, 102.0),
        _request(None, "aux", 0, 100.5, 100.6),
    ]

    level = analyze_level(runs, requests)

    assert level["runs"] == {
        "total": 2,
        "ok": 1,
        "failed": 1,
        "by_status": {"completed": 1, "timeout": 1},
        "tool_calls": 1,
        "tool_errors": 0,
    }
    assert level["errors"] == ["timeout: boom"]
    assert level["ttft_ms"]["p50"] == pytest.approx(1000.0)
    assert level["ttft_overhead_ms"]["p50"] == pytest.approx(400.0)
    assert level["admission_ms"]["p50"] == pytest.approx(200.0)
    assert level["step_overhead_ms"]["p50"] == pytest.approx(100.0)
    assert level["step_overhead_excl_tool_ms"]["p50"] == pytest.approx(50.0)
    assert level["step_overhead_by_tool_ms"]["read"]["tool_exec"]["p50"] == 50.0
    assert level["run_ideal_ms"]["p50"] == pytest.approx(1600.0)
    assert level["run_excess_ms"]["p50"] == pytest.approx(400.0)
    assert level["run_duration_ratio"]["p50"] == pytest.approx(1.25)
    # Delta markers count from every Run that produced them.
    assert level["delta_latency_ms"]["count"] == 4
    assert level["provider"]["aux_requests"] == 1
    assert level["provider"]["scripted_requests"] == 2
