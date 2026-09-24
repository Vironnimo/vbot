"""One bounded trace recording and its retained Chrome Trace Event files.

A recording buffers compact event tuples while it is active and keeps its own
window histograms, gauge maxima and stalls. Each track becomes one trace
process; spans take the lowest free lane (trace thread) of their track, so
concurrent spans never overlap on one lane. Files are written only after the
recording is closed, off the Event Loop, in chunks so no single JSON encode
holds the interpreter for long.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from core.performance._metrics import MetricRegistry
from core.utils.atomic import atomic_write_stream, atomic_write_text
from core.utils.ids import is_safe_id, new_id
from core.utils.logging import get_logger

_LOGGER = get_logger("performance")

RECORDING_ID_PREFIX = "perf"
TRACE_SUFFIX = ".trace.json"
SUMMARY_SUFFIX = ".summary.json"
STOPPED_REQUESTED = "requested"
STOPPED_MAX_SECONDS = "max_seconds"
_ENCODE_CHUNK_EVENTS = 1000
_SUMMARY_FIELDS = (
    "recording_id",
    "label",
    "started_at",
    "duration_seconds",
    "event_count",
    "truncated",
    "stopped_reason",
)

# (ph, pid, tid, ts_us, dur_us, name, cat, args); tuples keep large buffers compact.
_Event = tuple[str, int, int, int, int | None, str, str | None, dict[str, Any] | None]


@dataclass(slots=True)
class _Lane:
    open: bool = False
    last_end: float = float("-inf")


@dataclass(slots=True)
class _Track:
    pid: int
    lanes: list[_Lane] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class OpenSpan:
    """A lane held by one live span until it closes."""

    recording: Recording
    track: _Track
    tid: int


class Recording:
    """Thread-safe event buffer and window statistics of one active recording."""

    def __init__(
        self,
        *,
        recording_id: str,
        label: str | None,
        max_seconds: int,
        max_events: int,
        max_metric_names: int,
        started_at: datetime,
        started_perf: float,
    ) -> None:
        self.recording_id = recording_id
        self.label = label
        self.max_seconds = max_seconds
        self.started_at = started_at
        self.started_perf = started_perf
        self.metrics = MetricRegistry(max_names=max_metric_names)
        self.gauges_max = MetricRegistry(max_names=max_metric_names)
        self._max_events = max_events
        self._lock = threading.Lock()
        self._events: list[_Event] = []
        self._tracks: dict[str, _Track] = {}
        self._stalls: list[dict[str, Any]] = []
        self._truncated = False
        self._closed = False
        self.stopped_perf: float | None = None
        self.stopped_reason: str | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def status(self, now_perf: float) -> dict[str, Any]:
        """Return the public status projection of this recording."""
        with self._lock:
            event_count = len(self._events)
            truncated = self._truncated
        return {
            "recording_id": self.recording_id,
            "label": self.label,
            "started_at": _iso(self.started_at),
            "elapsed_seconds": round(max(0.0, now_perf - self.started_perf), 3),
            "max_seconds": self.max_seconds,
            "event_count": event_count,
            "truncated": truncated,
        }

    def open_span(self, track: str, started: float) -> OpenSpan | None:
        """Hold the lowest free lane of ``track`` for a span starting now."""
        with self._lock:
            if self._closed:
                return None
            state = self._track(track)
            tid = self._free_lane(state, started)
            state.lanes[tid].open = True
            return OpenSpan(self, state, tid)

    def close_span(
        self,
        span: OpenSpan,
        started: float,
        ended: float,
        *,
        name: str,
        category: str,
        args: dict[str, Any] | None,
        keep: bool,
    ) -> None:
        """Release a lane and emit its complete event when the recording still runs."""
        with self._lock:
            lane = span.track.lanes[span.tid]
            lane.open = False
            lane.last_end = max(lane.last_end, ended)
            if self._closed or not keep:
                return
            self._append_span(span.track.pid, span.tid, started, ended, name, category, args)

    def add_span(
        self,
        track: str,
        started: float,
        ended: float,
        *,
        name: str,
        category: str,
        args: dict[str, Any] | None,
    ) -> None:
        """Emit a span measured before this call, when it began inside the recording."""
        if started < self.started_perf:
            return
        with self._lock:
            if self._closed:
                return
            state = self._track(track)
            tid = self._free_lane(state, started)
            state.lanes[tid].last_end = ended
            self._append_span(state.pid, tid, started, ended, name, category, args)

    def observe(self, metric: str, ms: float) -> None:
        if not self._closed:
            self.metrics.observe(metric, ms)

    def gauge(self, name: str, value: float, track: str, at: float) -> None:
        """Track a gauge maximum and emit its counter sample."""
        if self._closed:
            return
        self.gauges_max.raise_gauge(name, value)
        self.counter(name, value, track, at)

    def counter(self, name: str, value: float, track: str, at: float) -> None:
        """Emit one counter sample without touching the window statistics."""
        with self._lock:
            if self._closed:
                return
            pid = self._track(track).pid
            self._append(("C", pid, 0, self._ts(at), None, name, "gauge", {"value": value}))

    def add_stall(self, record: Mapping[str, Any], started: float, track: str) -> None:
        """Store one Event Loop stall and emit its instant event."""
        if started < self.started_perf:
            return
        with self._lock:
            if self._closed:
                return
            self._stalls.append(dict(record))
            pid = self._track(track).pid
            args = {"duration_ms": record["duration_ms"], "samples": record["samples"]}
            self._append(("i", pid, 0, self._ts(started), None, "event_loop.stall", "stall", args))

    def close(self, ended: float, reason: str) -> bool:
        """Stop accepting events; return False when already closed."""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self.stopped_perf = ended
            self.stopped_reason = reason
            return True

    def summary(self) -> dict[str, Any]:
        """Return the window histograms, gauge maxima and stalls of this recording."""
        with self._lock:
            stalls = [dict(stall) for stall in self._stalls]
        return {
            "metrics": self.metrics.histogram_summaries(),
            "gauges_max": self.gauges_max.gauges(),
            "stalls": stalls,
        }

    def events(self) -> list[_Event]:
        """Return the buffered events; call only after :meth:`close`."""
        with self._lock:
            return list(self._events)

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def truncated(self) -> bool:
        with self._lock:
            return self._truncated

    def _track(self, track: str) -> _Track:
        state = self._tracks.get(track)
        if state is None:
            state = self._tracks[track] = _Track(pid=len(self._tracks) + 1)
            self._append(("M", state.pid, 0, 0, None, "process_name", None, {"name": track}))
        return state

    def _free_lane(self, state: _Track, started: float) -> int:
        for tid, lane in enumerate(state.lanes):
            if not lane.open and lane.last_end <= started:
                return tid
        state.lanes.append(_Lane())
        tid = len(state.lanes) - 1
        self._append(("M", state.pid, tid, 0, None, "thread_name", None, {"name": f"lane {tid}"}))
        return tid

    def _append_span(
        self,
        pid: int,
        tid: int,
        started: float,
        ended: float,
        name: str,
        category: str,
        args: dict[str, Any] | None,
    ) -> None:
        start_us = self._ts(started)
        duration_us = max(0, self._ts(ended) - start_us)
        self._append(("X", pid, tid, start_us, duration_us, name, category, args))

    def _append(self, event: _Event) -> None:
        if len(self._events) >= self._max_events:
            self._truncated = True
            return
        self._events.append(event)

    def _ts(self, at: float) -> int:
        return round((at - self.started_perf) * 1_000_000)


def new_recording_id(directory: Path) -> str:
    """Return an unused recording id for ``directory``.

    Only one recording is active per process, so checking both file names is a
    sufficient claim for this single writer.
    """

    def claim(candidate: str) -> bool:
        return not any(
            (directory / f"{candidate}{suffix}").exists()
            for suffix in (TRACE_SUFFIX, SUMMARY_SUFFIX)
        )

    return new_id(RECORDING_ID_PREFIX, claim=claim)


def write_recording(recording: Recording, directory: Path, *, keep: int) -> dict[str, Any]:
    """Write the trace and summary of a closed recording, prune, and return its result."""
    if recording.stopped_perf is None or recording.stopped_reason is None:
        raise RuntimeError("Recording must be closed before it is written")
    duration_seconds = round(recording.stopped_perf - recording.started_perf, 3)
    events = recording.events()
    summary = recording.summary()
    trace_path = directory / f"{recording.recording_id}{TRACE_SUFFIX}"
    summary_path = directory / f"{recording.recording_id}{SUMMARY_SUFFIX}"
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_stream(trace_path, lambda handle: _write_trace(handle, events))
    document = {
        "recording_id": recording.recording_id,
        "label": recording.label,
        "started_at": _iso(recording.started_at),
        "stopped_at": _iso(recording.started_at + timedelta(seconds=duration_seconds)),
        "duration_seconds": duration_seconds,
        "event_count": len(events),
        "truncated": recording.truncated,
        "stopped_reason": recording.stopped_reason,
        "summary": summary,
    }
    # The summary is written last: its presence marks a complete recording.
    atomic_write_text(
        summary_path,
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
    )
    prune_recordings(directory, keep=keep)
    result = {name: document[name] for name in _SUMMARY_FIELDS}
    result["trace_path"] = _public_path(trace_path)
    result["summary"] = summary
    return result


def list_recordings(directory: Path, *, limit: int) -> list[dict[str, Any]]:
    """Return retained recordings newest first."""
    entries: list[dict[str, Any]] = []
    if not directory.is_dir():
        return entries
    for summary_path in directory.glob(f"{RECORDING_ID_PREFIX}_*{SUMMARY_SUFFIX}"):
        recording_id = summary_path.name.removesuffix(SUMMARY_SUFFIX)
        if not is_safe_id(recording_id):
            continue
        entry = _read_summary(summary_path, recording_id)
        if entry is None:
            continue
        entry["trace_path"] = _public_path(directory / f"{recording_id}{TRACE_SUFFIX}")
        entries.append(entry)
    entries.sort(key=lambda entry: (entry["started_at"], entry["recording_id"]), reverse=True)
    return entries[:limit]


def prune_recordings(directory: Path, *, keep: int) -> None:
    """Delete every recording file pair beyond the newest ``keep`` recordings."""
    groups: dict[str, list[Path]] = {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for path in entries:
        recording_id = _recording_id_for(path.name)
        if recording_id is not None:
            groups.setdefault(recording_id, []).append(path)
    ordered = sorted(
        groups.items(),
        key=lambda item: (max(_mtime_ns(path) for path in item[1]), item[0]),
        reverse=True,
    )
    for recording_id, paths in ordered[keep:]:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                _LOGGER.warning(
                    "Could not delete a pruned performance recording file (recording=%s)",
                    recording_id,
                )


def _write_trace(handle: BinaryIO, events: list[_Event]) -> None:
    handle.write(b'{"traceEvents":[')
    for start in range(0, len(events), _ENCODE_CHUNK_EVENTS):
        chunk = [_event_object(event) for event in events[start : start + _ENCODE_CHUNK_EVENTS]]
        encoded = json.dumps(chunk, separators=(",", ":"), ensure_ascii=False, default=str)
        if start:
            handle.write(b",")
        handle.write(encoded[1:-1].encode("utf-8"))
    handle.write(b'],"displayTimeUnit":"ms"}\n')


def _event_object(event: _Event) -> dict[str, Any]:
    phase, pid, tid, ts, dur, name, category, args = event
    item: dict[str, Any] = {"ph": phase, "pid": pid, "tid": tid, "ts": ts, "name": name}
    if dur is not None:
        item["dur"] = dur
    if category is not None:
        item["cat"] = category
    if args is not None:
        item["args"] = args
    if phase == "i":
        item["s"] = "p"
    return item


def _read_summary(path: Path, recording_id: str) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = None
    if (
        not isinstance(document, dict)
        or document.get("recording_id") != recording_id
        or any(name not in document for name in _SUMMARY_FIELDS)
    ):
        _LOGGER.warning(
            "Skipped an unreadable performance recording summary (recording=%s)", recording_id
        )
        return None
    return {name: document[name] for name in _SUMMARY_FIELDS}


def _recording_id_for(name: str) -> str | None:
    if not name.startswith(f"{RECORDING_ID_PREFIX}_"):
        return None
    for suffix in (TRACE_SUFFIX, SUMMARY_SUFFIX):
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return None


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _public_path(path: Path) -> str:
    return path.absolute().as_posix()


def _iso(value: datetime) -> str:
    return value.isoformat()
