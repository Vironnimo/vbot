"""Always-on performance measurement for the vBot server process.

Instrumented code records through module-level functions into one
process-wide sink, the same way it logs: ``record_duration``, ``record_span``,
``set_gauge`` and the ``measure`` context manager. This is the documented
exception to constructor injection; the sink holds only measurements, never
behavior other code depends on.

Durations are milliseconds in log-scale histograms; gauges keep their last
value. Metric names are low-cardinality dotted lowercase words and never carry
ids or content. While a recording is active, measurements given a ``track``
also become Chrome Trace Event spans for Perfetto.

The Runtime-owned :class:`PerformanceService` runs the Event Loop monitor and
stall watchdog and owns recordings and their files.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from core.performance._metrics import MetricRegistry
from core.performance._monitor import LoopMonitor, StallRecord
from core.performance._recording import (
    STOPPED_MAX_SECONDS,
    STOPPED_REQUESTED,
    Recording,
    list_recordings,
    new_recording_id,
    write_recording,
)
from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("performance")

MAX_METRIC_NAMES = 1000
DROPPED_METRICS_GAUGE = "performance.dropped_metrics"
RUNTIME_TRACK = "runtime"
DEFAULT_RECORDING_SECONDS = 300
MAX_RECORDING_SECONDS = 3600
MAX_RECORDING_EVENTS = 500_000
MAX_LABEL_LENGTH = 200
RETAINED_RECORDINGS = 20
RETAINED_STALLS = 50
EVENT_LOOP_LAG_METRIC = "event_loop.lag"
_STALL_WARNING_MS = 1000.0
_STALL_WARNING_INTERVAL_S = 30.0
_STALL_WARNING_FRAMES = 5


class PerformanceError(VBotError):
    """Base error of the performance measurement domain."""


class RecordingActiveError(PerformanceError):
    """Raised when a recording is started while another one is active."""


class RecordingInactiveError(PerformanceError):
    """Raised when a recording is stopped while none is active."""


class _Sink:
    """Process-wide histograms, gauges and the active recording."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.started_at = datetime.now(UTC)
            self.started_perf = perf_counter()
            self.metrics = MetricRegistry(max_names=MAX_METRIC_NAMES)
            self.recording: Recording | None = None
            self.gauge_tracks: dict[str, str] = {}
            self.dropped = 0
            self.drop_warned = False

    def observe(self, metric: str, ms: float) -> None:
        if not self.metrics.observe(metric, ms):
            self.drop()
            return
        recording = self.recording
        if recording is not None:
            recording.observe(metric, ms)

    def set_gauge(self, name: str, value: float, track: str | None) -> None:
        if not self.metrics.set_gauge(name, value):
            self.drop()
            return
        if track is not None and self.gauge_tracks.get(name) != track:
            self.gauge_tracks[name] = track
        recording = self.recording
        if recording is not None:
            recording.gauge(name, value, self.gauge_tracks.get(name, RUNTIME_TRACK), perf_counter())

    def drop(self) -> None:
        with self.lock:
            self.dropped += 1
            dropped = self.dropped
            warn = not self.drop_warned
            self.drop_warned = True
        self.metrics.set_gauge(DROPPED_METRICS_GAUGE, dropped, capped=False)
        if warn:
            _LOGGER.warning(
                "Performance metric name limit reached (limit=%d); new names are dropped",
                MAX_METRIC_NAMES,
            )


_SINK = _Sink()


