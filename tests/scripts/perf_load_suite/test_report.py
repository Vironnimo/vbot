"""Console/Markdown rendering, result files and run comparison."""

import json

import pytest

from scripts.perf_load_suite.report import (
    RESULT_KIND,
    compare_results,
    format_value,
    get_path,
    load_result,
    render_console,
    render_markdown,
    write_result,
)


def _level(agents, *, ttft_overhead_p50, ok=True, lag_p99=None):
    return {
        "agents": agents,
        "status": "ok" if ok else "failed",
        "error": None if ok else "StackError: vBot server exited during startup",
        "notes": [],
        "files": {"runs": f"level-{agents:02d}/runs.json"},
        "client": {
            "runs": {"ok": agents, "total": agents, "failed": 0, "tool_calls": 3, "tool_errors": 0},
            "ttft_overhead_ms": {"p50": ttft_overhead_p50, "p95": ttft_overhead_p50 * 2},
            "delta_latency_ms": {"p50": 31.5, "p95": 48.0, "p99": 60.0},
            "step_overhead_by_tool_ms": {
                "read": {"gap": {"count": 1, "p50": 100.0, "p95": 100.0}, "tool_exec": {}}
            },
            "errors": [],
        },
        "server": {
            "event_loop_lag_ms": {"p50": 1.0, "p99": lag_p99, "max": 90.0},
            "sqlite_write_wait_ms": {"p50": 0.5, "p99": 30.0, "max": 120.0},
            "tools_ms": {"read": {"count": 2, "p50": 3.0, "p99": 5.0, "max": 5.0}},
            "worker_pools": [
                {"pool": "sqlite", "wait_count": 4, "wait_p99_ms": 12.0, "wait_max_ms": 20.0}
            ],
            "top_metrics": [],
            "stalls": {
                "count": 1,
                "worst": {"duration_ms": 250.0, "started_at": "t", "frames": ["inner", "outer"]},
            },
        },
    }


def _result(levels, *, started="2026-09-24T10:00:00+00:00", commit="abc"):
    return {
        "kind": RESULT_KIND,
        "schema": 1,
        "started_at": started,
        "finished_at": started,
        "config": {"turns": 3, "tools": ["read"], "steps": 4},
        "git": {"commit": commit, "branch": "main", "dirty": False},
        "machine": {"platform": "test", "python": "3.14"},
        "levels": levels,
    }


def test_values_are_formatted_by_magnitude():
    assert [format_value(value) for value in (None, True, 7, 1234.5, 12.345, 1.23456)] == [
        "-",
        "yes",
        "7",
        "1234",
        "12.3",
        "1.23",
    ]


def test_get_path_tolerates_missing_steps():
    assert get_path({"a": {"b": 1}}, "a.b") == 1
    assert get_path({"a": {"b": 1}}, "a.c") is None
    assert get_path({"a": 3}, "a.b") is None


def test_console_table_has_one_column_per_level_and_flags_failures():
    result = _result(
        [
            _level(1, ttft_overhead_p50=100.0, lag_p99=5.0),
            _level(10, ttft_overhead_p50=300.0, ok=False),
        ]
    )

    table = render_console(result)

    lines = table.splitlines()
    assert lines[0].split() == ["Metric", "1", "agent", "10", "agents"]
    assert any(
        line.startswith("TTFT overhead p50 / p95 ms") and "100 / 200" in line for line in lines
    )
    assert any("Event loop lag" in line and "5.00" in line for line in lines)
    assert "10 agents FAILED" in table
    # Rows without any value in any level are left out.
    assert "UI long tasks" not in table


def test_markdown_report_contains_summary_and_level_details():
    markdown = render_markdown(_result([_level(1, ttft_overhead_p50=100.0)]))

    assert "| TTFT overhead p50 / p95 ms | 100 / 200 |" in markdown
    assert "| sqlite.sessions.write_wait p50 / p99 / max ms | 0.50 / 30.0 / 120 |" in markdown
    assert "| sqlite | 4 | 12.0 | 20.0 | - | - | - |" in markdown
    assert "| read | 2 | 3.00 | 5.00 | 5.00 |" in markdown
    assert "Worst Event Loop stall: 250 ms" in markdown
    assert "inner\nouter" in markdown
    assert "\n\n\n" not in markdown
    assert "- File `runs`: `level-01/runs.json`" in markdown


def test_compare_reports_deltas_for_shared_levels():
    old = _result([_level(10, ttft_overhead_p50=200.0, lag_p99=10.0)], commit="old")
    new = _result(
        [_level(10, ttft_overhead_p50=300.0, lag_p99=5.0), _level(20, ttft_overhead_p50=1.0)],
        commit="new",
    )

    text = compare_results(old, new)

    assert "(old) ->" in text and "(new)" in text
    assert "== 10 agent(s) ==" in text
    assert "== 20 agent(s) ==" not in text
    overhead = next(
        line for line in text.splitlines() if line.startswith("client.ttft_overhead_ms.p50")
    )
    assert overhead.split() == [
        "client.ttft_overhead_ms.p50",
        "200",
        "300",
        "+100",
        "+50.0%",
        "(worse)",
    ]
    lag = next(
        line for line in text.splitlines() if line.startswith("server.event_loop_lag_ms.p99")
    )
    assert "-50.0%" in lag and "worse" not in lag


def test_compare_without_shared_levels_says_so():
    text = compare_results(_result([_level(1, ttft_overhead_p50=1.0)]), _result([]))

    assert "No concurrency level appears in both results." in text


def test_written_result_loads_back(tmp_path):
    result = _result([_level(1, ttft_overhead_p50=100.0)])

    write_result(tmp_path, result)

    assert load_result(tmp_path / "result.json") == json.loads(json.dumps(result))
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# vBot load report")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.md", "result.json"]


def test_loading_a_foreign_json_file_fails(tmp_path):
    path = tmp_path / "other.json"
    path.write_text('{"kind": "something-else"}', encoding="utf-8")

    with pytest.raises(ValueError, match="not a perf_load result"):
        load_result(path)
