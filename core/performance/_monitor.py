"""Event Loop lag monitor, resource sampler and stall watchdog.

The monitor is a task on the observed loop: each tick sleeps one interval,
records how late it woke, and publishes its next deadline. About once per
second it samples loop utilization, process resources and injected gauges.

The watchdog is a daemon thread. When the published deadline is overdue by
more than the stall threshold, it samples the loop thread's Python stack on
every cadence tick and aggregates identical stacks until the loop ticks again.
Stack frames carry only code locations.
"""

from __future__ import annotations

import asyncio
import os
import sys
import sysconfig
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

import psutil  # type: ignore[import-untyped]

from core.utils.logging import get_logger

_LOGGER = get_logger("performance")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAX_STACK_FRAMES = 40
_MAX_DISTINCT_STACKS = 20
_MEGABYTE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StallRecord:
    """One finished Event Loop stall with its aggregated stack samples."""

    started_perf: float
    started_at: datetime
    duration_ms: float
    samples: tuple[tuple[int, tuple[str, ...]], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(self.duration_ms, 1),
            "samples": [{"count": count, "stack": list(stack)} for count, stack in self.samples],
        }


@dataclass(slots=True)
class _StallCapture:
    deadline: float
    started_at: datetime
    stacks: dict[tuple[str, ...], int] = field(default_factory=dict)


