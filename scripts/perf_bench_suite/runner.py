"""Benchmark model, iteration calibration, sampling and statistics.

The runner uses only the standard library. A benchmark's ``setup`` builds its
fixtures once and returns the operation to time. The operation is warmed up
once, calibrated so one sample lasts about ``target_sample_seconds`` (at least
one iteration), and then timed for ``samples`` samples with
``time.perf_counter_ns``. ``gc.collect()`` runs between samples while the
collector stays enabled during them, so allocation-heavy code pays its normal
collection cost. Coroutine operations run on the single Event Loop owned by
:class:`BenchContext`, and their timing is taken inside that loop.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import math
import statistics
import time
import traceback
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast

from scripts.perf_bench_suite.report import ResultRecord

T = TypeVar("T")

Operation = Callable[[], object]
"""A benchmark operation: a plain callable or an ``async def`` function."""

TimeIterations = Callable[[int], int]
"""Run an operation ``n`` times and return the elapsed nanoseconds."""


@dataclass(frozen=True)
class Prepared:
    """One benchmark operation whose fixtures are already built.

    ``params`` are the size parameters reported with the result. ``items`` and
    ``item_unit`` state how many units (frames, chunks, deltas, renders) one
    operation processes, so the report can derive a per-item cost.
    """

    operation: Operation
    params: dict[str, Any] = field(default_factory=dict)
    items: int = 1
    item_unit: str = ""


@dataclass(frozen=True)
class Benchmark:
    """A named benchmark whose setup builds fixtures and returns the operation."""

    name: str
    description: str
    setup: Callable[[BenchContext], Prepared]

    @property
    def group(self) -> str:
        return self.name.split(".", 1)[0]


class BenchContext:
    """Resources shared by one suite run: work directory, Event Loop, fixtures."""

    def __init__(self, work_dir: Path, loop: asyncio.AbstractEventLoop) -> None:
        self.work_dir = work_dir
        self.loop = loop
        self._fixtures: dict[str, object] = {}
        self._cleanups: list[Callable[[], object]] = []

    def run(self, awaitable: Coroutine[Any, Any, T]) -> T:
        """Run one coroutine to completion on the suite's Event Loop."""
        return self.loop.run_until_complete(awaitable)

    def fixture(self, key: str, build: Callable[[], T]) -> T:
        """Build a shared fixture once; later calls return the same instance."""
        if key not in self._fixtures:
            self._fixtures[key] = build()
        return cast(T, self._fixtures[key])

    def add_cleanup(self, cleanup: Callable[[], object]) -> None:
        """Register a cleanup; an awaitable result is awaited on the Event Loop."""
        self._cleanups.append(cleanup)

    def close(self) -> None:
        """Run every cleanup in reverse registration order, then re-raise a failure."""
        first_error: BaseException | None = None
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    self.loop.run_until_complete(cast(Awaitable[object], result))
            except Exception as exc:  # noqa: BLE001 - later cleanups must still run
                first_error = first_error or exc
        self._fixtures.clear()
        if first_error is not None:
            raise first_error


@dataclass(frozen=True)
class RunSettings:
    """Sampling policy shared by every Python benchmark of one suite run."""

    samples: int = 10
    target_sample_seconds: float = 0.2
    max_iterations: int = 1_000_000

    def __post_init__(self) -> None:
        if self.samples < 1:
            raise ValueError("samples must be at least 1")
        if self.target_sample_seconds <= 0:
            raise ValueError("target sample time must be positive")
        if self.max_iterations < 1:
            raise ValueError("max iterations must be at least 1")

    @property
    def target_sample_ns(self) -> int:
        return max(1, round(self.target_sample_seconds * 1e9))


@dataclass(frozen=True)
class Measurement:
    """Calibrated iteration count and the mean cost per operation of each sample."""

    iterations: int
    samples_ns: tuple[float, ...]


@dataclass(frozen=True)
class Summary:
    """Order statistics over per-operation sample costs, in nanoseconds."""

    median_ns: float
    p90_ns: float
    min_ns: float
    max_ns: float
    mean_ns: float
    stdev_ns: float


@dataclass(frozen=True)
class BenchFailure:
    """A benchmark whose setup or operation raised instead of producing a result."""

    name: str
    error: str
    traceback: str


def iteration_timer(operation: Operation, loop: asyncio.AbstractEventLoop) -> TimeIterations:
    """Return a function that times ``n`` consecutive runs of ``operation``.

    An ``async def`` operation is awaited ``n`` times inside one coroutine on
    ``loop``; the clock is read inside the loop, so loop start-up is excluded.
    """
    clock = time.perf_counter_ns
    if inspect.iscoroutinefunction(operation):
        coroutine_operation = cast(Callable[[], Awaitable[object]], operation)

        async def run_async(iterations: int) -> int:
            start = clock()
            for _ in range(iterations):
                await coroutine_operation()
            return clock() - start

        return lambda iterations: loop.run_until_complete(run_async(iterations))

    def run_sync(iterations: int) -> int:
        start = clock()
        for _ in range(iterations):
            operation()
        return clock() - start

    return run_sync


