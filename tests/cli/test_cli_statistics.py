"""Tests for the ``vbot statistics`` commands: the report request and each printed section."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

ALL_TIME: dict[str, Any] = {"since": None, "until": None}


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
            "vbot-cli",
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
    assert rpc.calls == [("statistics.report", {})]
    for text in (
        "total skills: 3",
        "activated skills: 1",
        "without offer conversion: 1",
        "without offer data: 1",
        "glossary [global, project:vBot]",
        "deep-research [global]",
        "vbot-cli [bundled]",
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
            "window: since=2026-06-01T00:00:00+00:00 until=-",
            id="since-only",
        ),
        pytest.param(
            ("--since", "2026-06-01", "--until", "2026-07-01"),
            {"since": "2026-06-01", "until": "2026-07-01"},
            "window: since=2026-06-01T00:00:00+00:00 until=2026-07-01T00:00:00+00:00",
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
    window = {bound: f"{value}T00:00:00+00:00" for bound, value in params.items()}
    rpc.reply("statistics.report", {"window": ALL_TIME | window, "skills": SKILLS_SECTION})

    code, out, _err = run_cli("statistics", "skills", *options)

    assert code == 0
    assert rpc.calls == [("statistics.report", params)]
    assert out.splitlines()[:2] == ["skills:", window_line]


def test_statistics_overview_prints_totals_roles_run_status_and_agents(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    overview = {
        "total_agents": 2,
        "total_sessions": 5,
        "total_runs": 12,
        "open_run_groups": 1,
        "total_chat_messages": 85,
        "chat_messages_by_role": {"user": 40, "assistant": 45},
        "total_session_records": 200,
        "session_records_by_role": {"user": 40, "assistant": 45, "note": 55, "run_summary": 60},
        "last_activity": "2026-07-01T10:00:00+00:00",
        "run_status": {"completed": 10, "failed": 1, "cancelled": 0, "interrupted": 1},
        "average_run_duration_ms": 1234.5,
        "median_run_duration_ms": 900.0,
        "runs_with_tool_calls": 8,
        "total_tool_calls": 30,
        "agents": [
            {
                "agent_id": "assistant",
                "sessions": 5,
                "runs": 12,
                "chat_messages": 85,
                "session_records": 200,
                "errors": 2,
                "last_activity": "2026-07-01T10:00:00+00:00",
            }
        ],
        "daily_trend": [],
    }
    rpc.reply("statistics.report", {"window": ALL_TIME, "overview": overview})

    code, out, _err = run_cli("statistics", "overview")

    assert code == 0
    assert rpc.calls == [("statistics.report", {})]
    for text in (
        "overview:",
        "agents: 2",
        "visible chat messages: 85",
        "stored session records: 200",
        "visible chat messages by role:\n  user: 40\n  assistant: 45",
        "stored session records by role:",
        "  note: 55",
        "  run_summary: 60",
        "run status: completed=10 failed=1 cancelled=0 interrupted=1",
        "  assistant: sessions=5 runs=12 chat_messages=85 session_records=200 errors=2 "
        "last_activity=2026-07-01T10:00:00+00:00",
    ):
        assert text in out


def test_statistics_runs_prints_rates_agent_messages_and_model_steps(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    runs = {
        "total_runs": 4,
        "open_run_groups": 1,
        "status": {"completed": 2, "failed": 1, "cancelled": 0, "interrupted": 1},
        "cancel_rate": 0.0,
        "failure_rate": 0.25,
        "interruption_rate": 0.25,
        "duration": {
            "count": 4,
            "average_ms": 1000.0,
            "p50_ms": 900.0,
            "p90_ms": 1500.0,
            "p95_ms": 1600.0,
        },
        "runs_with_tool_calls": 3,
        "total_tool_calls": 9,
        "average_tool_calls_per_run": 2.25,
        "agent_messages": 6,
        "model_steps": 14,
        "average_agent_messages_per_run": 1.5,
        "average_model_steps_per_run": 3.5,
        "derived_fallback_runs": 1,
        "runs_per_agent": [],
        "top_sessions_by_runs": [],
        "longest_runs": [],
        "daily": [],
    }
    rpc.reply("statistics.report", {"window": ALL_TIME, "runs": runs})

    code, out, _err = run_cli("statistics", "runs")

    assert code == 0
    for text in (
        "status: completed=2 failed=1 cancelled=0 interrupted=1",
        "interruption rate: 0.25",
        "agent messages: 6",
        "model steps: 14",
        "average agent messages per run: 1.50",
        "average model steps per run: 3.50",
    ):
        assert text in out


def test_statistics_compactions_prints_checkpoint_activity(rpc: FakeRpc, run_cli: RunCli) -> None:
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
    rpc.reply("statistics.report", {"window": ALL_TIME, "compactions": compactions})

    code, out, _err = run_cli("statistics", "compactions")

    assert code == 0
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


def test_statistics_fails_when_the_report_lacks_the_requested_section(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    # A report without the requested section is a broken contract, not an empty state:
    # the CLI must fail explicitly rather than print nothing.
    rpc.reply("statistics.report", {"window": ALL_TIME})

    code, out, _err = run_cli("statistics", "skills")

    assert code == 1
    assert out.strip()