class LoopMonitor:
    """Own one monitor task and its watchdog thread for one Event Loop."""

    def __init__(
        self,
        *,
        interval_s: float,
        sample_interval_s: float,
        stall_threshold_s: float,
        watchdog_cadence_s: float,
        samplers: Mapping[str, Callable[[], float]],
        record_lag: Callable[[float, float], None],
        record_gauge: Callable[[str, float], None],
        on_stall: Callable[[StallRecord], None],
    ) -> None:
        self._interval_s = interval_s
        self._sample_interval_s = sample_interval_s
        self._stall_threshold_s = stall_threshold_s
        self._watchdog_cadence_s = watchdog_cadence_s
        self._samplers = dict(samplers)
        self._record_lag = record_lag
        self._record_gauge = record_gauge
        self._on_stall = on_stall
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # (deadline, previous wake) published by the loop as one atomic reference.
        self._heartbeat: tuple[float, float] | None = None
        self._loop_thread_id: int | None = None
        self._failed_samplers: set[str] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start on the running Event Loop; a no-op while already running."""
        if self.running:
            return
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._loop_thread_id = threading.get_ident()
        self._heartbeat = None
        self._stop = threading.Event()
        self._task = loop.create_task(self._run(), name="vbot-performance-monitor")
        self._thread = threading.Thread(
            target=self._watch,
            args=(self._stop, loop),
            name="vbot-performance-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Cancel the monitor task and join the watchdog thread."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            loop = task.get_loop()
            # A closed loop never runs the task again; cancelling it would raise.
            with suppress(RuntimeError):
                if threading.get_ident() == self._loop_thread_id:
                    task.cancel()
                elif not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)
        self._stop_watchdog()

    async def aclose(self) -> None:
        """Cancel and await the monitor task, then join the watchdog thread."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._stop_watchdog()

    def _stop_watchdog(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            # The watchdog waits on the stop event, so it exits at once unless it
            # is mid-sample; the timeout only bounds a pathological shutdown.
            thread.join(timeout=1.0)
        self._heartbeat = None

    async def _run(self) -> None:
        interval = self._interval_s
        process = psutil.Process(os.getpid())
        process.cpu_percent(None)
        wall_mark = time.perf_counter()
        cpu_mark = time.thread_time()
        next_sample = wall_mark + self._sample_interval_s
        woke = wall_mark
        while True:
            deadline = time.perf_counter() + interval
            self._heartbeat = (deadline, woke)
            await asyncio.sleep(interval)
            woke = time.perf_counter()
            self._record_lag(max(0.0, (woke - deadline) * 1000.0), woke)
            if woke < next_sample:
                continue
            cpu_now = time.thread_time()
            wall_elapsed = woke - wall_mark
            if wall_elapsed > 0:
                self._record_gauge(
                    "event_loop.utilization", round((cpu_now - cpu_mark) / wall_elapsed, 4)
                )
            wall_mark, cpu_mark = woke, cpu_now
            next_sample = woke + self._sample_interval_s
            self._sample_process(process)
            self._sample_injected()

    def _sample_process(self, process: psutil.Process) -> None:
        # Only per-process queries run here, on the loop. The OS thread count
        # would need a system-wide process scan on Windows (milliseconds, holding
        # the GIL); the Python thread count covers vBot's own pools and threads.
        try:
            with process.oneshot():
                cpu_percent = process.cpu_percent(None)
                rss_mb = process.memory_info().rss / _MEGABYTE
        except psutil.Error:
            self._sampler_failed("process")
            return
        self._record_gauge("process.cpu_percent", round(cpu_percent, 1))
        self._record_gauge("process.rss_mb", round(rss_mb, 1))
        self._record_gauge("process.python_threads", threading.active_count())
        self._record_gauge("asyncio.tasks", len(asyncio.all_tasks()))

    def _sample_injected(self) -> None:
        for name, sampler in self._samplers.items():
            try:
                value = sampler()
            except Exception:
                self._sampler_failed(name)
                continue
            self._record_gauge(name, value)

    def _sampler_failed(self, name: str) -> None:
        if name in self._failed_samplers:
            return
        self._failed_samplers.add(name)
        _LOGGER.warning("Performance gauge sampling failed (gauge=%s)", name, exc_info=True)

    def _watch(self, stop: threading.Event, loop: asyncio.AbstractEventLoop) -> None:
        capture: _StallCapture | None = None
        while not stop.wait(self._watchdog_cadence_s):
            if loop.is_closed():
                return
            heartbeat = self._heartbeat
            if heartbeat is None:
                continue
            deadline, woke = heartbeat
            if capture is not None and deadline != capture.deadline:
                self._finish_stall(capture, woke)
                capture = None
            now = time.perf_counter()
            overdue = now - deadline
            if overdue <= self._stall_threshold_s:
                continue
            if capture is None:
                capture = _StallCapture(
                    deadline=deadline,
                    started_at=datetime.now(UTC) - timedelta(seconds=overdue),
                )
            self._sample_stack(capture)

    def _finish_stall(self, capture: _StallCapture, resumed: float) -> None:
        samples = sorted(capture.stacks.items(), key=lambda item: item[1], reverse=True)
        record = StallRecord(
            started_perf=capture.deadline,
            started_at=capture.started_at,
            duration_ms=max(0.0, (resumed - capture.deadline) * 1000.0),
            samples=tuple((count, stack) for stack, count in samples),
        )
        try:
            self._on_stall(record)
        except Exception:
            _LOGGER.warning("Recording an Event Loop stall failed", exc_info=True)

    def _sample_stack(self, capture: _StallCapture) -> None:
        thread_id = self._loop_thread_id
        if thread_id is None:
            return
        frame = sys._current_frames().get(thread_id)  # noqa: SLF001 - sampling profiler access.
        if frame is None:
            return
        stack = render_stack(frame)
        if stack in capture.stacks:
            capture.stacks[stack] += 1
        elif len(capture.stacks) < _MAX_DISTINCT_STACKS:
            capture.stacks[stack] = 1


def render_stack(frame: FrameType | None) -> tuple[str, ...]:
    """Render ``path:line function`` frames, innermost first."""
    rendered: list[str] = []
    while frame is not None and len(rendered) < _MAX_STACK_FRAMES:
        code = frame.f_code
        rendered.append(f"{code_path(code.co_filename)}:{frame.f_lineno} {code.co_qualname}")
        frame = frame.f_back
    return tuple(rendered)


_PATH_CACHE: dict[str, str] = {}
_LIBRARY_ROOTS = tuple(
    sorted(
        {
            Path(path).resolve()
            for key in ("stdlib", "platstdlib", "purelib", "platlib")
            if (path := sysconfig.get_paths().get(key))
        },
        key=lambda path: len(path.parts),
        reverse=True,
    )
)


def code_path(filename: str) -> str:
    """Return a code location relative to vBot or its Python library root."""
    cached = _PATH_CACHE.get(filename)
    if cached is not None:
        return cached
    rendered = _render_code_path(filename)
    if len(_PATH_CACHE) < 4096:
        _PATH_CACHE[filename] = rendered
    return rendered


def _render_code_path(filename: str) -> str:
    if filename.startswith("<"):
        return filename
    try:
        path = Path(filename).resolve()
    except (OSError, ValueError):
        return filename.replace("\\", "/")
    for root in _LIBRARY_ROOTS:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    if path.is_relative_to(_PROJECT_ROOT):
        return path.relative_to(_PROJECT_ROOT).as_posix()
    return path.as_posix()
