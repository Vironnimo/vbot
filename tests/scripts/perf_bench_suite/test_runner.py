"""Benchmark runner: calibration, sampling, statistics, selection and failures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts.perf_bench_suite.runner import (
    BenchContext,
    Benchmark,
    Prepared,
    RunSettings,
    calibrate,
    iteration_timer,
    measure,
    percentile,
    run_benchmarks,
    select_benchmarks,
    summarize,
)


def _fixed_cost_timer(cost_ns: int, calls: list[int] | None = None):
    def time_iterations(iterations: int) -> int:
        if calls is not None:
            calls.append(iterations)
        return cost_ns * iterations

    return time_iterations


def test_calibrate_scales_iterations_to_the_target_sample_time():
    calls: list[int] = []

    iterations = calibrate(
        _fixed_cost_timer(1_000, calls), target_ns=200_000_000, max_iterations=10**9
    )

    assert iterations == 200_000
    # Growth stops once a run reaches a tenth of the target.
    assert calls[-1] * 1_000 >= 20_000_000
    assert len(calls) <= 4


def test_calibrate_uses_one_iteration_for_an_operation_slower_than_the_target():
    calls: list[int] = []

    assert (
        calibrate(_fixed_cost_timer(500_000_000, calls), target_ns=200_000_000, max_iterations=100)
        == 1
    )
    assert calls == [1]


def test_calibrate_is_capped_by_max_iterations_even_when_the_clock_reads_zero():
    calls: list[int] = []

    assert (
        calibrate(_fixed_cost_timer(0, calls), target_ns=200_000_000, max_iterations=5_000) == 5_000
    )
    assert calls[-1] == 5_000


def test_measure_warms_up_collects_between_samples_and_reports_per_operation_costs():
    events: list[object] = []

    def time_iterations(iterations: int) -> int:
        events.append(iterations)
        return 2_000 * iterations

    measurement = measure(
        time_iterations,
        RunSettings(samples=3, target_sample_seconds=0.001),
        collect=lambda: events.append("gc"),
    )

    assert events[0] == 1  # warm-up
    assert measurement.iterations == 500
    assert measurement.samples_ns == (2_000.0, 2_000.0, 2_000.0)
    assert events[-6:] == ["gc", 500, "gc", 500, "gc", 500]


def test_iteration_timer_awaits_coroutine_operations_on_the_given_loop():
    loop = asyncio.new_event_loop()
    calls: list[asyncio.AbstractEventLoop] = []

    async def operation() -> None:
        calls.append(asyncio.get_running_loop())

    try:
        elapsed = iteration_timer(operation, loop)(4)
    finally:
        loop.close()

    assert calls == [loop] * 4
    assert elapsed >= 0


def test_percentile_interpolates_linearly_between_ranks():
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.9) == pytest.approx(3.7)
    assert percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert percentile([7.0], 0.9) == 7.0
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_summarize_reports_order_statistics():
    summary = summarize([10.0, 30.0, 20.0, 40.0])

    assert summary.median_ns == 25.0
    assert summary.p90_ns == pytest.approx(37.0)
    assert summary.min_ns == 10.0
    assert summary.max_ns == 40.0
    assert summary.mean_ns == 25.0
    assert summary.stdev_ns == pytest.approx(12.909944)
    assert summarize([5.0]).stdev_ns == 0.0


def _benchmark(name: str) -> Benchmark:
    return Benchmark(name=name, description="", setup=lambda _context: Prepared(lambda: None))


def test_select_benchmarks_matches_a_case_insensitive_substring():
    benchmarks = [_benchmark("sessions.append[1kb]"), _benchmark("chat.request_wire[100msg]")]

    assert [b.name for b in select_benchmarks(benchmarks, "SESSIONS.")] == ["sessions.append[1kb]"]
    assert select_benchmarks(benchmarks, None) == benchmarks
    assert select_benchmarks(benchmarks, "missing") == []


def test_run_benchmarks_records_a_failure_and_measures_the_rest(tmp_path: Path):
    loop = asyncio.new_event_loop()
    context = BenchContext(tmp_path, loop)

    def broken(_context: BenchContext) -> Prepared:
        raise RuntimeError("fixture exploded")

    benchmarks = [
        Benchmark("group.broken", "fails in setup", broken),
        Benchmark(
            "group.ok[small]",
            "trivial",
            lambda _context: Prepared(
                lambda: sum(range(10)), params={"n": 10}, items=10, item_unit="x"
            ),
        ),
    ]
    try:
        records, failures = run_benchmarks(
            benchmarks, context, RunSettings(samples=2, target_sample_seconds=0.001)
        )
    finally:
        context.close()
        loop.close()

    assert [failure.name for failure in failures] == ["group.broken"]
    assert failures[0].error == "RuntimeError: fixture exploded"
    assert "fixture exploded" in failures[0].traceback
    [record] = records
    assert (record.name, record.group, record.source) == ("group.ok[small]", "group", "python")
    assert record.samples == 2
    assert record.params == {"n": 10}
    assert record.band_low_ns == record.min_ns and record.band_high_ns == record.max_ns
    assert record.median_per_item_ns == pytest.approx(record.median_ns / 10)


def test_bench_context_builds_fixtures_once_and_runs_every_cleanup(tmp_path: Path):
    loop = asyncio.new_event_loop()
    context = BenchContext(tmp_path, loop)
    order: list[str] = []
    builds: list[int] = []

    async def close_async() -> None:
        order.append("async")

    def fail() -> None:
        order.append("fail")
        raise OSError("cleanup failed")

    def build() -> object:
        builds.append(len(builds) + 1)
        return object()

    first = context.fixture("key", build)
    assert context.fixture("key", build) is first
    context.add_cleanup(lambda: order.append("first"))
    context.add_cleanup(fail)
    context.add_cleanup(close_async)
    try:
        with pytest.raises(OSError, match="cleanup failed"):
            context.close()
    finally:
        loop.close()

    assert builds == [1]
    assert order == ["async", "fail", "first"]


def test_run_settings_reject_invalid_values():
    with pytest.raises(ValueError):
        RunSettings(samples=0)
    with pytest.raises(ValueError):
        RunSettings(target_sample_seconds=0)
