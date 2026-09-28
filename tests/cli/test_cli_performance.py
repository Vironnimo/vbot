"""Tests for the ``vbot performance`` commands: RPC requests and printed output."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

METRIC = {
    "count": 4,
    "sum_ms": 80.0,
    "min_ms": 5.0,
    "max_ms": 40.0,
    "p50_ms": 10.5,
    "p90_ms": 38.1,
    "p99_ms": 40.0,
}
RPC_CHAT_ROW = "- rpc.chat.send count=4 p50_ms=10.5 p90_ms=38.1 p99_ms=40.0 max_ms=40.0 sum_ms=80.0"
SQLITE_ROW = "- sqlite.write count=4 p50_ms=10.5 p90_ms=38.1 p99_ms=3.0 max_ms=40.0 sum_ms=900.0"


def test_status_shows_slowest_metrics_gauges_stalls_and_recording(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "performance.snapshot",
        {
            "started_at": "2026-09-24T10:00:00+00:00",
            "uptime_seconds": 120.5,
            "metrics": {
                "rpc.chat.send": METRIC,
                "sqlite.write": {**METRIC, "p99_ms": 3.0, "sum_ms": 900.0},
            },
            "gauges": {"process.rss_mb": 210.4, "runs.active": 1, "worker_pool.x.active": 0},
            "counters": {"events.resource_changed": 40, "events.sse": 900},
            "stalls": [
                {
                    "started_at": "2026-09-24T10:01:00+00:00",
                    "duration_ms": 320.0,
                    "gc_ms": 250.0,
                    "samples": [
                        {"count": 1, "stack": ["core/a.py:1 other"]},
                        {
                            "count": 5,
                            "stack": ["core/x.py:10 work", "core/y.py:5 caller", "a", "b"],
                        },
                    ],
                }
            ],
            "recording": {
                "recording_id": "perf_abc",
                "label": "baseline",
                "started_at": "2026-09-24T10:00:30+00:00",
                "elapsed_seconds": 12.5,
                "max_seconds": 300,
                "event_count": 1234,
                "truncated": False,
            },
        },
    )

    code, out, _err = run_cli("performance", "status")

    assert code == 0
    assert rpc.calls == [("performance.snapshot", {})]
    assert out.splitlines() == [
        "started_at=2026-09-24T10:00:00+00:00 uptime_seconds=120.5",
        "recording: id=perf_abc label=baseline elapsed_seconds=12.5 max_seconds=300 "
        "events=1234 truncated=no",
        "gauges: process.rss_mb=210.4 runs.active=1",
        "top metrics by p99:",
        RPC_CHAT_ROW,
        SQLITE_ROW,
        "top metrics by total time:",
        SQLITE_ROW,
        RPC_CHAT_ROW,
        "top counters:",
        "- events.sse=900",
        "- events.resource_changed=40",
        "recent stalls (newest first, 1 retained):",
        "- started_at=2026-09-24T10:01:00+00:00 duration_ms=320.0 gc_ms=250.0 samples=6",
        "    core/x.py:10 work",
        "    core/y.py:5 caller",
        "    a",
    ]


def test_status_reports_an_idle_server_without_samples(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "performance.snapshot",
        {
            "started_at": "2026-09-24T10:00:00+00:00",
            "uptime_seconds": 1.0,
            "metrics": {},
            "gauges": {},
            "stalls": [],
            "recording": None,
        },
    )

    code, out, _err = run_cli("perf", "status")

    assert code == 0
    assert out.splitlines()[1:] == [
        "recording: none",
        "gauges: -",
        "top metrics by p99: -",
        "top metrics by total time: -",
        "top counters: -",
        "recent stalls: none",
    ]


@pytest.mark.parametrize(
    ("options", "params"),
    [
        pytest.param(
            ("--label", "baseline", "--max-seconds", "60"),
            {"max_seconds": 60, "label": "baseline"},
            id="label-and-limit",
        ),
        pytest.param((), {"max_seconds": 300}, id="defaults"),
    ],
)
def test_record_start_sends_the_label_and_limit(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], params: dict[str, Any]
) -> None:
    rpc.reply(
        "performance.recording_start",
        {
            "recording_id": "perf_abc",
            "label": "baseline",
            "started_at": "2026-09-24T10:00:00+00:00",
            "elapsed_seconds": 0.0,
            "max_seconds": 60,
            "event_count": 0,
            "truncated": False,
        },
    )

    code, out, _err = run_cli("perf", "record", "start", *options)

    assert code == 0
    assert rpc.calls == [("performance.recording_start", params)]
    assert out.splitlines()[0] == "recording started: id=perf_abc label=baseline max_seconds=60"
    assert "vbot performance record stop" in out


def test_record_stop_prints_the_trace_path_and_perfetto_hint(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "performance.recording_stop",
        {
            "recording_id": "perf_abc",
            "label": None,
            "started_at": "2026-09-24T10:00:00+00:00",
            "duration_seconds": 12.5,
            "event_count": 800,
            "truncated": True,
            "stopped_reason": "max_seconds",
            "trace_path": "C:/data/artifacts/performance/perf_abc.trace.json",
            "summary": {"metrics": {"rpc.chat.send": METRIC}, "gauges_max": {}, "stalls": []},
        },
    )

    code, out, _err = run_cli("performance", "record", "stop")

    assert code == 0
    assert rpc.calls == [("performance.recording_stop", {})]
    assert out.splitlines() == [
        "recording stopped: id=perf_abc reason=max_seconds duration_seconds=12.5 events=800 "
        "truncated=yes stalls=0",
        "trace: C:/data/artifacts/performance/perf_abc.trace.json",
        "open the trace file at https://ui.perfetto.dev",
        "top metrics by p99:",
        RPC_CHAT_ROW,
    ]
    # Writing a large trace may exceed the default RPC timeout.
    assert isinstance(rpc.timeouts[0], httpx.Timeout) and rpc.timeouts[0].read is None


def test_record_stop_without_recording_explains_the_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail(
        "performance.recording_stop",
        "performance_recording_inactive",
        "No performance recording is active",
    )

    code, out, _err = run_cli("performance", "record", "stop")

    assert code == 1
    assert "performance_recording_inactive: No performance recording is active" in out


@pytest.mark.parametrize(
    ("options", "limit", "recordings", "expected"),
    [
        pytest.param(
            ("--limit", "5"),
            5,
            [
                {
                    "recording_id": "perf_abc",
                    "label": "baseline",
                    "started_at": "2026-09-24T10:00:00+00:00",
                    "duration_seconds": 12.5,
                    "event_count": 800,
                    "truncated": False,
                    "stopped_reason": "requested",
                    "trace_path": "C:/data/artifacts/performance/perf_abc.trace.json",
                }
            ],
            [
                "recordings:",
                "- id=perf_abc started_at=2026-09-24T10:00:00+00:00 duration_seconds=12.5 "
                "events=800 truncated=no reason=requested label=baseline",
                "  trace=C:/data/artifacts/performance/perf_abc.trace.json",
            ],
            id="stored",
        ),
        pytest.param((), 20, [], ["no performance recordings stored"], id="empty"),
    ],
)
def test_recordings_lists_the_stored_recordings(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    limit: int,
    recordings: list[dict[str, Any]],
    expected: list[str],
) -> None:
    rpc.reply("performance.recording_list", {"recordings": recordings})

    code, out, _err = run_cli("performance", "recordings", *options)

    assert code == 0
    assert rpc.calls == [("performance.recording_list", {"limit": limit})]
    assert out.splitlines() == expected


@pytest.mark.parametrize(
    ("previous", "growth", "expected_tail"),
    [
        pytest.param(
            None,
            [],
            ["growth: first census of this server process; run it again later to compare"],
            id="first",
        ),
        pytest.param(
            "2026-09-24T09:00:00+00:00",
            [{"name": "core.runs.RunEvent", "count": 900, "change": 400}],
            [
                "largest growth since 2026-09-24T09:00:00+00:00:",
                "- core.runs.RunEvent count=900 change=+400",
            ],
            id="compared",
        ),
    ],
)
def test_heap_prints_generations_top_types_modules_and_growth(
    rpc: FakeRpc,
    run_cli: RunCli,
    previous: str | None,
    growth: list[dict[str, Any]],
    expected_tail: list[str],
) -> None:
    change = None if previous is None else -5
    rpc.reply(
        "performance.heap",
        {
            "taken_at": "2026-09-24T10:00:00+00:00",
            "duration_ms": 412.5,
            "previous_taken_at": previous,
            "tracked": 1500,
            "generations": [
                {"objects": 100, "collections": 50, "collected": 7, "uncollectable": 0},
                {"objects": 0, "collections": 5, "collected": 1, "uncollectable": 0},
                {"objects": 1400, "collections": 2, "collected": 0, "uncollectable": 0},
            ],
            "frozen": 9000,
            "types": [{"name": "builtins.dict", "count": 600, "change": change}],
            "modules": [{"name": "builtins", "count": 1100, "change": change}],
            "growth": growth,
        },
    )

    code, out, _err = run_cli("performance", "heap", "--top", "5")

    change_text = "" if change is None else " change=-5"
    assert code == 0
    assert rpc.calls == [("performance.heap", {"top": 5})]
    assert out.splitlines() == [
        "heap census: taken_at=2026-09-24T10:00:00+00:00 duration_ms=412.5 tracked=1500 "
        "frozen=9000",
        "generations: gen0 objects=100 collections=50 | gen1 objects=0 collections=5 "
        "| gen2 objects=1400 collections=2",
        "top types:",
        f"- builtins.dict count=600{change_text}",
        "top modules:",
        f"- builtins count=1100{change_text}",
        *expected_tail,
    ]