class Measurement:
    """Context manager returned by :func:`measure`; usable across awaits."""

    __slots__ = ("_args", "_keep", "_metric", "_name", "_span", "_started", "_track")

    def __init__(
        self,
        metric: str,
        track: str | None,
        name: str | None,
        args: dict[str, Any] | None,
    ) -> None:
        self._metric = metric
        self._track = track
        self._name = name
        self._args = args
        self._started = 0.0
        self._span: Any = None
        self._keep = True

    def __enter__(self) -> Measurement:
        started = perf_counter()
        self._started = started
        if self._track is not None:
            recording = _SINK.recording
            if recording is not None:
                self._span = recording.open_span(self._track, started)
        return self

    def __exit__(self, *_exc_info: object) -> None:
        ended = perf_counter()
        if self._keep:
            _SINK.observe(self._metric, (ended - self._started) * 1000.0)
        span = self._span
        if span is not None:
            self._span = None
            span.recording.close_span(
                span,
                self._started,
                ended,
                name=self._name or self._metric,
                category=_category(self._metric),
                args=self._args,
                keep=self._keep and _SINK.recording is span.recording,
            )

    def annotate(self, **args: Any) -> None:
        """Add span args (ids, counts or names only; never content)."""
        self._args = {**(self._args or {}), **args}

    def discard(self) -> None:
        """Record neither the histogram observation nor the span."""
        self._keep = False


def record_duration(metric: str, ms: float) -> None:
    """Add one duration in milliseconds to the ``metric`` histogram."""
    _SINK.observe(metric, ms)


def set_gauge(name: str, value: float, *, track: str | None = None) -> None:
    """Store the latest value of one gauge; ``track`` places its trace counter."""
    _SINK.set_gauge(name, value, track)


def measure(
    metric: str,
    *,
    track: str | None = None,
    name: str | None = None,
    args: dict[str, Any] | None = None,
) -> Measurement:
    """Measure the wall-clock duration of a ``with`` block into ``metric``.

    The histogram is always updated. With ``track`` and an active recording,
    the block also becomes a span named ``name`` (default: the metric).
    """
    return Measurement(metric, track, name, args)


def record_span(
    metric: str,
    started: float,
    *,
    ended: float | None = None,
    track: str | None = None,
    name: str | None = None,
    args: dict[str, Any] | None = None,
    min_span_ms: float = 0.0,
) -> None:
    """Record a duration that began at ``started`` (a ``time.perf_counter()`` value).

    The histogram is always updated. A span is emitted only with ``track``, an
    active recording that began before ``started``, and at least ``min_span_ms``.
    """
    end = perf_counter() if ended is None else ended
    ms = (end - started) * 1000.0
    _SINK.observe(metric, ms)
    if track is None or ms < min_span_ms:
        return
    recording = _SINK.recording
    if recording is not None:
        recording.add_span(
            track, started, end, name=name or metric, category=_category(metric), args=args
        )


def session_track(agent_id: str, session_id: str, project_id: str | None = None) -> str:
    """Return the trace track name of one Session address."""
    if project_id:
        return f"{agent_id}@{project_id}/{session_id}"
    return f"{agent_id}/{session_id}"


def reset_for_tests() -> None:
    """Clear every process-wide measurement and detach any recording (tests only)."""
    _SINK.reset()


def _category(metric: str) -> str:
    return metric.split(".", 1)[0]


