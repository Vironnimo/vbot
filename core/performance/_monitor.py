"""Event Loop lag monitor, resource sampler and stall watchdog.

The monitor is a task on the observed loop: each tick sleeps one interval,
records how late it woke, and publishes its next deadline. About once per
second it samples loop utilization, process resources and injected gauges.

The watchdog is a daemon thread. When the published deadline is overdue by
more than the stall threshold, it samples the loop thread's Python stack on
every cadence tick and aggregates identical stacks until the loop ticks again.
Where the platform reports per-thread CPU time, it also reads it for every
Python thread and samples the stacks of the other threads that ran since the
previous tick; the stall names those that used a noticeable share of CPU, which
is how a thread holding the GIL shows up. Stack frames carry only code
locations.

Work that runs in a Task of its own shows only that Task's frames on the loop
thread's stack. Once per stall, the first sample that finds the loop running a
Task also captures the asyncio call graph and keeps the frames of the
suspended Tasks awaiting it, which name the Run or service that started it.

While running, the monitor also times cyclic garbage collections: each tick
records the finished collections, and a stall reports how much of it was
collection pause.
"""

from __future__ import annotations

import asyncio
import os
import sys
import sysconfig
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

import psutil  # type: ignore[import-untyped]

from core.performance._gc import GcObserver
from core.performance._thread_cpu import ThreadCpuReader, thread_cpu_reader
from core.utils.logging import get_logger

_LOGGER = get_logger("performance")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAX_STACK_FRAMES = 40
_MAX_DISTINCT_STACKS = 20
_MAX_THREAD_STACKS = 5
_MAX_STALL_THREADS = 5
# Other threads are named in a stall from this share of its CPU window on.
_STALL_THREAD_CPU_SHARE = 0.1
_MEGABYTE = 1024 * 1024


Samples = tuple[tuple[int, tuple[str, ...]], ...]
# (file name, line, qualified name) per frame, innermost first. Stalls keep
# stacks raw and render them when finished: rendering resolves file paths,
# and each file system call releases the GIL, which under GIL contention
# costs a switch interval to get back.
_RawStack = tuple[tuple[str, int | None, str], ...]


@dataclass(frozen=True, slots=True)
class StallThread:
    """Another thread that used CPU during a stall, with its sampled stacks."""

    name: str
    cpu_ms: float
    samples: Samples

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "cpu_ms": round(self.cpu_ms, 1), **_samples_dict(self.samples)}


@dataclass(frozen=True, slots=True)
class StallRecord:
    """One finished Event Loop stall with its aggregated stack samples.

    ``cpu_window_ms`` is the stretch from the first sample to the resume over
    which ``loop_cpu_ms`` and the ``threads`` CPU were measured; all three are
    empty where the platform reports no per-thread CPU time. ``awaited_by``
    holds the frames of the Tasks awaiting the Task the loop ran, innermost
    first; it is empty when the loop ran no Task, nothing awaited it, or the
    call graph could not be read.
    """

    started_perf: float
    started_at: datetime
    duration_ms: float
    samples: Samples
    gc_ms: float = 0.0
    cpu_window_ms: float | None = None
    loop_cpu_ms: float | None = None
    threads: tuple[StallThread, ...] = ()
    awaited_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(self.duration_ms, 1),
            "gc_ms": round(self.gc_ms, 1),
            "cpu_window_ms": _rounded(self.cpu_window_ms),
            "loop_cpu_ms": _rounded(self.loop_cpu_ms),
            **_samples_dict(self.samples),
            "awaited_by": list(self.awaited_by),
            "threads": [thread.to_dict() for thread in self.threads],
        }


def _samples_dict(samples: Samples) -> dict[str, Any]:
    return {"samples": [{"count": count, "stack": list(stack)} for count, stack in samples]}


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


@dataclass(slots=True)
class _ThreadCapture:
    name: str
    cpu_start: float
    cpu_last: float
    stacks: dict[_RawStack, int] = field(default_factory=dict)


