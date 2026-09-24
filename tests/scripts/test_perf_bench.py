"""perf_bench.py command line: listing, option handling and report writing."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.perf_bench as perf_bench
from scripts.perf_bench_suite.frontend import FrontendRun
from scripts.perf_bench_suite.report import ResultRecord, build_report


def test_list_prints_python_benchmarks_and_vitest_files(capsys: pytest.CaptureFixture[str]):
    assert perf_bench.main(["--list", "--filter", "sessions."]) == 0

    output = capsys.readouterr().out
    assert "sessions.append[1kb]" in output
    assert "chat.request_wire" not in output
    assert "webui/src/lib/__benchmarks__/markdown.bench.js" in output


def test_run_without_matching_benchmarks_still_writes_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    exit_code = perf_bench.main(
        ["--backend", "--quick", "--filter", "no-such-benchmark", "--output-dir", str(tmp_path)]
    )

    assert exit_code == 0
    report = json.loads((tmp_path / "bench-latest.json").read_text(encoding="utf-8"))
    assert report["results"] == []
    assert report["settings"]["samples"] == 3
    assert report["settings"]["only"] == "backend"
    assert report["frontend"]["status"] == "skipped"
    assert "No benchmark ran." in capsys.readouterr().out


def _vitest_record(name: str, median_ns: float) -> ResultRecord:
    return ResultRecord(
        name=name,
        group=name.split(".", 1)[0],
        source="vitest",
        description="",
        median_ns=median_ns,
        min_ns=median_ns,
        max_ns=median_ns,
        mean_ns=median_ns,
        band_low_ns=median_ns * 0.98,
        band_high_ns=median_ns * 1.02,
        samples=50,
        p99_ns=median_ns,
    )


def test_compare_covers_only_the_benchmarks_this_run_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    baseline_path = tmp_path / "baseline.json"
    baseline = build_report(
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        environment={},
        settings={},
        records=[
            _vitest_record("markdown.stream[2k]", 1_000_000.0),
            _vitest_record("timeline.history[1000msg]", 1_000_000.0),
            replace(_vitest_record("sessions.append[1kb]", 1_000_000.0), source="python"),
        ],
        failures=[],
        frontend={"status": "ok"},
    )
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(
        perf_bench,
        "_run_frontend",
        lambda _filter, _time_ms: FrontendRun(
            "ok", records=[_vitest_record("markdown.stream[2k]", 1_500_000.0)]
        ),
    )

    exit_code = perf_bench.main(
        [
            "--frontend",
            "--filter",
            "markdown",
            "--compare",
            str(baseline_path),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    comparison = capsys.readouterr().out.split("Compared with", 1)[1]
    assert exit_code == 0
    assert "markdown.stream[2k]" in comparison
    assert "+50.0%" in comparison and "slower" in comparison
    assert "timeline.history" not in comparison
    assert "sessions.append" not in comparison


def test_compare_with_a_missing_baseline_fails_before_running(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        perf_bench.main(["--backend", "--compare", str(tmp_path / "missing.json")])


def test_sampling_options_reject_non_positive_values():
    with pytest.raises(SystemExit):
        perf_bench.build_parser().parse_args(["--samples", "0"])
    with pytest.raises(SystemExit):
        perf_bench.build_parser().parse_args(["--target-seconds", "-1"])