class PerformanceService:
    """Own the Event Loop monitor, stall watchdog, recordings and their files."""

    def __init__(
        self,
        recordings_dir: Path,
        *,
        samplers: Mapping[str, Callable[[], float]] | None = None,
        monitor_interval_s: float = 0.1,
        sample_interval_s: float = 1.0,
        stall_threshold_s: float = 0.25,
        watchdog_cadence_s: float = 0.05,
        max_recording_events: int = MAX_RECORDING_EVENTS,
        retained_recordings: int = RETAINED_RECORDINGS,
    ) -> None:
        self._recordings_dir = recordings_dir
        self._max_recording_events = max_recording_events
        self._retained_recordings = retained_recordings
        self._monitor = LoopMonitor(
            interval_s=monitor_interval_s,
            sample_interval_s=sample_interval_s,
            stall_threshold_s=stall_threshold_s,
            watchdog_cadence_s=watchdog_cadence_s,
            samplers=samplers or {},
            record_lag=self._record_lag,
            record_gauge=self._record_gauge,
            on_stall=self._record_stall,
        )
        # Worker pools record their own metrics through this module, so the pool
        # type is imported only once a service exists, never at module import.
        from core.utils.workers import BoundedWorkerPool

        self._workers = BoundedWorkerPool(name="performance", max_workers=2)
        self._stall_lock = threading.Lock()
        self._stalls: deque[dict[str, Any]] = deque(maxlen=RETAINED_STALLS)
        self._last_stall_warning: float | None = None
        self._suppressed_stall_warnings = 0
        self._recording: Recording | None = None
        self._auto_stop: asyncio.TimerHandle | None = None
        self._finishing: set[asyncio.Task[dict[str, Any]]] = set()

    @property
    def monitoring(self) -> bool:
        """Return whether the Event Loop monitor is running."""
        return self._monitor.running

    def start(self) -> None:
        """Start the monitor and watchdog on the running Event Loop."""
        self._monitor.start()

    def stop(self) -> None:
        """Discard this service's active recording and stop monitoring."""
        self._discard_recording()
        self._monitor.stop()
        self._workers.shutdown(wait=False)

    async def aclose(self) -> None:
        """Discard the active recording, finish pending writes and stop monitoring."""
        self._discard_recording()
        await self._monitor.aclose()
        if self._finishing:
            await asyncio.gather(*self._finishing, return_exceptions=True)
        self._workers.shutdown(wait=False)

    async def snapshot(self) -> dict[str, Any]:
        """Return process-wide metrics, gauges, recent stalls and recording status."""
        return await self._workers.run(self._snapshot)

    def start_recording(
        self,
        *,
        label: str | None = None,
        max_seconds: int = DEFAULT_RECORDING_SECONDS,
    ) -> dict[str, Any]:
        """Start the single active recording on the running Event Loop."""
        _validate_recording_request(label, max_seconds)
        loop = asyncio.get_running_loop()
        started_perf = perf_counter()
        with _SINK.lock:
            if _SINK.recording is not None:
                raise RecordingActiveError("A performance recording is already active")
            recording = Recording(
                recording_id=new_recording_id(self._recordings_dir),
                label=label,
                max_seconds=max_seconds,
                max_events=self._max_recording_events,
                max_metric_names=MAX_METRIC_NAMES,
                started_at=datetime.now(UTC),
                started_perf=started_perf,
            )
            _SINK.recording = recording
            gauge_tracks = dict(_SINK.gauge_tracks)
        self._recording = recording
        for gauge, value in _SINK.metrics.gauges().items():
            recording.gauge(gauge, value, gauge_tracks.get(gauge, RUNTIME_TRACK), started_perf)
        self._auto_stop = loop.call_later(max_seconds, self._stop_at_limit, recording)
        _LOGGER.info(
            "Performance recording started (recording=%s max_seconds=%d)",
            recording.recording_id,
            max_seconds,
        )
        return recording.status(perf_counter())

    async def stop_recording(self) -> dict[str, Any]:
        """Stop the active recording and write its trace and summary files."""
        recording = self._detach(STOPPED_REQUESTED)
        return await self._finish(recording)

    def recording_status(self) -> dict[str, Any] | None:
        """Return the active recording status, or ``None``."""
        recording = _SINK.recording
        return None if recording is None else recording.status(perf_counter())

    async def list_recordings(self, *, limit: int = RETAINED_RECORDINGS) -> list[dict[str, Any]]:
        """Return retained recordings newest first."""
        return await self._workers.run(list_recordings, self._recordings_dir, limit=limit)

    def _snapshot(self) -> dict[str, Any]:
        with self._stall_lock:
            stalls = list(self._stalls)
        return {
            "started_at": _SINK.started_at.isoformat(),
            "uptime_seconds": round(perf_counter() - _SINK.started_perf, 3),
            "metrics": _SINK.metrics.histogram_summaries(),
            "gauges": _SINK.metrics.gauges(),
            "stalls": stalls,
            "recording": self.recording_status(),
        }

    def _detach(self, reason: str, expected: Recording | None = None) -> Recording:
        ended = perf_counter()
        with _SINK.lock:
            recording = _SINK.recording
            if recording is None or (expected is not None and recording is not expected):
                raise RecordingInactiveError("No performance recording is active")
            _SINK.recording = None
            recording.close(ended, reason)
        if self._recording is recording:
            self._recording = None
            if self._auto_stop is not None:
                self._auto_stop.cancel()
                self._auto_stop = None
        return recording

    async def _finish(self, recording: Recording) -> dict[str, Any]:
        try:
            result = await self._workers.run(
                write_recording,
                recording,
                self._recordings_dir,
                keep=self._retained_recordings,
            )
        except Exception:
            _LOGGER.error(
                "Writing a performance recording failed (recording=%s)",
                recording.recording_id,
                exc_info=True,
            )
            raise
        _LOGGER.info(
            "Performance recording stopped (recording=%s reason=%s duration_seconds=%.1f "
            "events=%d truncated=%s)",
            recording.recording_id,
            result["stopped_reason"],
            result["duration_seconds"],
            result["event_count"],
            result["truncated"],
        )
        return result

    def _stop_at_limit(self, recording: Recording) -> None:
        self._auto_stop = None
        try:
            detached = self._detach(STOPPED_MAX_SECONDS, expected=recording)
        except RecordingInactiveError:
            return
        task = asyncio.get_running_loop().create_task(
            self._finish(detached), name="vbot-performance-recording-finish"
        )
        self._finishing.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[dict[str, Any]]) -> None:
        self._finishing.discard(task)
        if not task.cancelled():
            # _finish already logged a failure; retrieving it keeps asyncio quiet.
            task.exception()

    def _discard_recording(self) -> None:
        recording = self._recording
        if recording is None:
            return
        try:
            self._detach("discarded", expected=recording)
        except RecordingInactiveError:
            self._recording = None
            return
        _LOGGER.info(
            "Performance recording discarded at shutdown (recording=%s)", recording.recording_id
        )

    def _record_lag(self, lag_ms: float, at: float) -> None:
        _SINK.observe(EVENT_LOOP_LAG_METRIC, lag_ms)
        recording = _SINK.recording
        if recording is not None:
            recording.counter(EVENT_LOOP_LAG_METRIC, round(lag_ms, 3), RUNTIME_TRACK, at)

    @staticmethod
    def _record_gauge(name: str, value: float) -> None:
        _SINK.set_gauge(name, value, RUNTIME_TRACK)

    def _record_stall(self, stall: StallRecord) -> None:
        record = stall.to_dict()
        with self._stall_lock:
            self._stalls.append(record)
        recording = _SINK.recording
        if recording is not None:
            recording.add_stall(record, stall.started_perf, RUNTIME_TRACK)
        if stall.duration_ms >= _STALL_WARNING_MS:
            self._warn_stall(stall)

    def _warn_stall(self, stall: StallRecord) -> None:
        now = perf_counter()
        with self._stall_lock:
            last = self._last_stall_warning
            if last is not None and now - last < _STALL_WARNING_INTERVAL_S:
                self._suppressed_stall_warnings += 1
                return
            suppressed = self._suppressed_stall_warnings
            self._suppressed_stall_warnings = 0
            self._last_stall_warning = now
        frames = stall.samples[0][1][:_STALL_WARNING_FRAMES] if stall.samples else ()
        _LOGGER.warning(
            "Event Loop stalled for %d ms (samples=%d suppressed_warnings=%d); top frames: %s",
            round(stall.duration_ms),
            sum(count for count, _stack in stall.samples),
            suppressed,
            " <- ".join(frames) or "-",
        )


def _validate_recording_request(label: str | None, max_seconds: int) -> None:
    if label is not None and (
        not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_LENGTH
    ):
        raise ValueError(
            f"label must be a non-empty string of at most {MAX_LABEL_LENGTH} characters"
        )
    if (
        not isinstance(max_seconds, int)
        or isinstance(max_seconds, bool)
        or not 1 <= max_seconds <= MAX_RECORDING_SECONDS
    ):
        raise ValueError(f"max_seconds must be an integer from 1 to {MAX_RECORDING_SECONDS}")