def calibrate(time_iterations: TimeIterations, *, target_ns: int, max_iterations: int) -> int:
    """Return the iteration count whose run lasts about ``target_ns``.

    The count grows until one timed run reaches a tenth of the target, which
    keeps timer resolution negligible, and is then scaled to the target from
    that run's per-iteration cost. The result is always within
    ``[1, max_iterations]``.
    """
    floor_ns = max(1, target_ns // 10)
    iterations = 1
    while True:
        elapsed = time_iterations(iterations)
        if elapsed >= floor_ns or iterations >= max_iterations:
            per_iteration = max(elapsed, 1) / iterations
            return max(1, min(max_iterations, round(target_ns / per_iteration)))
        grown = iterations * 10 if elapsed <= 0 else math.ceil(iterations * floor_ns * 2 / elapsed)
        iterations = min(max_iterations, max(iterations * 2, grown))


def measure(
    time_iterations: TimeIterations,
    settings: RunSettings,
    *,
    collect: Callable[[], object] = gc.collect,
) -> Measurement:
    """Warm up, calibrate and take ``settings.samples`` samples of one operation."""
    time_iterations(1)
    iterations = calibrate(
        time_iterations,
        target_ns=settings.target_sample_ns,
        max_iterations=settings.max_iterations,
    )
    samples: list[float] = []
    for _ in range(settings.samples):
        collect()
        samples.append(time_iterations(iterations) / iterations)
    return Measurement(iterations=iterations, samples_ns=tuple(samples))


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linearly interpolated percentile (NumPy's default); ``fraction`` is in [0, 1]."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("percentile fraction must be within [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(samples_ns: Sequence[float]) -> Summary:
    """Summarize per-operation sample costs."""
    if not samples_ns:
        raise ValueError("cannot summarize zero samples")
    return Summary(
        median_ns=statistics.median(samples_ns),
        p90_ns=percentile(samples_ns, 0.9),
        min_ns=min(samples_ns),
        max_ns=max(samples_ns),
        mean_ns=statistics.fmean(samples_ns),
        stdev_ns=statistics.stdev(samples_ns) if len(samples_ns) > 1 else 0.0,
    )


def record_from_measurement(
    benchmark: Benchmark, prepared: Prepared, measurement: Measurement
) -> ResultRecord:
    """Build the report record of one Python benchmark."""
    summary = summarize(measurement.samples_ns)
    return ResultRecord(
        name=benchmark.name,
        group=benchmark.group,
        source="python",
        description=benchmark.description,
        params=dict(prepared.params),
        median_ns=summary.median_ns,
        min_ns=summary.min_ns,
        max_ns=summary.max_ns,
        mean_ns=summary.mean_ns,
        band_low_ns=summary.min_ns,
        band_high_ns=summary.max_ns,
        p90_ns=summary.p90_ns,
        stdev_ns=summary.stdev_ns,
        samples=len(measurement.samples_ns),
        iterations=measurement.iterations,
        samples_ns=measurement.samples_ns,
        items=prepared.items,
        item_unit=prepared.item_unit,
    )


def select_benchmarks(benchmarks: Sequence[Benchmark], name_filter: str | None) -> list[Benchmark]:
    """Keep benchmarks whose name contains ``name_filter`` (case-insensitive)."""
    if not name_filter:
        return list(benchmarks)
    needle = name_filter.casefold()
    return [benchmark for benchmark in benchmarks if needle in benchmark.name.casefold()]


def run_benchmarks(
    benchmarks: Sequence[Benchmark],
    context: BenchContext,
    settings: RunSettings,
    *,
    progress: Callable[[str], None] = lambda _message: None,
) -> tuple[list[ResultRecord], list[BenchFailure]]:
    """Set up and measure each benchmark; a failure is recorded, not propagated."""
    records: list[ResultRecord] = []
    failures: list[BenchFailure] = []
    for index, benchmark in enumerate(benchmarks, start=1):
        progress(f"[{index}/{len(benchmarks)}] {benchmark.name}")
        try:
            prepared = benchmark.setup(context)
            measurement = measure(iteration_timer(prepared.operation, context.loop), settings)
        except Exception as exc:  # noqa: BLE001 - report every broken benchmark, keep going
            failures.append(
                BenchFailure(
                    name=benchmark.name,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            )
            continue
        records.append(record_from_measurement(benchmark, prepared, measurement))
    return records, failures
