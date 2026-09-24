"""Recording buffers, lanes, Chrome trace files, listing and retention."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.performance._recording import (
    SUMMARY_SUFFIX,
    TRACE_SUFFIX,
    Recording,
    list_recordings,
    new_recording_id,
    prune_recordings,
    write_recording,
)
from core.utils.ids import is_safe_id


def _recording(*, max_events: int = 1000, started_perf: float = 100.0) -> Recording:
    return Recording(
        recording_id="perf_0000000000aa",
        label="baseline",
        max_seconds=60,
        max_events=max_events,
        max_metric_names=100,
        started_at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        started_perf=started_perf,
    )


def _spans(recording: Recording) -> list[tuple]:
    return [event for event in recording.events() if event[0] == "X"]


def _args(event: tuple) -> dict[str, Any]:
    args = event[7]
    assert isinstance(args, dict)
    return args


def test_overlapping_spans_take_distinct_lanes_and_free_lanes_are_reused() -> None:
    recording = _recording()
    first = recording.open_span("agent/ses_1", 100.1)
    second = recording.open_span("agent/ses_1", 100.2)
    assert first is not None and second is not None
    assert first.tid != second.tid

    recording.close_span(second, 100.2, 100.3, name="b", category="chat", args=None, keep=True)
    recording.close_span(first, 100.1, 100.4, name="a", category="chat", args=None, keep=True)
    third = recording.open_span("agent/ses_1", 100.5)
    assert third is not None
    assert third.tid == 0

    lanes = {event[5]: event[2] for event in _spans(recording)}
    assert lanes == {"a": first.tid, "b": second.tid}


def test_spans_on_one_lane_never_overlap_including_retroactive_spans() -> None:
    recording = _recording()
    live = recording.open_span("sqlite", 100.5)
    assert live is not None
    # A retroactive span overlapping the open live span must use another lane,
    # and one overlapping a finished span on lane 0 as well.
    recording.add_span("sqlite", 100.4, 100.6, name="late", category="sqlite", args=None)
    recording.close_span(live, 100.5, 100.9, name="live", category="sqlite", args=None, keep=True)
    recording.add_span("sqlite", 100.8, 101.0, name="overlap", category="sqlite", args=None)
    recording.add_span("sqlite", 101.0, 101.1, name="after", category="sqlite", args=None)

    by_lane: dict[int, list[tuple[int, int]]] = {}
    for event in _spans(recording):
        by_lane.setdefault(event[2], []).append((event[3], event[3] + event[4]))
    for spans in by_lane.values():
        spans.sort()
        for (_start, end), (next_start, _next_end) in zip(spans, spans[1:], strict=False):
            assert end <= next_start
    names = {event[5]: event[2] for event in _spans(recording)}
    assert names["late"] != names["live"]
    assert names["overlap"] != names["live"]
    assert names["after"] == 0


def test_tracks_map_to_named_processes_and_lanes_to_named_threads() -> None:
    recording = _recording()
    recording.add_span("worker pool session-io", 100.1, 100.2, name="w", category="x", args=None)
    recording.add_span("rpc", 100.1, 100.2, name="r", category="rpc", args=None)
    recording.add_span("rpc", 100.15, 100.25, name="r2", category="rpc", args=None)

    metadata = [event for event in recording.events() if event[0] == "M"]
    processes = {_args(event)["name"]: event[1] for event in metadata if event[5] == "process_name"}
    threads = {
        (event[1], event[2]): _args(event)["name"]
        for event in metadata
        if event[5] == "thread_name"
    }

    assert processes == {"worker pool session-io": 1, "rpc": 2}
    assert threads == {(1, 0): "lane 0", (2, 0): "lane 0", (2, 1): "lane 1"}


def test_spans_starting_before_the_recording_or_after_close_are_dropped() -> None:
    recording = _recording(started_perf=100.0)
    recording.add_span("rpc", 99.9, 100.1, name="early", category="rpc", args=None)
    live = recording.open_span("rpc", 100.2)
    assert live is not None
    assert recording.close(100.5, "requested")
    recording.close_span(live, 100.2, 100.6, name="late", category="rpc", args=None, keep=True)
    recording.add_span("rpc", 100.3, 100.4, name="closed", category="rpc", args=None)

    assert _spans(recording) == []
    assert recording.open_span("rpc", 100.7) is None
    assert not recording.close(100.8, "requested")


def test_event_buffer_is_bounded_and_marks_truncation() -> None:
    recording = _recording(max_events=3)
    for index in range(10):
        start = 100.0 + index / 10
        recording.add_span("rpc", start, start + 0.01, name="r", category="rpc", args=None)

    status = recording.status(101.0)

    assert recording.event_count == 3
    assert recording.truncated is True
    assert status["event_count"] == 3
    assert status["truncated"] is True


def test_gauges_emit_counters_and_keep_window_maxima() -> None:
    recording = _recording()
    recording.gauge("runs.active", 2, "runtime", 100.1)
    recording.gauge("runs.active", 5, "runtime", 100.2)
    recording.gauge("runs.active", 1, "runtime", 100.3)
    recording.counter("event_loop.lag", 3.5, "runtime", 100.4)
    recording.observe("rpc.chat.send", 12.0)

    counters = [event for event in recording.events() if event[0] == "C"]
    summary = recording.summary()

    assert [_args(event)["value"] for event in counters] == [2, 5, 1, 3.5]
    assert summary["gauges_max"] == {"runs.active": 5}
    assert summary["metrics"]["rpc.chat.send"]["count"] == 1
    assert "event_loop.lag" not in summary["gauges_max"]


def test_written_trace_is_valid_chrome_trace_event_json(tmp_path: Path) -> None:
    recording = _recording()
    recording.add_span(
        "main/ses_1",
        100.001,
        100.004,
        name="provider.response",
        category="provider",
        args={"run_id": "run_1"},
    )
    recording.gauge("process.rss_mb", 80.5, "runtime", 100.002)
    recording.add_stall(
        {
            "started_at": "2026-09-24T12:00:00.100000+00:00",
            "duration_ms": 320.0,
            "samples": [{"count": 2, "stack": ["core/x.py:3 work"]}],
        },
        100.1,
        "runtime",
    )
    recording.close(101.0, "requested")

    result = write_recording(recording, tmp_path, keep=20)

    trace_path = tmp_path / f"{recording.recording_id}{TRACE_SUFFIX}"
    document = json.loads(trace_path.read_text(encoding="utf-8"))
    assert set(document) == {"traceEvents", "displayTimeUnit"}
    assert document["displayTimeUnit"] == "ms"
    events = document["traceEvents"]
    span = next(event for event in events if event["ph"] == "X")
    assert span == {
        "ph": "X",
        "pid": span["pid"],
        "tid": 0,
        "ts": 1000,
        "dur": 3000,
        "name": "provider.response",
        "cat": "provider",
        "args": {"run_id": "run_1"},
    }
    counter = next(event for event in events if event["ph"] == "C")
    assert counter["name"] == "process.rss_mb" and counter["args"] == {"value": 80.5}
    stall = next(event for event in events if event["ph"] == "i")
    assert stall["s"] == "p" and stall["ts"] == 100_000
    assert stall["args"]["samples"][0]["stack"] == ["core/x.py:3 work"]
    assert {event["ph"] for event in events} == {"M", "X", "C", "i"}
    assert result["trace_path"] == trace_path.absolute().as_posix()
    assert "\\" not in result["trace_path"]
    assert result["duration_seconds"] == 1.0
    assert result["stopped_reason"] == "requested"
    assert result["event_count"] == len(events)
    assert result["summary"]["gauges_max"] == {"process.rss_mb": 80.5}
    assert len(result["summary"]["stalls"]) == 1


def test_large_traces_are_streamed_in_valid_chunks(tmp_path: Path) -> None:
    recording = _recording(max_events=5000)
    for index in range(2500):
        start = 100.0 + index / 10_000
        recording.add_span("rpc", start, start + 0.00001, name="r", category="rpc", args=None)
    recording.close(101.0, "max_seconds")

    write_recording(recording, tmp_path, keep=20)

    document = json.loads((tmp_path / "perf_0000000000aa.trace.json").read_text("utf-8"))
    assert len(document["traceEvents"]) == recording.event_count


def test_listing_returns_newest_first_and_skips_invalid_summaries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for recording_id, started in (("perf_000000000001", "01"), ("perf_000000000002", "02")):
        (tmp_path / f"{recording_id}{SUMMARY_SUFFIX}").write_text(
            json.dumps(
                {
                    "recording_id": recording_id,
                    "label": None,
                    "started_at": f"2026-09-{started}T00:00:00+00:00",
                    "stopped_at": f"2026-09-{started}T00:01:00+00:00",
                    "duration_seconds": 60.0,
                    "event_count": 4,
                    "truncated": False,
                    "stopped_reason": "requested",
                    "summary": {"metrics": {}, "gauges_max": {}, "stalls": []},
                }
            ),
            encoding="utf-8",
        )
    (tmp_path / f"perf_000000000003{SUMMARY_SUFFIX}").write_text("{broken", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="vbot.performance"):
        entries = list_recordings(tmp_path, limit=10)

    assert [entry["recording_id"] for entry in entries] == [
        "perf_000000000002",
        "perf_000000000001",
    ]
    assert set(entries[0]) == {
        "recording_id",
        "label",
        "started_at",
        "duration_seconds",
        "event_count",
        "truncated",
        "stopped_reason",
        "trace_path",
    }
    assert entries[0]["trace_path"].endswith("/perf_000000000002.trace.json")
    assert list_recordings(tmp_path, limit=1)[0]["recording_id"] == "perf_000000000002"
    assert "perf_000000000003" in caplog.text
    assert "{broken" not in caplog.text
    assert list_recordings(tmp_path / "missing", limit=10) == []


def test_pruning_keeps_the_newest_recording_pairs(tmp_path: Path) -> None:
    for index in range(4):
        for suffix in (TRACE_SUFFIX, SUMMARY_SUFFIX):
            path = tmp_path / f"perf_00000000000{index}{suffix}"
            path.write_text("{}", encoding="utf-8")
            os.utime(path, ns=(index * 1_000_000_000, index * 1_000_000_000))
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    prune_recordings(tmp_path, keep=2)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "notes.txt",
        "perf_000000000002.summary.json",
        "perf_000000000002.trace.json",
        "perf_000000000003.summary.json",
        "perf_000000000003.trace.json",
    ]


def test_new_recording_ids_are_safe_and_avoid_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / f"perf_taken{TRACE_SUFFIX}").write_text("{}", encoding="utf-8")
    candidates = iter(["perf_taken", "perf_free"])
    monkeypatch.setattr(
        "core.performance._recording.new_id",
        lambda prefix, claim: next(candidate for candidate in candidates if claim(candidate)),
    )

    assert new_recording_id(tmp_path) == "perf_free"
    monkeypatch.undo()
    generated = new_recording_id(tmp_path)
    assert generated.startswith("perf_") and is_safe_id(generated)
