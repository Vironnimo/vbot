"""Tests for performance CLI parsing, RPC commands, and output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import _dispatch_operations, performance_management
from cli import main as cli_main
from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path

METRIC = {
    "count": 4,
    "sum_ms": 80.0,
    "min_ms": 5.0,
    "max_ms": 40.0,
    "p50_ms": 10.5,
    "p90_ms": 38.1,
    "p99_ms": 40.0,
}


def make_instance(tmp_path: Path) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=8427,
        data_dir=data_dir,
        url="http://127.0.0.1:8427",
        log_path=resolve_daily_log_path(data_dir),
    )


def fake_rpc(
    monkeypatch: pytest.MonkeyPatch, method: str, params: dict[str, Any], response: dict[str, Any]
) -> list[Any]:
    timeouts: list[Any] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: Any, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": method, "params": params}
        timeouts.append(timeout)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(performance_management.httpx, "post", fake_post)
    return timeouts


def test_parse_args_supports_nested_record_commands_and_the_perf_alias() -> None:
    start = cli_main.parse_args(
        ["perf", "record", "start", "--label", "baseline", "--max-seconds", "60"]
    )
    stop = cli_main.parse_args(["performance", "record", "stop"])
    recordings = cli_main.parse_args(["performance", "recordings", "--limit", "3"])
    defaults = cli_main.parse_args(["performance", "record", "start"])

    assert (start.area, start.command, start.label, start.max_seconds) == (
        "performance",
        "record-start",
        "baseline",
        60,
    )
    assert (stop.area, stop.command) == ("performance", "record-stop")
    assert recordings.limit == 3
    assert (defaults.label, defaults.max_seconds) == (None, 300)
    assert cli_main.parse_args(["performance", "recordings"]).limit == 20


def test_status_shows_slowest_metrics_gauges_stalls_and_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    snapshot = {
        "started_at": "2026-09-24T10:00:00+00:00",
        "uptime_seconds": 120.5,
        "metrics": {
            "rpc.chat.send": METRIC,
            "sqlite.write": {**METRIC, "p99_ms": 3.0, "sum_ms": 900.0},
        },
        "gauges": {"process.rss_mb": 210.4, "runs.active": 1, "worker_pool.x.active": 0},
        "stalls": [
            {
                "started_at": "2026-09-24T10:01:00+00:00",
                "duration_ms": 320.0,
                "samples": [
                    {"count": 1, "stack": ["core/a.py:1 other"]},
                    {"count": 5, "stack": ["core/x.py:10 work", "core/y.py:5 caller", "a", "b"]},
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
    }
    fake_rpc(monkeypatch, "performance.snapshot", {}, {"ok": True, "result": snapshot})

    result = performance_management.performance_status(instance)

    assert result.ok is True
    assert result.message.splitlines() == [
        "started_at=2026-09-24T10:00:00+00:00 uptime_seconds=120.5",
        "recording: id=perf_abc label=baseline elapsed_seconds=12.5 max_seconds=300 "
        "events=1234 truncated=no",
        "gauges: process.rss_mb=210.4 runs.active=1",
        "top metrics by p99:",
        "- rpc.chat.send count=4 p50_ms=10.5 p90_ms=38.1 p99_ms=40.0 max_ms=40.0 sum_ms=80.0",
        "- sqlite.write count=4 p50_ms=10.5 p90_ms=38.1 p99_ms=3.0 max_ms=40.0 sum_ms=900.0",
        "top metrics by total time:",
        "- sqlite.write count=4 p50_ms=10.5 p90_ms=38.1 p99_ms=3.0 max_ms=40.0 sum_ms=900.0",
        "- rpc.chat.send count=4 p50_ms=10.5 p90_ms=38.1 p99_ms=40.0 max_ms=40.0 sum_ms=80.0",
        "recent stalls (newest first, 1 retained):",
        "- started_at=2026-09-24T10:01:00+00:00 duration_ms=320.0 samples=6",
        "    core/x.py:10 work",
        "    core/y.py:5 caller",
        "    a",
    ]


def test_status_reports_an_idle_server_without_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = {
        "started_at": "2026-09-24T10:00:00+00:00",
        "uptime_seconds": 1.0,
        "metrics": {},
        "gauges": {},
        "stalls": [],
        "recording": None,
    }
    fake_rpc(monkeypatch, "performance.snapshot", {}, {"ok": True, "result": snapshot})

    message = performance_management.performance_status(make_instance(tmp_path)).message

    assert message.splitlines()[1:] == [
        "recording: none",
        "gauges: -",
        "top metrics by p99: -",
        "top metrics by total time: -",
        "recent stalls: none",
    ]


def test_record_start_sends_label_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {
        "recording_id": "perf_abc",
        "label": "baseline",
        "started_at": "2026-09-24T10:00:00+00:00",
        "elapsed_seconds": 0.0,
        "max_seconds": 60,
        "event_count": 0,
        "truncated": False,
    }
    fake_rpc(
        monkeypatch,
        "performance.recording_start",
        {"max_seconds": 60, "label": "baseline"},
        {"ok": True, "result": status},
    )

    result = performance_management.performance_record_start(
        make_instance(tmp_path), "baseline", 60
    )

    assert result.message.splitlines()[0] == (
        "recording started: id=perf_abc label=baseline max_seconds=60"
    )
    assert "vbot performance record stop" in result.message


def test_record_stop_prints_the_trace_path_and_perfetto_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = {
        "recording_id": "perf_abc",
        "label": None,
        "started_at": "2026-09-24T10:00:00+00:00",
        "duration_seconds": 12.5,
        "event_count": 800,
        "truncated": True,
        "stopped_reason": "max_seconds",
        "trace_path": "C:/data/artifacts/performance/perf_abc.trace.json",
        "summary": {"metrics": {"rpc.chat.send": METRIC}, "gauges_max": {}, "stalls": []},
    }
    timeouts = fake_rpc(
        monkeypatch, "performance.recording_stop", {}, {"ok": True, "result": stopped}
    )

    result = performance_management.performance_record_stop(make_instance(tmp_path))

    assert result.message.splitlines() == [
        "recording stopped: id=perf_abc reason=max_seconds duration_seconds=12.5 events=800 "
        "truncated=yes stalls=0",
        "trace: C:/data/artifacts/performance/perf_abc.trace.json",
        "open the trace file at https://ui.perfetto.dev",
        "top metrics by p99:",
        "- rpc.chat.send count=4 p50_ms=10.5 p90_ms=38.1 p99_ms=40.0 max_ms=40.0 sum_ms=80.0",
    ]
    # Writing a large trace may exceed the default RPC timeout.
    assert isinstance(timeouts[0], httpx.Timeout) and timeouts[0].read is None


def test_record_stop_without_recording_explains_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_rpc(
        monkeypatch,
        "performance.recording_stop",
        {},
        {
            "ok": False,
            "error": {
                "code": "performance_recording_inactive",
                "message": "No performance recording is active",
            },
        },
    )

    result = performance_management.performance_record_stop(make_instance(tmp_path))

    assert result.ok is False
    assert result.message.startswith("performance_recording_inactive:")


def test_recordings_lists_newest_first_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = {
        "recording_id": "perf_abc",
        "label": "baseline",
        "started_at": "2026-09-24T10:00:00+00:00",
        "duration_seconds": 12.5,
        "event_count": 800,
        "truncated": False,
        "stopped_reason": "requested",
        "trace_path": "C:/data/artifacts/performance/perf_abc.trace.json",
    }
    fake_rpc(
        monkeypatch,
        "performance.recording_list",
        {"limit": 5},
        {"ok": True, "result": {"recordings": [entry]}},
    )

    result = performance_management.performance_recordings(make_instance(tmp_path), 5)

    assert result.message.splitlines() == [
        "recordings:",
        "- id=perf_abc started_at=2026-09-24T10:00:00+00:00 duration_seconds=12.5 events=800 "
        "truncated=no reason=requested label=baseline",
        "  trace=C:/data/artifacts/performance/perf_abc.trace.json",
    ]


def test_recordings_reports_an_empty_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rpc(
        monkeypatch,
        "performance.recording_list",
        {"limit": 20},
        {"ok": True, "result": {"recordings": []}},
    )

    result = performance_management.performance_recordings(make_instance(tmp_path), 20)

    assert result == CommandResult(
        ok=True, message="no performance recordings stored", instance=result.instance
    )


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["status"], ("status",)),
        (["record", "start", "--label", "x", "--max-seconds", "9"], ("start", "x", 9)),
        (["record", "stop"], ("stop",)),
        (["recordings", "--limit", "2"], ("recordings", 2)),
    ],
)
def test_run_routes_each_command_to_its_operation(
    tokens: list[str],
    expected: tuple[Any, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[Any, ...]] = []

    def record(*values: Any) -> CommandResult:
        calls.append(values)
        return CommandResult(ok=True, message="done", instance=instance)

    monkeypatch.setattr(_dispatch_operations, "performance_status", lambda _i: record("status"))
    monkeypatch.setattr(
        _dispatch_operations,
        "performance_record_start",
        lambda _i, label, seconds: record("start", label, seconds),
    )
    monkeypatch.setattr(_dispatch_operations, "performance_record_stop", lambda _i: record("stop"))
    monkeypatch.setattr(
        _dispatch_operations,
        "performance_recordings",
        lambda _i, limit: record("recordings", limit),
    )

    code = cli_main.run(["perf", *tokens], resolve=lambda **_kw: instance)

    assert code == 0
    assert calls == [expected]
    assert "done" in capsys.readouterr().out
