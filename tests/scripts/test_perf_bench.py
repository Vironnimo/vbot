"""Smoke test of the hot-path microbenchmark CLI (``scripts/perf_bench.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.perf_bench as perf_bench


def test_a_quick_backend_run_measures_the_selected_benchmark_and_writes_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = perf_bench.main(
        [
            "--backend",
            "--quick",
            "--samples",
            "1",
            "--target-seconds",
            "0.001",
            "--filter",
            "sessions.append",
            "--output-dir",
            str(tmp_path),
        ]
    )

    report = json.loads((tmp_path / "bench-latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert [result["name"] for result in report["results"]] == ["sessions.append[1kb]"]
    assert report["frontend"]["status"] == "skipped"
    assert "sessions.append[1kb]" in capsys.readouterr().out
