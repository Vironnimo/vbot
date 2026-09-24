"""The browser probe's raw measurements reduce to report figures."""

from scripts.perf_load_suite.ui_probe import summarize_probe


def test_probe_result_is_summarized():
    summary = summarize_probe(
        {
            "event": "result",
            "stop_reason": "stop",
            "duration_ms": 12_345.67,
            "frames": 700,
            "mutations": 900,
            "long_tasks_ms": [55, 120.5],
            "frame_gaps_ms": [60.0, 80.0],
            "marker_latencies_ms": [80.0, 90.0, 100.0],
            "heap_used_mb": 41.234,
            "dom_nodes": 1500,
        }
    )

    assert summary["status"] == "ok"
    assert summary["duration_ms"] == 12_345.7
    assert summary["long_tasks"] == {"count": 2, "total_ms": 175.5, "max_ms": 120.5}
    assert summary["frame_gaps"]["count"] == 2
    assert summary["frame_gaps"]["max_ms"] == 80.0
    assert summary["dom_marker_latency_ms"]["p50"] == 90.0
    assert summary["heap_used_mb"] == 41.2


def test_quiet_page_has_no_long_tasks_or_gaps():
    summary = summarize_probe({"event": "result", "heap_used_mb": None})

    assert summary["long_tasks"] == {"count": 0, "total_ms": 0, "max_ms": None}
    assert summary["frame_gaps"] == {"count": 0, "max_ms": None, "p95_ms": None}
    assert summary["heap_used_mb"] is None
