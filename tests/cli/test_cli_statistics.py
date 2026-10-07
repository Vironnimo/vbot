"""Tests for the ``vbot statistics`` commands: the report request and each printed section."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

ALL_TIME: dict[str, Any] = {"since": None, "until": None, "timezone": "Europe/Berlin"}

_TOTAL_COUNTS = [
    "calls",
    "failed_calls",
    "input_tokens",
    "estimated_input_tokens",
    "output_tokens",
    "estimated_output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "unreported_calls",
    "reported_calls",
    "estimated_calls",
    "unpriced_calls",
    "retrospective_calls",
]
_TOTAL_AMOUNTS = ("cost_usd", "reported_cost_usd", "estimated_cost_usd", "uncached_cost_usd")


def _totals(**values: Any) -> dict[str, Any]:
    """A report ``Totals`` object: zero counts and unknown amounts unless given."""
    return {
        **dict.fromkeys(_TOTAL_COUNTS, 0),
        **dict.fromkeys(_TOTAL_AMOUNTS),
        **values,
    }


def _run_row(run_id: str, **values: Any) -> dict[str, Any]:
    return {
        "agent_id": "main",
        "session_id": "ses_a",
        "session_title": "Parser refactor",
        "run_id": run_id,
        "origin": "user",
        "status": "completed",
        "started_at": "2026-06-01T09:00:00.000000Z",
        "duration_ms": 95000,
        "cost_usd": 0.073,
        "input_tokens": 1000,
        "output_tokens": 100,
        "calls": 2,
        "model_steps": 2,
        "tool_calls": 3,
        "iterations": 2,
        "primary_model": "anthropic/claude-sonnet-4",
        "models": ["anthropic/claude-sonnet-4"],
        **values,
    }


def _skill(name: str, origins: list[str], **usage: Any) -> dict[str, Any]:
    return {
        "name": name,
        "origins": origins,
        "offered_sessions": 0,
        "activated_sessions": 0,
        "activated_offered_sessions": 0,
        "usage_rate": None,
        "first_offered": None,
        "last_offered": None,
        "first_activated": None,
        "last_activated": None,
        "by_agent": [],
        **usage,
    }


SKILLS_SECTION: dict[str, Any] = {
    "total_skills": 3,
    "used_skills": 1,
    "never_used_skills": 2,
    "offered_unactivated_skills": 1,
    "skills_without_offer_data": 1,
    "skills": [
        _skill(
            "vbot-docs",
            ["bundled"],
            offered_sessions=10,
            activated_sessions=4,
            activated_offered_sessions=4,
            usage_rate=0.4,
            first_offered="2026-06-01T09:00:00+00:00",
            last_offered="2026-07-01T09:00:00+00:00",
            first_activated="2026-06-02T09:00:00+00:00",
            last_activated="2026-07-01T10:00:00+00:00",
            by_agent=[{"key": "assistant", "count": 4}],
        ),
        _skill(
            "glossary",
            ["global", "project:vBot"],
            offered_sessions=8,
            usage_rate=0.0,
            first_offered="2026-06-01T09:00:00+00:00",
            last_offered="2026-06-20T09:00:00+00:00",
        ),
        _skill("deep-research", ["global"]),
    ],
}


def test_statistics_skills_prints_the_skill_usage_report(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("statistics.report", {"window": ALL_TIME, "skills": SKILLS_SECTION})

    code, out, _err = run_cli("statistics", "skills")

    assert code == 0
    assert rpc.calls == [("statistics.report", {"sections": ["skills"]})]
    for text in (
        "total skills: 3",
        "activated skills: 1",
        "without offer conversion: 1",
        "without offer data: 1",
        "glossary [global, project:vBot]",
        "deep-research [global]",
        "vbot-docs [bundled]",
        "offered=10",
        "activated=4",
        "offer_conversion=0.40",
        "last_activated=2026-07-01T10:00:00+00:00",
    ):
        assert text in out


def test_statistics_skills_prints_explicit_empty_counts(rpc: FakeRpc, run_cli: RunCli) -> None:
    empty: dict[str, Any] = {key: 0 for key in SKILLS_SECTION if key != "skills"}
    empty["skills"] = []
    rpc.reply("statistics.report", {"window": ALL_TIME, "skills": empty})

    code, out, _err = run_cli("statistics", "skills")

    assert code == 0
    for text in (
        "total skills: 0",
        "activated skills: 0",
        "without offer conversion: 0",
        "without offer data: 0",
    ):
        assert text in out


@pytest.mark.parametrize(
    ("options", "params", "window_line"),
    [
        pytest.param((), {}, "window: all time", id="all-time"),
        pytest.param(
            ("--since", "2026-06-01"),
            {"since": "2026-06-01"},
            "window: since=2026-06-01T00:00:00.000000Z until=-",
            id="since-only",
        ),
        pytest.param(
            ("--since", "2026-06-01", "--until", "2026-07-01"),
            {"since": "2026-06-01", "until": "2026-07-01"},
            "window: since=2026-06-01T00:00:00.000000Z until=2026-07-01T00:00:00.000000Z",
            id="since-and-until",
        ),
    ],
)
def test_statistics_sends_only_the_given_window_bounds_and_echoes_the_window(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, str],
    window_line: str,
) -> None:
    window = {bound: f"{value}T00:00:00.000000Z" for bound, value in params.items()}
    rpc.reply("statistics.report", {"window": ALL_TIME | window, "skills": SKILLS_SECTION})

    code, out, _err = run_cli("statistics", "skills", *options)

    assert code == 0
    assert rpc.calls == [("statistics.report", {"sections": ["skills"], **params})]
    assert out.splitlines()[:2] == ["skills:", window_line]


def test_statistics_overview_prints_cost_runs_leaders_and_insights(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    overview = {
        "totals": _totals(
            calls=8,
            input_tokens=173000,
            output_tokens=10500,
            cache_read_tokens=91000,
            cost_usd=0.6714,
            reported_cost_usd=0.623,
            reported_calls=5,
            estimated_cost_usd=0.0484,
            estimated_calls=3,
        ),
        "previous": _totals(calls=3, cost_usd=0.2),
        "runs": {
            "total": 7,
            "completed": 4,
            "failed": 1,
            "cancelled": 1,
            "interrupted": 1,
            "running": 0,
        },
        "previous_runs": {"total": 2},
        "user_runs": {
            "count": 6,
            "duration_p50_ms": 180000,
            "duration_p90_ms": 600000,
            "cost_p50_usd": 0.073,
            "cost_p90_usd": 0.25,
            "first_visible_p50_ms": None,
        },
        "active_agents": 3,
        "active_sessions": 4,
        "series": [],
        "by_origin": [
            {
                "origin": "user",
                "calls": 7,
                "runs": 6,
                "input_tokens": 170000,
                "output_tokens": 10300,
                "cost_usd": 0.6703,
            }
        ],
        "top_agents": [],
        "top_models": [
            {
                "model": "anthropic/claude-sonnet-4",
                "calls": 5,
                "input_tokens": 145000,
                "output_tokens": 8700,
                "cost_usd": None,
                "cache_read_tokens": 91000,
            }
        ],
        "insights": [
            {
                "id": "cancelled_cost",
                "severity": "warn",
                "values": {"share": 0.1787, "cost_usd": 0.12, "runs": 1},
            },
            {"id": "future_signal", "severity": "info", "values": {"b": 2, "a": "x"}},
        ],
    }
    rpc.reply("statistics.report", {"window": ALL_TIME, "overview": overview})

    code, out, _err = run_cli("statistics", "overview", "--since", "2026-06-01")

    assert code == 0
    assert rpc.calls == [("statistics.report", {"sections": ["overview"], "since": "2026-06-01"})]
    for text in (
        "cost: $0.6714 (reported=$0.6230 over 5 calls, estimated=$0.0484 over 3 calls, "
        "unpriced calls=0)",
        "cache: hit_rate=52.6% (read=91000 of input=173000) write=0",
        "runs: total=7 completed=4 failed=1 cancelled=1 interrupted=1 running=0",
        "user runs: count=6 duration_p50=3m00s duration_p90=10m00s cost_p50=$0.0730 "
        "cost_p90=$0.2500 first_visible_p50=-",
        "active: agents=3 sessions=4",
        "previous window of equal length: cost=$0.2000 calls=3 runs=2",
        "  user: runs=6 calls=7 input=170000 output=10300 cost=$0.6703",
        "top agents by cost:\n  no agent activity recorded",
        # An amount without any priced request is unknown, never $0.
        "  anthropic/claude-sonnet-4: calls=5 input=145000 output=8700 cache_hit=62.8% "
        "cost=unknown",
        "  [warn] cancelled_cost: cancelled Runs (1) cost $0.1200, 17.9% of cost",
        # An insight the CLI does not know still prints all of its values.
        "  [info] future_signal: a=x b=2",
    ):
        assert text in out


def test_statistics_usage_prints_totals_breakdowns_and_costliest_work(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    usage = {
        "totals": _totals(calls=2, cost_usd=0.5, reported_cost_usd=0.5, reported_calls=2),
        "breakdowns": {
            "model": [
                {"key": "openai/gpt-5", "runs": 1, "sessions": 1, **_totals(calls=2, cost_usd=0.5)}
            ],
            "project": [{"key": "", "runs": 1, "sessions": 1, **_totals(calls=2)}],
        },
        "series": [],
        "top_runs": [_run_row("run_1", cost_usd=0.5)],
        "top_sessions": [],
        "recent_calls": [],
    }
    rpc.reply("statistics.report", {"window": ALL_TIME, "usage": usage})

    code, out, _err = run_cli("statistics", "usage")

    assert code == 0
    assert rpc.calls == [("statistics.report", {"sections": ["usage"]})]
    for text in (
        "model calls: 2 (failed=0, without usage=0)",
        "tokens: input=0 output=0 reasoning=0 (of which estimated: input=0 output=0)",
        "  openai/gpt-5: calls=2 failed=0 runs=1 sessions=1 input=0 output=0 cache_hit=- "
        "cost=$0.5000",
        "by project:\n  (no project): calls=2",
        "by provider:\n  no model calls recorded",
        "costliest runs:\n  main ses_a run_1: origin=user status=completed",
        "costliest sessions:\n  no sessions recorded",
    ):
        assert text in out


def test_statistics_runs_prints_outcomes_percentiles_and_notable_runs(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    group = {
        "runs": 6,
        "completed": 3,
        "failed": 1,
        "cancelled": 1,
        "interrupted": 1,
        "duration_p50_ms": 180000,
        "duration_p90_ms": 600000,
        "cost_usd": 0.6703,
        "cost_p50_usd": 0.073,
        "avg_tool_calls": 1.5,
        "avg_model_steps": 1.1666,
    }
    runs = {
        "totals": {
            "total": 6,
            "completed": 3,
            "failed": 1,
            "cancelled": 1,
            "interrupted": 1,
            "running": 0,
        },
        "by_origin": [{"origin": "user", **group, "cost_p90_usd": 0.25}],
        "duration_buckets": [],
        "agents": [
            {
                "agent_id": "main",
                **group,
                "tool_ms": 36160,
                "changed_files": 2,
                "lines_added": 10,
                "lines_removed": 3,
            }
        ],
        "daily": [],
        "longest": [
            _run_row(
                "run_5",
                status="interrupted",
                duration_ms=None,
                cost_usd=None,
                primary_model=None,
            )
        ],
        "costliest": [],
        "most_steps": [],
        "cancelled": {"runs": 1, "cost_usd": 0.12, "wait_p50_ms": 300000},
        "errors": {"total": 1, "failed_attempts": 4},
        "previous": {
            "totals": {"total": 2, "completed": 2},
            "user": {"duration_p50_ms": 90000, "duration_p90_ms": None},
        },
    }
    rpc.reply("statistics.report", {"window": ALL_TIME, "runs": runs})

    code, out, _err = run_cli("statistics", "runs")

    assert code == 0
    assert rpc.calls == [("statistics.report", {"sections": ["runs"]})]
    for text in (
        "runs: total=6 completed=3 failed=1 cancelled=1 interrupted=1 running=0",
        "previous window of equal length: total=2 completed=2 failed=0 cancelled=0 "
        "interrupted=0 running=0 user_duration_p50=1m30s user_duration_p90=-",
        "cancelled: runs=1 cost=$0.1200 wait_p50=5m00s",
        "errors: 1 (failed model requests=4; details: vbot statistics errors)",
        "  user: runs=6 completed=3 failed=1 cancelled=1 interrupted=1 duration_p50=3m00s "
        "duration_p90=10m00s cost=$0.6703 cost_p50=$0.0730 cost_p90=$0.2500 "
        "avg_tool_calls=1.50 avg_model_steps=1.17",
        "cost_p50=$0.0730 avg_tool_calls=1.50 avg_model_steps=1.17 tool_time=36.2s "
        "changed_files=2 lines=+10/-3",
        "  main ses_a run_5: origin=user status=interrupted started=2026-06-01T09:00:00.000000Z "
        "duration=- cost=unknown model_steps=2 tool_calls=3 model=-",
        "costliest runs:\n  no runs recorded",
    ):
        assert text in out


def test_statistics_errors_reads_the_runs_section(rpc: FakeRpc, run_cli: RunCli) -> None:
    errors = {
        "total": 2,
        "failed_attempts": 5,
        "by_kind": [{"key": "rate_limit", "count": 2}],
        "by_provider": [{"key": "openai", "count": 2}],
        "by_model": [],
        "by_agent": [{"key": "main", "count": 2}],
        "daily": [],
        "by_hour": [{"hour": hour, "count": 2 if hour == 9 else 0} for hour in range(24)],
    }
    rpc.reply("statistics.report", {"window": ALL_TIME, "runs": {"errors": errors}})

    code, out, _err = run_cli("statistics", "errors")

    assert code == 0
    assert rpc.calls == [("statistics.report", {"sections": ["runs"]})]
    for text in (
        "total errors: 2",
        "failed model requests: 5",
        "by kind:\n  rate_limit: 2",
        "by model:\n  no errors recorded",
        "by local hour (Europe/Berlin):\n  09: 2\n",
    ):
        assert text in out + "\n"


def test_statistics_tools_prints_outcomes_latency_and_rejection_codes(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    tools = {
        "totals": {
            "calls": 4,
            "accepted": 3,
            "rejected": 1,
            "unknown": 0,
            "tool_ms": 36000,
            "tools": 1,
        },
        "tools": [
            {
                "name": "bash",
                "calls": 4,
                "accepted": 3,
                "rejected": 1,
                "rejection_rate": 0.25,
                "p50_ms": 1877,
                "p95_ms": 30000,
                "max_ms": 30000,
                "total_ms": 36000,
                "time_share": 1.0,
                "top_codes": [{"code": "timeout", "count": 1}],
            }
        ],
        "rejection_codes": [{"code": "timeout", "count": 1, "tools": ["bash"]}],
        "by_agent": [{"agent_id": "main", "calls": 4, "rejected": 1, "tool_ms": 36000}],
    }
    rpc.reply("statistics.report", {"window": ALL_TIME, "tools": tools})

    code, out, _err = run_cli("statistics", "tools")

    assert code == 0
    for text in (
        "calls: 4 (accepted=3 rejected=1 unknown=0) tools=1 tool_time=36.0s",
        "  bash: calls=4 rejected=1 (25.0%) p50=1.9s p95=30.0s max=30.0s total=36.0s "
        "time_share=100.0% top_rejections=timeout:1",
        "  timeout: 1 (tools: bash)",
        "  main: calls=4 rejected=1 tool_time=36.0s",
    ):
        assert text in out


def test_statistics_compactions_reads_the_diagnostics_section(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    compactions = {
        "total_compactions": 5,
        "sessions_with_compactions": 2,
        "average_per_compacted_session": 2.5,
        "p50_per_compacted_session": 2,
        "p95_per_compacted_session": 3,
        "max_per_session": 3,
        "by_strategy": [
            {"strategy": "summary_tail", "compactions": 4},
            {"strategy": "continuation", "compactions": 1},
        ],
        "reclaim": {
            "observations": 4,
            "total_tokens": 220000,
            "average_tokens": 55000,
            "p50_tokens": 50000,
            "p95_tokens": 70000,
        },
        "top_sessions": [
            {
                "agent_id": "main",
                "session_id": "session-1",
                "compactions": 3,
                "estimated_reclaimed_tokens": 150000,
                "last_compaction": "2026-06-13T08:45:00+00:00",
            }
        ],
    }
    rpc.reply(
        "statistics.report", {"window": ALL_TIME, "diagnostics": {"compactions": compactions}}
    )

    code, out, _err = run_cli("statistics", "compactions")

    assert code == 0
    assert rpc.calls == [("statistics.report", {"sections": ["diagnostics"]})]
    for text in (
        "total compactions: 5",
        "average=2.50 p50=2.00 p95=3.00 max=3",
        "total=220000",
        "summary_tail: 4",
        "main session-1: compactions=3",
    ):
        assert text in out


def test_statistics_reports_a_rejected_window(rpc: FakeRpc, run_cli: RunCli) -> None:
    message = "params.since must be an ISO 8601 timestamp string"
    rpc.fail("statistics.report", "invalid_request", message)

    code, out, err = run_cli("statistics", "skills", "--since", "not-a-date")

    assert code == 1
    assert f"invalid_request: {message}" in out
    assert "rpc_method: statistics.report" in err


@pytest.mark.parametrize(
    ("section", "report"),
    [
        pytest.param("skills", {}, id="section"),
        pytest.param("errors", {"runs": {"totals": {}}}, id="nested-section"),
    ],
)
def test_statistics_fails_when_the_report_lacks_the_requested_section(
    rpc: FakeRpc, run_cli: RunCli, section: str, report: dict[str, Any]
) -> None:
    # A report without the requested section is a broken contract, not an empty state:
    # the CLI must fail explicitly rather than print nothing.
    rpc.reply("statistics.report", {"window": ALL_TIME, **report})

    code, out, _err = run_cli("statistics", section)

    assert code == 1
    assert "missing" in out
