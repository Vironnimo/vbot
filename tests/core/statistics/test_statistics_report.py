"""The Statistics report and Run activity: overview, Runs, Tools, errors, Compactions, scopes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.sessions import ChatSessionManager, SessionAddress
from core.statistics import CountEntry, StatisticsReport
from core.tools import tool_failure, tool_success
from tests.core.sessions.history_fixtures import seed_history
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _assistant,
    _compaction,
    _run_summary,
    _tool,
    _write_session,
)

SONNET = "openrouter/anthropic/claude-sonnet-4"


def test_agents_without_sessions_yield_a_zeroed_serializable_report(
    statistics: StatisticsFactory,
) -> None:
    report = statistics(["main"]).report()

    assert isinstance(report, StatisticsReport)
    assert report.overview.total_agents == 1
    assert [(agent.agent_id, agent.sessions) for agent in report.overview.agents] == [("main", 0)]
    assert report.overview.total_sessions == 0
    assert report.overview.total_runs == 0
    assert report.overview.last_activity is None
    assert report.overview.chat_messages_by_role["assistant"] == 0
    assert report.overview.session_records_by_role["agent_takeover"] == 0
    assert report.usage.providers == []
    assert report.runs.duration.p95_ms is None
    assert report.compactions.total_compactions == 0
    assert report.compactions.sessions_with_compactions == 0
    assert report.compactions.average_per_compacted_session is None
    assert report.compactions.max_per_session == 0
    assert report.compactions.reclaim.observations == 0
    assert report.compactions.top_sessions == []
    assert report.errors.total_errors == 0
    assert report.tools.tools == []
    assert json.loads(json.dumps(report.to_dict()))["overview"]["total_runs"] == 0


def test_chat_messages_and_session_records_are_separate(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _write_session(
        manager,
        "main",
        [
            ChatMessage.user("hi", timestamp=BASE),
            _assistant(model=SONNET, at=BASE + timedelta(seconds=1)),
            ChatMessage.note("background", timestamp=BASE + timedelta(seconds=2)),
            _run_summary(
                status="completed",
                at=BASE + timedelta(seconds=3),
                duration_ms=1500,
                run_id="r1",
            ),
        ],
    )

    report = statistics().report()

    assert report.overview.chat_messages_by_role == {"user": 1, "assistant": 1}
    assert report.overview.total_chat_messages == 2
    assert report.overview.session_records_by_role["user"] == 1
    assert report.overview.session_records_by_role["assistant"] == 1
    assert report.overview.session_records_by_role["note"] == 1
    assert report.overview.session_records_by_role["run_summary"] == 1
    assert report.overview.session_records_by_role["agent_takeover"] == 0
    assert report.overview.total_session_records == 4
    assert report.overview.total_sessions == 1
    assert report.overview.last_activity is not None
    assert report.overview.agents[0].runs == 1
    assert report.overview.agents[0].chat_messages == 2
    assert report.overview.agents[0].session_records == 4


def test_chat_messages_exclude_thinking_and_tool_only_model_steps(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    tool_call = ToolCall(id="call-read", name="read", arguments={"path": "README.md"})
    _write_session(
        manager,
        "main",
        [
            ChatMessage.user("inspect", timestamp=BASE),
            _assistant(
                model=SONNET,
                at=BASE + timedelta(seconds=1),
                content=None,
                reasoning="I should inspect the file.",
                tool_calls=[tool_call],
            ),
            _assistant(
                model=SONNET, at=BASE + timedelta(seconds=2), content=None, tool_calls=[tool_call]
            ),
            _assistant(model=SONNET, at=BASE + timedelta(seconds=3), content="   "),
            _assistant(
                model=SONNET,
                at=BASE + timedelta(seconds=4),
                content="I found the cause; checking the fix.",
                reasoning="Summarize the finding.",
                tool_calls=[tool_call],
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=5), duration_ms=5000, run_id="r1"
            ),
        ],
    )

    report = statistics().report()

    assert report.overview.chat_messages_by_role == {"user": 1, "assistant": 1}
    assert report.overview.total_chat_messages == 2
    assert report.overview.session_records_by_role["assistant"] == 4
    assert report.overview.agents[0].chat_messages == 2
    assert report.usage.totals.assistant_messages == 4
    assert report.runs.agent_messages == 1
    assert report.runs.model_steps == 4
    assert report.runs.average_agent_messages_per_run == 1.0
    assert report.runs.average_model_steps_per_run == 4.0


def test_fork_counts_only_its_own_activity_never_inherited_history(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    source = manager.create("main")
    source_messages = [
        ChatMessage.user("source work", timestamp=BASE),
        _assistant(
            model=SONNET,
            at=BASE + timedelta(seconds=1),
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
        _tool(
            name="edit",
            at=BASE + timedelta(seconds=2),
            envelope=tool_success({"changed": True}),
            duration_ms=4,
        ),
        ChatMessage.error("source_error", "source failure", timestamp=BASE + timedelta(seconds=3)),
        _run_summary(
            status="completed",
            at=BASE + timedelta(seconds=4),
            duration_ms=1000,
            run_id="source-run",
        ),
    ]
    seed_history(source, source_messages)

    fork = asyncio.run(
        manager.fork(SessionAddress(project_id=None, agent_id="main", session_id=source.id))
    )
    # The fork inherits the source's history into its current view without
    # writing any of it into its own audit.
    assert fork.load() == []
    assert fork.load_active() == source.load_active()
    fork_messages = [
        ChatMessage.user("fork work", timestamp=BASE + timedelta(minutes=1)),
        _assistant(
            model=SONNET,
            at=BASE + timedelta(minutes=1, seconds=1),
            usage={"input_tokens": 50, "output_tokens": 10},
        ),
        _tool(
            name="edit",
            at=BASE + timedelta(minutes=1, seconds=2),
            envelope=tool_failure("ambiguous_match", "choose one"),
            duration_ms=2,
        ),
        ChatMessage.error(
            "fork_error", "fork failure", timestamp=BASE + timedelta(minutes=1, seconds=3)
        ),
        _run_summary(
            status="failed",
            at=BASE + timedelta(minutes=1, seconds=4),
            duration_ms=500,
            run_id="fork-run",
        ),
    ]
    seed_history(fork, fork_messages)
    assert len(fork.load_active()) == len(source_messages) + len(fork_messages)

    report = statistics().report()

    assert report.overview.total_sessions == 2
    assert {(row.session_id, row.runs) for row in report.runs.top_sessions_by_runs} == {
        (source.id, 1),
        (fork.id, 1),
    }
    assert report.overview.total_session_records == len(source_messages) + len(fork_messages)
    assert report.overview.total_chat_messages == 4
    assert report.runs.total_runs == 2
    assert report.runs.status.completed == 1
    assert report.runs.status.failed == 1
    assert report.usage.totals.measured_input_tokens == 150
    assert report.usage.totals.measured_output_tokens == 30
    assert report.errors.total_errors == 2
    assert report.tools.total_calls == 2
    edit = next(tool for tool in report.tools.tools if tool.name == "edit")
    assert edit.successes == 1
    assert edit.failures == 1


def test_runs_are_segmented_by_summaries_with_status_rates_and_open_groups(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _write_session(
        manager,
        "main",
        [
            # Run 1 — completed, used a Tool.
            _assistant(model=SONNET, at=BASE),
            _tool(
                name="read",
                at=BASE + timedelta(seconds=1),
                envelope=tool_success({"text": "x"}),
                duration_ms=40,
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=2), duration_ms=2000, run_id="r1"
            ),
            # Run 2 — failed, no Tools.
            _assistant(model=SONNET, at=BASE + timedelta(seconds=3)),
            _run_summary(
                status="failed", at=BASE + timedelta(seconds=4), duration_ms=500, run_id="r2"
            ),
            # Run 3 — interrupted.
            _assistant(model=SONNET, at=BASE + timedelta(seconds=5)),
            _run_summary(
                status="interrupted", at=BASE + timedelta(seconds=6), duration_ms=250, run_id="r3"
            ),
            # A trailing turn without a Run summary is an open Run group.
            _assistant(model=SONNET, at=BASE + timedelta(seconds=7)),
        ],
    )

    report = statistics().report()
    runs = report.runs

    assert runs.total_runs == 3
    assert (runs.status.completed, runs.status.failed, runs.status.interrupted) == (1, 1, 1)
    assert runs.runs_with_tool_calls == 1
    assert runs.total_tool_calls == 1
    assert runs.agent_messages == 3
    assert runs.model_steps == 3
    assert runs.average_agent_messages_per_run == 1.0
    assert runs.average_model_steps_per_run == 1.0
    assert runs.failure_rate == pytest.approx(1 / 3)
    assert runs.interruption_rate == pytest.approx(1 / 3)
    overview = report.overview
    assert (overview.run_status.completed, overview.run_status.interrupted) == (1, 1)
    assert overview.daily_trend[0].interrupted == 1
    assert overview.open_run_groups == 1


def test_derived_fallback_detects_mid_run_model_switch(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(model=SONNET, at=BASE),
            _assistant(model="openai/gpt-5", at=BASE + timedelta(seconds=1)),
            _assistant(model="openai/gpt-4.1", at=BASE + timedelta(milliseconds=1500)),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=2), duration_ms=1000, run_id="r1"
            ),
            # Single-model run — no fallback.
            _assistant(model="openai/gpt-5", at=BASE + timedelta(seconds=3)),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=4), duration_ms=1000, run_id="r2"
            ),
        ],
    )

    report = statistics().report()

    assert report.runs.derived_fallback_runs == 1
    assert report.runs.total_runs == 2
    assert {provider.provider: provider.runs for provider in report.usage.providers} == {
        "openrouter": 1,
        "openai": 2,
    }
    assert {model.model: model.runs for model in report.usage.models} == {
        SONNET: 1,
        "openai/gpt-5": 2,
        "openai/gpt-4.1": 1,
    }


def test_tool_success_failure_envelopes_and_p95(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    # Ten read calls: nine fast successes, one slow failure with an error code.
    messages = [
        _tool(
            name="read",
            at=BASE + timedelta(seconds=index),
            envelope=tool_success({"text": "x"}),
            duration_ms=10,
        )
        for index in range(9)
    ]
    messages.append(
        _tool(
            name="read",
            at=BASE + timedelta(seconds=9),
            envelope=tool_failure("not_found", "missing"),
            duration_ms=1000,
        )
    )
    _write_session(manager, "main", messages)

    report = statistics().report()
    read = next(tool for tool in report.tools.tools if tool.name == "read")

    assert read.calls == 10
    assert read.successes == 9
    assert read.failures == 1
    assert read.success_rate == pytest.approx(0.9)
    assert read.top_error_code == "not_found"
    assert read.error_codes == [CountEntry(key="not_found", count=1)]
    # Nearest-rank P95 of ten samples is the tenth (the 1000 ms outlier).
    assert read.p95_duration_ms == 1000.0
    assert report.tools.total_calls == 10


def test_errors_are_grouped_by_kind_provider_model_agent_and_hour(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(model=SONNET, at=BASE),
            ChatMessage.error("rate_limit", "slow down", timestamp=BASE + timedelta(seconds=1)),
            ChatMessage.error("timeout", "too slow", timestamp=BASE + timedelta(seconds=2)),
            _run_summary(
                status="failed", at=BASE + timedelta(seconds=3), duration_ms=100, run_id="r1"
            ),
        ],
    )
    # An error before any Model call has no Model to attribute it to.
    _write_session(manager, "main", [ChatMessage.error("config_error", "bad", timestamp=BASE)])

    report = statistics().report()
    errors = report.errors

    assert errors.total_errors == 3
    assert {entry.key: entry.count for entry in errors.by_kind} == {
        "rate_limit": 1,
        "timeout": 1,
        "config_error": 1,
    }
    assert {entry.key: entry.count for entry in errors.by_provider} == {
        "openrouter": 2,
        "unknown": 1,
    }
    assert {entry.key: entry.count for entry in errors.by_model} == {SONNET: 2, "unknown": 1}
    assert {entry.key: entry.count for entry in errors.by_agent} == {"main": 3}
    assert errors.by_hour[12].count == 3
    assert report.usage.models[0].errors == 2


def test_percentiles_over_known_run_durations(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _write_session(
        manager,
        "main",
        [
            _run_summary(
                status="completed",
                at=BASE + timedelta(minutes=index),
                duration_ms=(index + 1) * 100,
                run_id=f"r{index}",
            )
            for index in range(10)
        ],
    )

    report = statistics().report()
    duration_stats = report.runs.duration

    assert duration_stats.count == 10
    assert duration_stats.average_ms == pytest.approx(550.0)
    assert duration_stats.p50_ms == 500.0
    assert duration_stats.p90_ms == 900.0
    assert duration_stats.p95_ms == 1000.0
    assert report.overview.median_run_duration_ms == 500.0


def test_since_until_windowing_filters_by_message_timestamp(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    day_one = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    day_two = datetime(2026, 6, 5, 9, 0, tzinfo=UTC)
    _write_session(
        manager,
        "main",
        [
            _assistant(model=SONNET, at=day_one, usage={"input_tokens": 10, "output_tokens": 1}),
            _run_summary(
                status="completed", at=day_one + timedelta(seconds=1), duration_ms=111, run_id="r1"
            ),
            _assistant(model=SONNET, at=day_two, usage={"input_tokens": 50, "output_tokens": 5}),
            _run_summary(
                status="completed", at=day_two + timedelta(seconds=1), duration_ms=222, run_id="r2"
            ),
        ],
    )
    service = statistics()

    assert service.report().runs.total_runs == 2

    windowed = service.report(
        since=datetime(2026, 6, 4, 0, 0, tzinfo=UTC),
        until=datetime(2026, 6, 6, 0, 0, tzinfo=UTC),
    )
    assert windowed.runs.total_runs == 1
    assert windowed.usage.totals.measured_input_tokens == 50
    assert windowed.window.since == "2026-06-04T00:00:00+00:00"
    # The daily series only holds in-window days.
    assert [point.date for point in windowed.usage.daily] == ["2026-06-05"]


def test_multiple_agents_and_daily_trend(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    model = "openai/gpt-5"
    _write_session(
        manager,
        "main",
        [
            _assistant(model=model, at=BASE),
            ChatMessage.error("network_error", "boom", timestamp=BASE + timedelta(seconds=1)),
            _run_summary(
                status="failed", at=BASE + timedelta(seconds=2), duration_ms=300, run_id="r1"
            ),
        ],
    )
    _write_session(
        manager,
        "research",
        [
            _assistant(model=model, at=BASE + timedelta(days=1)),
            _run_summary(
                status="completed",
                at=BASE + timedelta(days=1, seconds=1),
                duration_ms=700,
                run_id="r2",
            ),
        ],
    )

    report = statistics(["main", "research"]).report()

    assert report.overview.total_agents == 2
    assert {entry.agent_id for entry in report.runs.runs_per_agent} == {"main", "research"}
    trend = {
        point.date: (point.runs, point.completed, point.failed, point.cancelled)
        for point in report.overview.daily_trend
    }
    assert trend["2026-06-01"] == (1, 0, 1, 0)
    assert trend["2026-06-02"] == (1, 1, 0, 0)


def test_compactions_report_distribution_reclaim_strategy_window_and_forks(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    day_one = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    day_two = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    day_three = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)

    source = manager.create("main")
    source.append(_compaction(at=day_one, before=100_000, after=70_000))
    source.append(_compaction(at=day_two, before=90_000, after=30_000))

    fork = asyncio.run(
        manager.fork(SessionAddress(project_id=None, agent_id="main", session_id=source.id))
    )
    fork.append(_compaction(at=day_three, before=95_000, after=25_000))

    second_session_id = _write_session(
        manager,
        "main",
        [
            _compaction(
                at=day_two + timedelta(hours=1),
                before=80_000,
                after=40_000,
                strategy="continuation",
            ),
            _compaction(at=day_three + timedelta(hours=1), before=75_000, after=25_000),
        ],
    )

    report = statistics().report(since=day_two, until=day_three + timedelta(hours=2))
    compactions = report.compactions

    # The fork's inherited source checkpoints are history, not the fork's own activity.
    assert compactions.total_compactions == 4
    assert compactions.sessions_with_compactions == 3
    assert compactions.average_per_compacted_session == pytest.approx(4 / 3)
    assert compactions.p50_per_compacted_session == 1.0
    assert compactions.p95_per_compacted_session == 2.0
    assert compactions.max_per_session == 2
    assert [(row.strategy, row.compactions) for row in compactions.by_strategy] == [
        ("summary_tail", 3),
        ("continuation", 1),
    ]
    assert compactions.reclaim.observations == 4
    assert compactions.reclaim.total_tokens == 220_000
    assert compactions.reclaim.average_tokens == 55_000
    assert compactions.reclaim.p50_tokens == 50_000
    assert compactions.reclaim.p95_tokens == 70_000
    assert compactions.top_sessions[0].session_id == second_session_id
    assert compactions.top_sessions[0].compactions == 2
    assert compactions.top_sessions[0].estimated_reclaimed_tokens == 90_000
    assert {row.session_id for row in compactions.top_sessions[1:]} == {source.id, fork.id}


# -- Run activity -------------------------------------------------------------


def test_run_activity_returns_overlapping_runs_with_local_usage(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    checkpoint = _compaction(at=BASE + timedelta(minutes=3), before=2000, after=400)
    checkpoint = replace(
        checkpoint,
        usage={
            **(checkpoint.usage or {}),
            "model_call": {
                "model": "summary/m",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 30,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            },
        },
    )
    session_id = _write_session(
        manager,
        "main",
        [
            _assistant(
                model="openai/gpt-5", at=BASE, usage={"input_tokens": 100, "output_tokens": 20}
            ),
            _assistant(
                model="openai/gpt-5",
                at=BASE + timedelta(minutes=1),
                usage={
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "input_tokens_estimated": True,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            ),
            _tool(
                name="read",
                at=BASE + timedelta(minutes=2),
                envelope=tool_success({"text": "ok"}),
                duration_ms=20,
            ),
            checkpoint,
            _run_summary(status="completed", at=BASE, duration_ms=10 * 60 * 1000, run_id="r1"),
            _assistant(
                model="openai/gpt-5",
                at=BASE + timedelta(hours=2),
                usage={"input_tokens": 200, "output_tokens": 30},
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(hours=2), duration_ms=100, run_id="r2"
            ),
        ],
    )

    report = statistics().run_activity(
        since=BASE + timedelta(minutes=5), until=BASE + timedelta(minutes=6)
    )

    assert report.total_runs == 1
    assert report.truncated is False
    run = report.runs[0]
    assert run.session_id == session_id
    assert run.run_id == "r1"
    assert run.models == ["openai/gpt-5", "summary/m"]
    assert run.tool_calls == 1
    assert run.measured_input_tokens == 1100
    assert run.measured_output_tokens == 20
    assert run.estimated_input_tokens == 10
    assert run.estimated_output_tokens == 33


def test_run_activity_counts_compaction_without_chat_steps(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    checkpoint = _compaction(at=BASE, before=1000, after=200)
    checkpoint = replace(
        checkpoint,
        usage={
            **(checkpoint.usage or {}),
            "model_call": {
                "model": "summary/m",
                "usage": {"input_tokens": 900, "output_tokens": 100},
            },
        },
    )
    _write_session(
        manager,
        "main",
        [
            checkpoint,
            _run_summary(status="completed", at=BASE, duration_ms=1000, run_id="compact"),
        ],
    )
    service = statistics()

    activity = service.run_activity(since=BASE, until=BASE + timedelta(seconds=1))
    [run] = activity.runs
    totals = service.report().usage.totals
    assert activity.total_runs == 1
    assert run.models == ["summary/m"]
    assert run.measured_input_tokens == totals.measured_input_tokens == 900
    assert run.measured_output_tokens == totals.measured_output_tokens == 100
    assert run.tool_calls == 0
    assert totals.assistant_messages == 0
    assert totals.model_calls == 1


@pytest.mark.parametrize(("runs", "truncated"), [(3, False), (4, True)])
def test_run_activity_returns_the_newest_bounded_runs_with_the_total(
    manager: ChatSessionManager,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
    runs: int,
    truncated: bool,
) -> None:
    monkeypatch.setattr("core.statistics.statistics.MAX_RUN_ACTIVITY", 3)
    _write_session(
        manager,
        "main",
        [
            _run_summary(
                status="completed",
                at=BASE + timedelta(minutes=index),
                duration_ms=1000,
                run_id=f"run-{index}",
            )
            for index in range(runs)
        ],
    )

    activity = statistics().run_activity(since=BASE, until=BASE + timedelta(days=1))

    assert activity.total_runs == runs
    assert activity.truncated is truncated
    assert [run.run_id for run in activity.runs] == [
        f"run-{index}" for index in range(runs - 1, runs - 4, -1)
    ]


# -- Session scopes -----------------------------------------------------------


def test_identity_and_project_sessions_count_once_under_distinct_agent_keys(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    # The same bare Agent id and Session id exist as an identity Session and
    # inside a project; they are different Sessions and each counts once.
    for project_id, run_id, at in ((None, "identity-run", BASE), ("vbot", "project-run", BASE)):
        _write_session(
            manager,
            "builder",
            [
                _assistant(model="openai/gpt-5", at=at),
                _run_summary(status="completed", at=at, duration_ms=100, run_id=run_id),
            ],
            project_id=project_id,
            session_id="shared-session",
        )

    report = statistics(["builder"], projects={"vbot": ["builder"]}).report()

    assert report.overview.total_agents == 2
    assert {agent.agent_id for agent in report.overview.agents} == {"builder", "builder@vbot"}
    assert report.overview.total_sessions == 2
    assert report.overview.total_runs == 2
    assert {entry.agent_id: entry.runs for entry in report.runs.runs_per_agent} == {
        "builder": 1,
        "builder@vbot": 1,
    }


def test_an_empty_project_directory_changes_no_figure(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="openai/gpt-5", at=BASE, usage={"input_tokens": 10, "output_tokens": 2}
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=300, run_id="r1"
            ),
        ],
    )

    identity_only = statistics(["main"]).report().to_dict()
    with_projects = statistics(["main"], projects={}).report().to_dict()

    identity_only.pop("generated_at")
    with_projects.pop("generated_at")
    assert with_projects == identity_only
