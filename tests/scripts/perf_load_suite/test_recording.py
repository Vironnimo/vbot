"""Server recording control and summary ingestion against a stubbed RPC client."""

import pytest

from scripts.perf_load_suite.recording import (
    RecordingError,
    check_instrumentation,
    copy_trace,
    digest_recording,
    start_recording,
    stop_recording,
)
from scripts.perf_load_suite.rpc import RpcCallError


class StubRpc:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


def _metric(p50, p99, maximum, total, count=10):
    return {
        "count": count,
        "p50_ms": p50,
        "p90_ms": p99,
        "p99_ms": p99,
        "max_ms": maximum,
        "sum_ms": total,
    }


STOP_RESULT = {
    "recording_id": "rec-1",
    "duration_seconds": 12.5,
    "event_count": 4000,
    "truncated": False,
    "stopped_reason": "requested",
    "trace_path": None,
    "summary": {
        "metrics": {
            "event_loop.lag": _metric(1.0, 12.0, 80.0, 50.0),
            "sqlite.write": _metric(0.5, 9.0, 20.0, 400.0),
            "sqlite.write_wait": _metric(0.2, 15.0, 25.0, 60.0),
            "chat.request_build": _metric(3.0, 30.0, 45.0, 900.0),
            "tool.read": _metric(2.0, 6.0, 7.0, 25.0, count=4),
            "tool.bash": _metric(600.0, 650.0, 700.0, 800.0, count=2),
            "worker_pools.ignored": _metric(0, 0, 0, 0),
            "worker_pool.sqlite.wait": _metric(0.1, 40.0, 60.0, 30.0),
            "worker_pool.sqlite.run": _metric(1.0, 8.0, 10.0, 300.0),
            "worker_pool.default.wait": _metric(0.1, 2.0, 3.0, 1.0),
        },
        "gauges_max": {
            "event_loop.utilization": 0.83,
            "process.rss_mb": 250.0,
            "runs.queued": 3,
            "worker_pool.sqlite.active": 1,
            "worker_pool.sqlite.waiting": 7,
        },
        "stalls": [
            {"started_at": "t1", "duration_ms": 120.0, "samples": []},
            {
                "started_at": "t2",
                "duration_ms": 480.0,
                "samples": [
                    {"count": 1, "stack": ["rare"]},
                    {"count": 5, "stack": [f"frame{index}" for index in range(20)]},
                ],
            },
        ],
    },
}


def test_preflight_rejects_a_server_without_performance_rpcs():
    rpc = StubRpc(
        {"performance.snapshot": RpcCallError("performance.snapshot", "method_not_found", "")}
    )

    with pytest.raises(RecordingError, match="no performance"):
        check_instrumentation(rpc)


def test_preflight_rejects_an_already_active_recording():
    rpc = StubRpc({"performance.snapshot": {"recording": {"recording_id": "old"}}})

    with pytest.raises(RecordingError, match="already active"):
        check_instrumentation(rpc)


def test_start_and_stop_use_the_recording_contract():
    rpc = StubRpc(
        {
            "performance.recording_start": {"recording_id": "rec-1"},
            "performance.recording_stop": STOP_RESULT,
        }
    )

    assert start_recording(rpc, label="perf-load 10", max_seconds=600)["recording_id"] == "rec-1"
    assert stop_recording(rpc) == STOP_RESULT
    assert rpc.calls == [
        ("performance.recording_start", {"label": "perf-load 10", "max_seconds": 600}),
        ("performance.recording_stop", {}),
    ]


def test_stop_after_the_recording_expired_names_the_remedy():
    rpc = StubRpc(
        {
            "performance.recording_stop": RpcCallError(
                "performance.recording_stop", "performance_recording_inactive", ""
            )
        }
    )

    with pytest.raises(RecordingError, match="--recording-max-seconds"):
        stop_recording(rpc)


def test_digest_extracts_the_report_figures():
    digest = digest_recording(STOP_RESULT)

    assert digest["event_loop_lag_ms"] == {
        "count": 10,
        "p50": 1.0,
        "p90": 12.0,
        "p99": 12.0,
        "max": 80.0,
        "sum": 50.0,
    }
    assert digest["event_loop_utilization_max"] == 0.83
    assert digest["runs_queued_max"] == 3
    assert digest["sqlite_write_ms"]["p99"] == 9.0
    assert digest["sqlite_write_wait_ms"]["max"] == 25.0
    assert digest["chat_request_build_ms"]["p50"] == 3.0
    # Metrics absent from the recording stay in the digest with empty figures.
    assert digest["chat_persist_ms"] == {
        "count": 0,
        "p50": None,
        "p90": None,
        "p99": None,
        "max": None,
        "sum": None,
    }
    assert list(digest["tools_ms"]) == ["bash", "read"]
    assert digest["tools_ms"]["bash"]["p50"] == 600.0
    assert [pool["pool"] for pool in digest["worker_pools"]] == ["sqlite", "default"]
    assert digest["worker_pools"][0] == {
        "pool": "sqlite",
        "wait_count": 10,
        "wait_p99_ms": 40.0,
        "wait_max_ms": 60.0,
        "run_p99_ms": 8.0,
        "active_max": 1,
        "waiting_max": 7,
    }
    assert digest["top_metrics"][0]["name"] == "chat.request_build"
    stalls = digest["stalls"]
    assert (stalls["count"], stalls["total_ms"]) == (2, 600.0)
    assert stalls["worst"]["duration_ms"] == 480.0
    assert stalls["worst"]["sample_count"] == 5
    assert stalls["worst"]["frames"] == [f"frame{index}" for index in range(12)]


def test_digest_of_an_empty_summary_has_no_figures():
    digest = digest_recording({"summary": {}})

    assert digest["event_loop_lag_ms"]["p99"] is None
    assert digest["tools_ms"] == {}
    assert digest["worker_pools"] == []
    assert digest["stalls"] == {"count": 0, "total_ms": 0.0, "worst": None}


def test_trace_is_copied_next_to_the_level_results(tmp_path):
    trace = tmp_path / "server" / "rec-1.jsonl"
    trace.parent.mkdir()
    trace.write_text("{}\n", encoding="utf-8")

    copied = copy_trace({"trace_path": str(trace)}, tmp_path / "level-01")

    assert copied == tmp_path / "level-01" / "rec-1.jsonl"
    assert copied.read_text(encoding="utf-8") == "{}\n"
    assert copy_trace({"trace_path": None}, tmp_path) is None
