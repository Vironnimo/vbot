"""Vitest benchmark integration: name parameters, name patterns and JSON parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.perf_bench_suite.frontend as frontend
from scripts.perf_bench_suite.frontend import (
    benchmark_files,
    case_insensitive_pattern,
    parse_vitest_report,
    run_frontend_benchmarks,
    split_benchmark_name,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_split_benchmark_name_types_trailing_parameters():
    assert split_benchmark_name(
        "markdown.stream[20k] (items=201, unit=chunk, chars=20006, ratio=0.5)"
    ) == ("markdown.stream[20k]", {"items": 201, "unit": "chunk", "chars": 20006, "ratio": 0.5})
    assert split_benchmark_name("timeline.history[1000msg]") == ("timeline.history[1000msg]", {})
    assert split_benchmark_name("odd (not parameters)") == ("odd (not parameters)", {})


def test_case_insensitive_pattern_escapes_regex_syntax():
    assert case_insensitive_pattern("Md.s[2k]") == r"[mM][dD]\.[sS]\[2[kK]\]"


def test_parse_vitest_report_converts_milliseconds_and_extracts_items(tmp_path: Path):
    webui = tmp_path / "webui"
    document = {
        "files": [
            {
                "filepath": str(webui / "src/lib/__benchmarks__/markdown.bench.js"),
                "groups": [
                    {
                        "fullName": "src/lib/__benchmarks__/markdown.bench.js",
                        "benchmarks": [
                            {
                                "name": "markdown.stream[2k] (items=21, unit=chunk, chars=2012)",
                                "median": 0.3263,
                                "min": 0.3131,
                                "max": 1.9062,
                                "mean": 0.3606,
                                "p99": 1.5767,
                                "moe": 0.0131,
                                "sd": 0.1578,
                                "sampleCount": 555,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    [record] = parse_vitest_report(document, webui_dir=webui)

    assert record.name == "markdown.stream[2k]"
    assert (record.group, record.source) == ("markdown", "vitest")
    assert record.params == {"chars": 2012}
    assert (record.items, record.item_unit) == (21, "chunk")
    assert record.median_ns == pytest.approx(326_300.0)
    assert record.p99_ns == pytest.approx(1_576_700.0)
    assert record.p90_ns is None
    assert record.band_low_ns == pytest.approx(326_300.0 - 13_100.0)
    assert record.band_high_ns == pytest.approx(326_300.0 + 13_100.0)
    assert record.samples == 555
    assert record.iterations is None
    assert record.description == "Vitest bench in src/lib/__benchmarks__/markdown.bench.js"


def test_frontend_run_is_skipped_without_npx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(frontend.shutil, "which", lambda _name: None)

    run = run_frontend_benchmarks(
        tmp_path, output_path=tmp_path / "out.json", name_filter=None, time_ms=100
    )

    assert run.status == "skipped"
    assert run.records == []


def test_benchmark_files_are_found_in_benchmark_folders():
    files = {path.as_posix() for path in benchmark_files(PROJECT_ROOT / "webui")}

    assert {
        "src/lib/__benchmarks__/markdown.bench.js",
        "src/lib/__benchmarks__/chatTimeline.bench.js",
    } <= files
