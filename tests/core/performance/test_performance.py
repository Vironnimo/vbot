"""Process-wide recording API and the PerformanceService recording lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from core.performance import (
    PerformanceService,
    RecordingActiveError,
    RecordingInactiveError,
    measure,
    record_duration,
    record_span,
    session_track,
    set_gauge,
)
from core.performance.performance import (
    DROPPED_METRICS_GAUGE,
    MAX_METRIC_NAMES,
)

SNAPSHOT_KEYS = {"started_at", "uptime_seconds", "metrics", "gauges", "stalls", "recording"}
STATUS_KEYS = {
    "recording_id",
    "label",
    "started_at",
    "elapsed_seconds",
    "max_seconds",
    "event_count",
    "truncated",
}
STOP_KEYS = {
    "recording_id",
    "label",
    "started_at",
    "duration_seconds",
    "event_count",
    "truncated",
    "stopped_reason",
    "trace_path",
    "summary",
}
METRIC_KEYS = {"count", "sum_ms", "min_ms", "max_ms", "p50_ms", "p90_ms", "p99_ms"}


def _service(tmp_path: Path, **kwargs) -> PerformanceService:
    return PerformanceService(tmp_path / "performance", **kwargs)


async def _metrics(service: PerformanceService) -> dict[str, Any]:
    metrics: dict[str, Any] = (await service.snapshot())["metrics"]
    return metrics


def _trace_events(result: dict[str, Any]) -> list[dict[str, Any]]:
    trace = json.loads(Path(result["trace_path"]).read_text(encoding="utf-8"))
    events: list[dict[str, Any]] = trace["traceEvents"]
    return events


def _complete_spans(result: dict) -> list[dict]:
    return [event for event in _trace_events(result) if event["ph"] == "X"]


@pytest.mark.asyncio
async def test_measure_records_sync_and_async_blocks_including_failures(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with measure("test.sync"):
        pass
    with pytest.raises(ValueError), measure("test.sync"):
        raise ValueError("still measured")
    with measure("test.async"):
        await asyncio.sleep(0.05)
    with measure("test.discarded") as timer:
        timer.discard()

    metrics = await _metrics(service)

    assert metrics["test.sync"]["count"] == 2
    assert set(metrics["test.sync"]) == METRIC_KEYS
    # Wall-clock across the await; the lower bound tolerates coarse timers.
    assert metrics["test.async"]["sum_ms"] >= 25
    assert "test.discarded" not in metrics


@pytest.mark.asyncio
async def test_record_duration_span_and_gauges_feed_the_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    record_duration("test.duration", 4.0)
    record_span("test.span", time.perf_counter() - 0.002)
    set_gauge("test.gauge", 3)
    set_gauge("test.gauge", 5)

    snapshot = await service.snapshot()

    assert set(snapshot) == SNAPSHOT_KEYS
    assert snapshot["metrics"]["test.duration"]["max_ms"] == 4.0
    assert snapshot["metrics"]["test.span"]["count"] == 1
    assert snapshot["gauges"]["test.gauge"] == 5
    assert snapshot["recording"] is None
    assert snapshot["stalls"] == []
    assert snapshot["uptime_seconds"] >= 0
    await service.aclose()


@pytest.mark.asyncio
async def test_new_metric_names_beyond_the_cap_are_dropped_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service(tmp_path)
    with caplog.at_level(logging.WARNING, logger="vbot.performance"):
        for index in range(MAX_METRIC_NAMES + 3):
            record_duration(f"test.name{index}", 1.0)

    snapshot = await service.snapshot()

    assert len(snapshot["metrics"]) == MAX_METRIC_NAMES
    # The snapshot's own worker-pool wait is one more new name beyond the cap.
    assert snapshot["gauges"][DROPPED_METRICS_GAUGE] >= 3
    assert caplog.text.count("metric name limit") == 1
    await service.aclose()


def test_session_tracks_name_the_session_address() -> None:
    assert session_track("main", "ses_1") == "main/ses_1"
    assert session_track("main", "ses_1", "vbot") == "main@vbot/ses_1"


@pytest.mark.asyncio
async def test_recording_start_status_and_stop_result_follow_the_contract(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service(tmp_path)
    with caplog.at_level(logging.INFO, logger="vbot.performance"):
        status = service.start_recording(label="secret label text", max_seconds=60)
        with measure("chat.run", track="main/ses_1", name="run", args={"run_id": "run_1"}):
            await asyncio.sleep(0)
        set_gauge("runs.active", 1)
        result = await service.stop_recording()

    assert set(status) == STATUS_KEYS
    assert status["recording_id"].startswith("perf_")
    assert status["label"] == "secret label text"
    assert status["max_seconds"] == 60
    assert status["truncated"] is False
    assert set(result) == STOP_KEYS
    assert result["recording_id"] == status["recording_id"]
    assert result["stopped_reason"] == "requested"
    assert set(result["summary"]) == {"metrics", "gauges_max", "stalls"}
    assert result["summary"]["metrics"]["chat.run"]["count"] == 1
    assert result["summary"]["gauges_max"]["runs.active"] == 1
    span = next(span for span in _complete_spans(result) if span["name"] == "run")
    assert span["cat"] == "chat" and span["args"] == {"run_id": "run_1"}
    summary_file = Path(result["trace_path"].replace(".trace.json", ".summary.json"))
    assert json.loads(summary_file.read_text(encoding="utf-8"))["stopped_reason"] == "requested"
    assert service.recording_status() is None
    started, stopped = (r for r in caplog.records if r.name == "vbot.performance")
    assert status["recording_id"] in started.getMessage()
    assert "secret label text" not in caplog.text
    assert "reason=requested" in stopped.getMessage()
    await service.aclose()


@pytest.mark.asyncio
async def test_only_one_recording_is_active_and_stop_requires_one(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(RecordingInactiveError):
        await service.stop_recording()
    service.start_recording()
    with pytest.raises(RecordingActiveError):
        service.start_recording()
    with pytest.raises(RecordingActiveError):
        _service(tmp_path).start_recording()

    await service.stop_recording()

    with pytest.raises(RecordingInactiveError):
        await service.stop_recording()
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"label": ""},
        {"label": "   "},
        {"label": "x" * 201},
        {"max_seconds": 0},
        {"max_seconds": 3601},
        {"max_seconds": True},
    ],
)
async def test_recording_requests_are_validated(tmp_path: Path, kwargs: dict) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError):
        service.start_recording(**kwargs)
    assert service.recording_status() is None


@pytest.mark.asyncio
async def test_spans_are_emitted_only_within_one_recording(tmp_path: Path) -> None:
    service = _service(tmp_path)
    before = measure("test.before", track="t")
    before.__enter__()
    service.start_recording()
    before.__exit__(None, None, None)
    across = measure("test.across", track="t")
    across.__enter__()
    with measure("test.inside", track="t"):
        pass
    first = await service.stop_recording()
    service.start_recording()
    across.__exit__(None, None, None)
    record_span("test.untracked", time.perf_counter())
    second = await service.stop_recording()

    assert [span["name"] for span in _complete_spans(first)] == ["test.inside"]
    assert _complete_spans(second) == []
    assert "test.across" in (await _metrics(service))
    await service.aclose()


@pytest.mark.asyncio
async def test_concurrent_measures_on_one_track_use_different_lanes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start_recording()

    async def step(name: str) -> None:
        with measure("tool.read", track="main/ses_1", name=name):
            await asyncio.sleep(0.01)

    await asyncio.gather(step("a"), step("b"), step("c"))
    result = await service.stop_recording()

    lanes = {span["name"]: span["tid"] for span in _complete_spans(result)}
    assert sorted(lanes.values()) == [0, 1, 2]
    await service.aclose()


@pytest.mark.asyncio
async def test_recording_seeds_current_gauges_on_their_tracks(tmp_path: Path) -> None:
    service = _service(tmp_path)
    set_gauge("worker_pool.test.active", 2, track="worker pool test")
    service.start_recording()
    result = await service.stop_recording()

    events = _trace_events(result)
    processes = {
        event["pid"]: event["args"]["name"] for event in events if event["name"] == "process_name"
    }
    counter = next(event for event in events if event["ph"] == "C")
    assert processes[counter["pid"]] == "worker pool test"
    assert result["summary"]["gauges_max"] == {"worker_pool.test.active": 2}
    await service.aclose()


@pytest.mark.asyncio
async def test_recording_stops_itself_after_max_seconds(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = service.start_recording(max_seconds=1)

    deadline = time.monotonic() + 10
    while not (recordings := await service.list_recordings()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    assert service.recording_status() is None
    assert [entry["recording_id"] for entry in recordings] == [status["recording_id"]]
    assert recordings[0]["stopped_reason"] == "max_seconds"
    await service.aclose()


@pytest.mark.asyncio
async def test_truncated_recordings_stop_buffering_at_the_event_limit(tmp_path: Path) -> None:
    service = _service(tmp_path, max_recording_events=4)
    service.start_recording()
    for _ in range(20):
        with measure("test.many", track="t"):
            pass
    status = service.recording_status()
    result = await service.stop_recording()

    assert status is not None and status["truncated"] is True
    assert result["truncated"] is True
    assert result["event_count"] == 4
    assert len(_trace_events(result)) == 4
    assert result["summary"]["metrics"]["test.many"]["count"] == 20
    await service.aclose()


@pytest.mark.asyncio
async def test_only_the_newest_recordings_are_retained(tmp_path: Path) -> None:
    service = _service(tmp_path, retained_recordings=2)
    ids = []
    for _ in range(3):
        ids.append(service.start_recording()["recording_id"])
        await service.stop_recording()

    recordings = await service.list_recordings()

    assert [entry["recording_id"] for entry in recordings] == [ids[2], ids[1]]
    assert not any(ids[0] in path.name for path in (tmp_path / "performance").iterdir())
    assert len(await service.list_recordings(limit=1)) == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_closing_the_service_discards_its_active_recording(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service(tmp_path)
    recording_id = service.start_recording()["recording_id"]

    with caplog.at_level(logging.INFO, logger="vbot.performance"):
        await service.aclose()

    assert service.recording_status() is None
    assert "discarded" in caplog.text and recording_id in caplog.text
    assert not (tmp_path / "performance").exists() or not any((tmp_path / "performance").iterdir())
