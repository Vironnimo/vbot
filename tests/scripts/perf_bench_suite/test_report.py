"""Benchmark report: records, JSON files, comparison verdicts and table formatting."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.perf_bench_suite.report import (
    LATEST_REPORT_NAME,
    REPORT_SCHEMA,
    ResultRecord,
    build_report,
    compare,
    format_comparison_table,
    format_duration,
    format_rate,
    format_results_table,
    load_records,
    write_report,
)


def _record(name: str = "chat.request_wire[100msg]", **overrides: Any) -> ResultRecord:
    values: dict[str, Any] = {
        "name": name,
        "group": name.split(".", 1)[0],
        "source": "python",
        "description": "",
        "median_ns": 1_000_000.0,
        "min_ns": 950_000.0,
        "max_ns": 1_080_000.0,
        "mean_ns": 1_010_000.0,
        "band_low_ns": 950_000.0,
        "band_high_ns": 1_080_000.0,
        "samples": 10,
        "p90_ns": 1_060_000.0,
        "params": {"messages": 100},
        "iterations": 200,
        "samples_ns": (1_000_000.0, 990_000.0),
    }
    values.update(overrides)
    return ResultRecord(**values)


def test_record_round_trips_through_its_json_form_with_derived_values():
    record = _record(items=4, item_unit="chunk")

    data = record.to_dict()

    assert data["ops_per_second"] == pytest.approx(1000.0)
    assert data["median_per_item_ns"] == pytest.approx(250_000.0)
    assert ResultRecord.from_dict(json.loads(json.dumps(data))) == record


def test_compare_flags_changes_beyond_threshold_and_outside_the_old_band():
    old = [
        _record("a.slower"),
        _record("a.faster"),
        _record("a.small_change"),
        _record("a.inside_band", band_high_ns=1_300_000.0),
        _record("a.removed"),
    ]
    new = [
        _record("a.slower", median_ns=1_250_000.0),
        _record("a.faster", median_ns=800_000.0),
        _record("a.small_change", median_ns=1_090_000.0),
        _record("a.inside_band", median_ns=1_200_000.0),
        _record("a.new"),
    ]

    by_name = {item.name: item for item in compare(old, new, threshold_percent=10.0)}

    assert by_name["a.slower"].verdict == "slower"
    assert by_name["a.slower"].change_percent == pytest.approx(25.0)
    assert by_name["a.faster"].verdict == "faster"
    assert by_name["a.faster"].change_percent == pytest.approx(-20.0)
    # Outside the old band, but within the threshold.
    assert by_name["a.small_change"].verdict == "noise"
    # Beyond the threshold, but inside the old run's own spread.
    assert by_name["a.inside_band"].verdict == "noise"
    assert by_name["a.new"].verdict == "new"
    assert by_name["a.removed"].verdict == "removed"
    assert list(by_name)[-1] == "a.removed"


def test_compare_threshold_is_configurable_and_parameter_changes_are_marked():
    old = [_record("a.bench", params={"messages": 100})]
    new = [_record("a.bench", median_ns=1_150_000.0, params={"messages": 120})]

    [strict] = compare(old, new, threshold_percent=10.0)
    [lenient] = compare(old, new, threshold_percent=20.0)

    assert strict.verdict == "slower"
    assert strict.params_changed is True
    assert lenient.verdict == "noise"


def test_format_duration_and_rate_use_three_significant_digits():
    assert format_duration(None) == "-"
    assert format_duration(812.0) == "812 ns"
    assert format_duration(9_850.0) == "9.85 us"
    assert format_duration(24_900_000.0) == "24.9 ms"
    assert format_duration(1_500_000_000.0) == "1.50 s"
    assert format_rate(86.5) == "86.5"
    assert format_rate(4.2) == "4.20"
    assert format_rate(1_570.0) == "1.57k"
    assert format_rate(2_300_000.0) == "2.30M"


def test_results_table_lists_every_record_with_per_item_cost_and_legend():
    table = format_results_table(
        [
            _record(),
            _record(
                "provider.stream_parse[content_2000]",
                median_ns=20_000_000.0,
                items=2000,
                item_unit="chunk",
            ),
            _record(
                "markdown.stream[20k]",
                source="vitest",
                p90_ns=None,
                p99_ns=40_000_000.0,
                iterations=None,
                median_ns=35_000_000.0,
            ),
        ]
    )

    lines = table.splitlines()
    assert lines[0].split()[:3] == ["benchmark", "median", "tail"]
    assert "10.0 us/chunk" in table
    assert "40.0 ms" in table  # Vitest p99 in the tail column
    assert "10 x -" in table
    assert "tail = p90" in lines[-1]


def test_comparison_table_shows_signed_change_and_verdict():
    [comparison] = compare([_record()], [_record(median_ns=1_250_000.0)], threshold_percent=10.0)

    table = format_comparison_table([comparison], 10.0)

    assert "+25.0%" in table
    assert "slower" in table
    assert "10%" in table.splitlines()[-1]


def test_write_report_keeps_timestamped_files_and_refreshes_latest(tmp_path: Path):
    created = datetime(2026, 9, 24, 13, 51, 36, tzinfo=UTC)
    report = build_report(
        created_at=created,
        environment={"python": "3.14"},
        settings={"samples": 10},
        records=[_record()],
        failures=[],
        frontend={"status": "skipped"},
    )

    first = write_report(report, tmp_path / "out", created)
    second = write_report(report, tmp_path / "out", created)

    assert first.name == "bench-20260924T135136Z.json"
    assert second.name == "bench-20260924T135136Z-2.json"
    latest = json.loads((tmp_path / "out" / LATEST_REPORT_NAME).read_text(encoding="utf-8"))
    assert latest["schema"] == REPORT_SCHEMA
    assert latest["created_at"] == "2026-09-24T13:51:36+00:00"
    assert load_records(first) == [_record()]


def test_load_records_rejects_other_documents(tmp_path: Path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"files": []}), encoding="utf-8")

    with pytest.raises(ValueError, match=REPORT_SCHEMA):
        load_records(path)
