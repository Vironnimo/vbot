"""Tests for the ``performance.*`` RPC handlers.

Coverage:
- ``performance.snapshot`` returns the documented fields, including dispatch metrics,
- a recording starts, reports status, stops into a trace file and is listed,
- starting while recording and stopping while idle return their stable error codes,
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
from core.performance.performance import reset_for_tests
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
    assert set(stopped["summary"]) == {"metrics", "gauges_max", "stalls"}
    trace_path = Path(stopped["trace_path"])
    assert trace_path.is_absolute() and "\\" not in stopped["trace_path"]
    events = json.loads(trace_path.read_text(encoding="utf-8"))["traceEvents"]
    rpc_spans = [event["name"] for event in events if event["ph"] == "X"]
    assert "performance.snapshot" in rpc_spans
    assert set(listed) == {"recordings"}
    assert [entry["recording_id"] for entry in listed["recordings"]] == [status["recording_id"]]
    assert set(listed["recordings"][0]) == LIST_KEYS


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
    ],
)
async def test_invalid_requests_are_rejected(
    service: PerformanceService, method: str, params: dict[str, Any]
) -> None:
    response = await _call(service, method, **params)

    assert response["ok"] is False
    assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
    assert service.recording_status() is None