@dataclass(slots=True)
class _StallCapture:
    deadline: float
    started_at: datetime
    gc_total_s: float
    stacks: dict[_RawStack, int] = field(default_factory=dict)
    # Per-thread CPU by thread ident, from the first CPU read on.
    cpu_started: float | None = None
    threads: dict[int, _ThreadCapture] = field(default_factory=dict)
    # The awaiters of the Task the loop runs; None until a sample found one.
    awaited_by: _RawStack | None = None


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
        record_gc: Callable[[int, float, float], None],
        on_stall: Callable[[StallRecord], None],
    ) -> None:
        self._interval_s = interval_s
        self._sample_interval_s = sample_interval_s
        self._stall_threshold_s = stall_threshold_s
        self._watchdog_cadence_s = watchdog_cadence_s
        self._samplers = dict(samplers)
        self._record_lag = record_lag
        self._record_gauge = record_gauge
        self._record_gc = record_gc
        self._on_stall = on_stall
        self._gc = GcObserver()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # (deadline, previous wake, collection seconds so far) published by the
        # loop as one atomic reference.
        self._heartbeat: tuple[float, float, float] | None = None
        self._loop_thread_id: int | None = None
        self._failed_samplers: set[str] = set()
        self._read_cpu: ThreadCpuReader | None = thread_cpu_reader()

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
        self._gc.install()
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
        self._gc.uninstall()
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
            self._heartbeat = (deadline, woke, self._gc.total_s)
            await asyncio.sleep(interval)
            woke = time.perf_counter()
            self._record_lag(max(0.0, (woke - deadline) * 1000.0), woke)
            for generation, started, ended in self._gc.drain():
                self._record_gc(generation, started, ended)
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
            deadline, woke, gc_total_s = heartbeat
            if capture is not None and deadline != capture.deadline:
                self._finish_stall(capture, woke, gc_total_s)
                capture = None
            now = time.perf_counter()
            overdue = now - deadline
            if overdue <= self._stall_threshold_s:
                continue
            if capture is None:
                capture = _StallCapture(
                    deadline=deadline,
                    started_at=datetime.now(UTC) - timedelta(seconds=overdue),
                    gc_total_s=gc_total_s,
                )
            self._sample(capture, loop)

    def _finish_stall(self, capture: _StallCapture, resumed: float, gc_total_s: float) -> None:
        duration_ms = max(0.0, (resumed - capture.deadline) * 1000.0)
        # Both totals come from heartbeats, so the window opens up to one tick
        # before the stall; the cap keeps that slack out of the reported share.
        gc_ms = min(duration_ms, max(0.0, (gc_total_s - capture.gc_total_s) * 1000.0))
        cpu_window_ms = loop_cpu_ms = None
        threads: tuple[StallThread, ...] = ()
        if capture.cpu_started is not None:
            self._sample_threads(capture, None)
            cpu_window_ms = (time.perf_counter() - capture.cpu_started) * 1000.0
            loop = capture.threads.pop(self._loop_thread_id or 0, None)
            if loop is not None:
                loop_cpu_ms = (loop.cpu_last - loop.cpu_start) * 1000.0
            threads = _busy_threads(capture.threads.values(), cpu_window_ms)
        record = StallRecord(
            started_perf=capture.deadline,
            started_at=capture.started_at,
            duration_ms=duration_ms,
            samples=_sorted_samples(capture.stacks),
            gc_ms=gc_ms,
            cpu_window_ms=cpu_window_ms,
            loop_cpu_ms=loop_cpu_ms,
            threads=threads,
            awaited_by=_render_raw(capture.awaited_by or ()),
        )
        try:
            self._on_stall(record)
        except Exception:
            _LOGGER.warning("Recording an Event Loop stall failed", exc_info=True)

    def _sample(self, capture: _StallCapture, loop: asyncio.AbstractEventLoop) -> None:
        frames = sys._current_frames()  # noqa: SLF001 - sampling profiler access.
        thread_id = self._loop_thread_id
        frame = frames.get(thread_id) if thread_id is not None else None
        if frame is not None:
            _count_stack(capture.stacks, _raw_stack(frame), _MAX_DISTINCT_STACKS)
        if capture.awaited_by is None:
            capture.awaited_by = _awaiter_stack(loop)
        if self._read_cpu is not None:
            self._sample_threads(capture, frames)

    def _sample_threads(
        self, capture: _StallCapture, frames: Mapping[int, FrameType] | None
    ) -> None:
        """Read every thread's CPU; sample the stacks of those that ran since the last read.

        A thread is sampled when first seen, since its earlier CPU is unknown;
        only threads with enough CPU during the stall are reported. Without
        ``frames`` it only reads CPU, for the end of the stall.
        """
        read_cpu = self._read_cpu
        if read_cpu is None:
            return
        if capture.cpu_started is None:
            capture.cpu_started = time.perf_counter()
        watchdog = threading.get_ident()
        for thread in threading.enumerate():
            ident, native_id = thread.ident, thread.native_id
            if ident is None or native_id is None or ident == watchdog:
                continue
            cpu = read_cpu(native_id)
            if cpu is None:
                continue
            state = capture.threads.get(ident)
            ran = state is None or cpu > state.cpu_last
            if state is None:
                state = capture.threads[ident] = _ThreadCapture(thread.name, cpu, cpu)
            state.cpu_last = cpu
            frame = frames.get(ident) if frames is not None and ran else None
            if frame is not None and ident != self._loop_thread_id:
                _count_stack(state.stacks, _raw_stack(frame), _MAX_THREAD_STACKS)


