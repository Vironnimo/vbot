"""Smoke test: every Python benchmark still sets up and runs against current vBot code.

The ~1000-message variants share their code path with the ~100-message ones and
are skipped to keep this fast. One suite context per test worker shares the
persisted history and Adapter fixtures, as a real suite run does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from scripts.perf_bench_suite.catalog import BENCHMARKS
from scripts.perf_bench_suite.runner import BenchContext, Benchmark, iteration_timer

_SMOKE_BENCHMARKS = [benchmark for benchmark in BENCHMARKS if "1000msg" not in benchmark.name]


@pytest.fixture(scope="module")
def bench_context(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BenchContext]:
    loop = asyncio.new_event_loop()
    context = BenchContext(tmp_path_factory.mktemp("perf-bench"), loop)
    try:
        yield context
    finally:
        context.close()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def test_benchmarks_have_unique_names_in_known_groups():
    names = [benchmark.name for benchmark in BENCHMARKS]

    assert len(names) == len(set(names))
    assert {benchmark.group for benchmark in BENCHMARKS} == {
        "sessions",
        "chat",
        "provider",
        "server",
    }


@pytest.mark.parametrize("benchmark", _SMOKE_BENCHMARKS, ids=lambda benchmark: benchmark.name)
def test_benchmark_sets_up_and_runs_one_operation(
    benchmark: Benchmark, bench_context: BenchContext
):
    prepared = benchmark.setup(bench_context)
    elapsed = iteration_timer(prepared.operation, bench_context.loop)(1)

    assert elapsed > 0
    assert prepared.items >= 1
