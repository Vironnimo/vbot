"""Smoke test of the hot-path microbenchmark CLI (``scripts/perf_bench.py``)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import scripts.perf_bench as perf_bench
from scripts.perf_bench_suite.catalog import BENCHMARKS
from scripts.perf_bench_suite.runner import BenchContext, iteration_timer


def test_a_quick_backend_run_measures_the_selected_benchmark_and_writes_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = perf_bench.main(
        ["--backend", "--quick", "--filter", "sessions.append", "--output-dir", str(tmp_path)]
    )

    report = json.loads((tmp_path / "bench-latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert [result["name"] for result in report["results"]] == ["sessions.append[1kb]"]
    assert report["frontend"]["status"] == "skipped"
    assert "sessions.append[1kb]" in capsys.readouterr().out


def test_the_session_load_benchmark_runs_one_operation(tmp_path: Path) -> None:
    # Holds production behavior no owner test reaches yet: tool_result_facts in
    # core/chat/_step_outcomes.py skipping history messages that are not Tool Results.
    # Delete once tests/core/chat covers it.
    loop = asyncio.new_event_loop()
    context = BenchContext(tmp_path, loop, smoke=True)
    try:
        benchmark = next(item for item in BENCHMARKS if item.name == "sessions.load_active[100msg]")
        prepared = benchmark.setup(context)
        assert iteration_timer(prepared.operation, loop)(1) > 0
    finally:
        context.close()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
