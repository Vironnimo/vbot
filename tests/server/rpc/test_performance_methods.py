"""Tests for the ``performance.*`` RPC handlers.

Coverage:
- ``performance.snapshot`` returns the documented fields, including dispatch metrics,
- a recording starts, reports status, stops into a trace file and is listed,
- starting while recording and stopping while idle return their stable error codes,
- ``performance.heap`` returns the documented census fields,
- ``performance.history`` accepts a time range, limit and names,
- ``performance.client_report`` merges known ``webui.*`` names, skips unknown
  ones, rejects malformed reports whole and limits the report rate,
- request parameters are validated before the service is touched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.performance import PerformanceService
from core.performance._metrics import bucket_index
from core.performance.performance import reset_for_tests
from server.rpc import performance_methods
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_PERFORMANCE_RECORDING_ACTIVE,
    RPC_ERROR_PERFORMANCE_RECORDING_INACTIVE,
)
from server.rpc.methods import dispatch_rpc

STATUS_KEYS = {
    "recording_id",
    "label",
    "started_at",
    "elapsed_seconds",
    "max_seconds",
    "event_count",
    "truncated",
}
LIST_KEYS = {
    "recording_id",
    "label",
    "started_at",
    "duration_seconds",
    "event_count",
    "truncated",
    "stopped_reason",
    "trace_path",
}


@pytest.fixture
def service(tmp_path: Path) -> Iterator[PerformanceService]:
    reset_for_tests()
    performance = PerformanceService(tmp_path / "performance")
    yield performance
    performance.stop()
    reset_for_tests()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def report_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(
        performance_methods,
        "_CLIENT_REPORTS",
        performance_methods._ReportLimiter(  # noqa: SLF001 - a fresh limit per test.
            performance_methods.CLIENT_REPORTS_PER_MINUTE, clock
        ),
    )
    return clock


def _client_histogram(*durations: float) -> dict[str, Any]:
    buckets: dict[str, int] = {}
    for ms in durations:
        key = str(bucket_index(ms))
        buckets[key] = buckets.get(key, 0) + 1
    return {
        "count": len(durations),
        "sum_ms": sum(durations),
        "min_ms": min(durations),
        "max_ms": max(durations),
        "buckets": buckets,
    }


def _state(service: PerformanceService) -> SimpleNamespace:
    return SimpleNamespace(runtime=SimpleNamespace(performance=service))


async def _call(service: PerformanceService, method: str, **params: Any) -> dict[str, Any]:
    return await dispatch_rpc(_state(service), {"method": method, "params": params})


async def _result(service: PerformanceService, method: str, **params: Any) -> Any:
    response = await _call(service, method, **params)
    assert response["ok"] is True, response
    return response["result"]


@pytest.mark.asyncio
async def test_snapshot_reports_metrics_gauges_stalls_and_recording(
    service: PerformanceService,
) -> None:
    await _result(service, "performance.snapshot")
    snapshot = await _result(service, "performance.snapshot")

    assert set(snapshot) == {
        "started_at",
        "uptime_seconds",
        "metrics",
        "gauges",
        "counters",
        "stalls",
        "recording",
    }
    assert set(snapshot["metrics"]["rpc.performance.snapshot"]) == {
        "count",
        "sum_ms",
        "min_ms",
        "max_ms",
        "p50_ms",
        "p90_ms",
        "p99_ms",
    }
    assert snapshot["recording"] is None
    assert snapshot["stalls"] == []


@pytest.mark.asyncio
async def test_recording_start_stop_and_list_follow_the_contract(
    service: PerformanceService,
) -> None:
    status = await _result(service, "performance.recording_start", label="baseline", max_seconds=30)
    snapshot = await _result(service, "performance.snapshot")
    stopped = await _result(service, "performance.recording_stop")
    listed = await _result(service, "performance.recording_list", limit=5)

    assert set(status) == STATUS_KEYS
    assert status["label"] == "baseline" and status["max_seconds"] == 30
    assert snapshot["recording"]["recording_id"] == status["recording_id"]
    assert set(stopped) == LIST_KEYS | {"summary"}
    assert stopped["stopped_reason"] == "requested"
    assert set(stopped["summary"]) == {"metrics", "gauges_max", "counters", "stalls"}
    trace_path = Path(stopped["trace_path"])
    assert trace_path.is_absolute() and "\\" not in stopped["trace_path"]
    events = json.loads(trace_path.read_text(encoding="utf-8"))["traceEvents"]
    rpc_spans = [event["name"] for event in events if event["ph"] == "X"]
    assert "performance.snapshot" in rpc_spans
    assert set(listed) == {"recordings"}
    assert [entry["recording_id"] for entry in listed["recordings"]] == [status["recording_id"]]
    assert set(listed["recordings"][0]) == LIST_KEYS


@pytest.mark.asyncio
async def test_heap_census_returns_the_documented_fields(service: PerformanceService) -> None:
    census = await _result(service, "performance.heap", top=3)

    assert set(census) == {
        "taken_at",
        "duration_ms",
        "previous_taken_at",
        "tracked",
        "generations",
        "frozen",
        "types",
        "modules",
        "growth",
    }
    assert len(census["types"]) == 3
    assert set(census["types"][0]) == {"name", "count", "change"}


@pytest.mark.asyncio
async def test_history_reads_stored_windows_in_a_time_range(service: PerformanceService) -> None:
    history = await _result(
        service,
        "performance.history",
        since="2026-09-28T10:00:00+02:00",
        until="2026-09-28T12:00:00Z",
        limit=10,
        names=["gc.gen2"],
    )

    assert history == {"interval_seconds": 600, "retention_days": 14, "windows": []}


@pytest.mark.asyncio
async def test_client_reports_merge_known_webui_names_into_the_sink_and_recording(
    service: PerformanceService,
) -> None:
    await _result(service, "performance.recording_start")
    first = await _result(
        service,
        "performance.client_report",
        histograms={"webui.rpc": _client_histogram(5.0, 15.0), "webui.other": _client_histogram(1)},
        counters={
            "webui.invalidations.extensions": 2,
            "webui.invalidation_rpcs.extensions.extensions.pages": 4,
            "webui.extension_page.invalidations.change": 1,
            "webui.invalidations.unknown_kind": 1,
            "webui.invalidation_rpcs.extensions.no.such_method": 1,
        },
    )
    await _result(
        service,
        "performance.client_report",
        histograms={"webui.rpc": _client_histogram(40.0)},
        counters={"webui.invalidations.extensions": 1},
    )
    snapshot = await _result(service, "performance.snapshot")
    stopped = await _result(service, "performance.recording_stop")

    assert first == {
        "accepted": 4,
        "skipped": [
            "webui.invalidation_rpcs.extensions.no.such_method",
            "webui.invalidations.unknown_kind",
            "webui.other",
        ],
    }
    rpc = snapshot["metrics"]["webui.rpc"]
    assert (rpc["count"], rpc["sum_ms"], rpc["min_ms"], rpc["max_ms"]) == (3, 60.0, 5.0, 40.0)
    assert 13.5 <= rpc["p50_ms"] <= 16.5
    assert "webui.other" not in snapshot["metrics"]
    counters = snapshot["counters"]
    assert counters["webui.invalidations.extensions"] == 3
    assert counters["webui.invalidation_rpcs.extensions.extensions.pages"] == 4
    assert counters["webui.extension_page.invalidations.change"] == 1
    assert counters["webui.reports"] == 2
    assert not any(name.endswith("unknown_kind") for name in counters)
    summary = stopped["summary"]
    assert summary["metrics"]["webui.rpc"]["count"] == 3
    assert summary["counters"]["webui.reports"] == 2
    events = json.loads(Path(stopped["trace_path"]).read_text(encoding="utf-8"))["traceEvents"]
    reports = [event for event in events if event["name"] == "webui.reports"]
    assert [event["args"]["rpcs"] for event in reports] == [2, 1]


@pytest.mark.asyncio
async def test_client_reports_beyond_the_rate_limit_are_dropped(
    service: PerformanceService, report_clock: FakeClock
) -> None:
    report = {"counters": {"webui.rpc_errors": 1}}
    for _ in range(performance_methods.CLIENT_REPORTS_PER_MINUTE):
        await _result(service, "performance.client_report", **report)
    limited = await _result(service, "performance.client_report", **report)
    report_clock.now += 60
    after_a_minute = await _result(service, "performance.client_report", **report)
    counters = (await _result(service, "performance.snapshot"))["counters"]

    assert limited == {"accepted": 0, "skipped": [], "rate_limited": True}
    assert after_a_minute["accepted"] == 1
    assert counters["webui.reports"] == performance_methods.CLIENT_REPORTS_PER_MINUTE + 1
    assert counters["webui.reports_dropped"] == 1


@pytest.mark.asyncio
async def test_recording_start_while_active_and_stop_while_idle_are_rejected(
    service: PerformanceService,
) -> None:
    idle = await _call(service, "performance.recording_stop")
    await _result(service, "performance.recording_start")
    busy = await _call(service, "performance.recording_start", label="second")
    await _result(service, "performance.recording_stop")

    assert idle["ok"] is False
    assert idle["error"]["code"] == RPC_ERROR_PERFORMANCE_RECORDING_INACTIVE
    assert busy["ok"] is False
    assert busy["error"]["code"] == RPC_ERROR_PERFORMANCE_RECORDING_ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("performance.snapshot", {"extra": 1}),
        ("performance.recording_start", {"label": ""}),
        ("performance.recording_start", {"label": "   "}),
        ("performance.recording_start", {"label": "x" * 201}),
        ("performance.recording_start", {"max_seconds": 0}),
        ("performance.recording_start", {"max_seconds": 3601}),
        ("performance.recording_start", {"max_seconds": "60"}),
        ("performance.recording_stop", {"force": True}),
        ("performance.recording_list", {"limit": 0}),
        ("performance.recording_list", {"limit": 101}),
        ("performance.heap", {"top": 0}),
        ("performance.heap", {"top": 101}),
        ("performance.heap", {"depth": 2}),
        ("performance.history", {"since": "2026-09-28T10:00:00"}),
        ("performance.history", {"until": "yesterday"}),
        ("performance.history", {"limit": 2017}),
        ("performance.history", {"names": "gc.gen2"}),
        ("performance.history", {"names": [f"name.{index}" for index in range(51)]}),
        ("performance.history", {"hours": 2}),
        ("performance.client_report", {"gauges": {}}),
        ("performance.client_report", {"counters": []}),
        ("performance.client_report", {"counters": {"webui.rpc_errors": 0}}),
        ("performance.client_report", {"counters": {"webui.rpc_errors": True}}),
        ("performance.client_report", {"counters": {"webui.rpc_errors": 1_000_001}}),
        (
            "performance.client_report",
            {"counters": {f"webui.invalidations.{index}": 1 for index in range(201)}},
        ),
        # One malformed histogram rejects the whole report, valid parts included.
        (
            "performance.client_report",
            {
                "counters": {"webui.rpc_errors": 1},
                "histograms": {"webui.rpc": {**_client_histogram(5.0), "count": 2}},
            },
        ),
        (
            "performance.client_report",
            {"histograms": {"webui.rpc": {**_client_histogram(5.0), "min_ms": -1}}},
        ),
        (
            "performance.client_report",
            {"histograms": {"webui.rpc": {**_client_histogram(5.0), "buckets": {"999": 1}}}},
        ),
        (
            "performance.client_report",
            {"histograms": {"webui.rpc": {**_client_histogram(5.0), "buckets": {"x": 1}}}},
        ),
        (
            "performance.client_report",
            {"histograms": {"webui.rpc": {**_client_histogram(5.0), "max_ms": float("inf")}}},
        ),
        ("performance.client_report", {"histograms": {"webui.rpc": {"count": 1}}}),
    ],
)
async def test_invalid_requests_are_rejected(
    service: PerformanceService, method: str, params: dict[str, Any]
) -> None:
    response = await _call(service, method, **params)
    snapshot = await _result(service, "performance.snapshot")

    assert response["ok"] is False
    assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
    assert service.recording_status() is None
    assert not any(name.startswith("webui.") for name in snapshot["counters"])
    assert not any(name.startswith("webui.") for name in snapshot["metrics"])