def _count_stack(stacks: dict[_RawStack, int], stack: _RawStack, limit: int) -> None:
    if stack in stacks:
        stacks[stack] += 1
    elif len(stacks) < limit:
        stacks[stack] = 1


def _sorted_samples(stacks: Mapping[_RawStack, int]) -> Samples:
    ordered = sorted(stacks.items(), key=lambda item: item[1], reverse=True)
    return tuple((count, _render_raw(stack)) for stack, count in ordered)


def _busy_threads(
    threads: Iterable[_ThreadCapture], cpu_window_ms: float
) -> tuple[StallThread, ...]:
    minimum = max(cpu_window_ms * _STALL_THREAD_CPU_SHARE, 1.0)
    busy = [
        StallThread(
            name=thread.name,
            cpu_ms=(thread.cpu_last - thread.cpu_start) * 1000.0,
            samples=_sorted_samples(thread.stacks),
        )
        for thread in threads
        if (thread.cpu_last - thread.cpu_start) * 1000.0 >= minimum
    ]
    busy.sort(key=lambda thread: thread.cpu_ms, reverse=True)
    return tuple(busy[:_MAX_STALL_THREADS])


def render_stack(frame: FrameType | None) -> tuple[str, ...]:
    """Render ``path:line function`` frames, innermost first."""
    return _render_raw(_raw_stack(frame))


def _raw_stack(frame: FrameType | None) -> _RawStack:
    raw: list[tuple[str, int | None, str]] = []
    while frame is not None and len(raw) < _MAX_STACK_FRAMES:
        code = frame.f_code
        raw.append((code.co_filename, frame.f_lineno, code.co_qualname))
        frame = frame.f_back
    return tuple(raw)


def _awaiter_stack(loop: asyncio.AbstractEventLoop) -> _RawStack | None:
    """Return the frames awaiting the Task ``loop`` runs; ``None`` while it runs none.

    The running Task's own frames are on the loop thread's stack; the Tasks
    awaiting it are suspended and appear only in its asyncio call graph. The
    chain follows the first awaiter of each level, innermost first. Reading the
    graph from another thread is not thread-safe, since the loop thread may
    change it meanwhile: a failed read yields no frames rather than a guess.
    """
    try:
        task = asyncio.current_task(loop)
        if task is None:
            return None
        graph = asyncio.capture_call_graph(task)
        raw: list[tuple[str, int | None, str]] = []
        awaiters = graph.awaited_by if graph is not None else ()
        while awaiters and len(raw) < _MAX_STACK_FRAMES:
            awaiter = awaiters[0]
            for entry in awaiter.call_stack:
                frame = entry.frame
                # A coroutine that finished meanwhile has no frame any more.
                if frame is not None:
                    code = frame.f_code
                    raw.append((code.co_filename, frame.f_lineno, code.co_qualname))
            awaiters = awaiter.awaited_by
    except Exception:
        return ()
    return tuple(raw[:_MAX_STACK_FRAMES])


def _render_raw(stack: _RawStack) -> tuple[str, ...]:
    return tuple(f"{code_path(filename)}:{line} {name}" for filename, line, name in stack)


_PATH_CACHE_LIMIT = 4096
_PATH_CACHE: dict[str, str] = {}
# Rendered paths of vBot's own files, collected as frames are rendered.
_PROJECT_PATHS: set[str] = set()
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
    rendered, owned = _render_code_path(filename)
    if len(_PATH_CACHE) < _PATH_CACHE_LIMIT:
        _PATH_CACHE[filename] = rendered
        if owned:
            _PROJECT_PATHS.add(rendered)
    return rendered


def is_project_frame(frame: str) -> bool:
    """Return whether a rendered frame lies in vBot's own source tree."""
    return frame.partition(" ")[0].rpartition(":")[0] in _PROJECT_PATHS


def _render_code_path(filename: str) -> tuple[str, bool]:
    if filename.startswith("<"):
        return filename, False
    try:
        path = Path(filename).resolve()
    except OSError, ValueError:
        return filename.replace("\\", "/"), False
    for root in _LIBRARY_ROOTS:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix(), False
    if path.is_relative_to(_PROJECT_ROOT):
        return path.relative_to(_PROJECT_ROOT).as_posix(), True
    return path.as_posix(), False
