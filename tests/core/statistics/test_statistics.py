"""Unit tests for the read-only statistics aggregation domain."""

from __future__ import annotations

import asyncio
import builtins
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.sessions import ChatSessionManager
from core.sessions.sessions import SKILL_CONTEXT_NOTE_PREFIX
from core.statistics import (
    AgentDirectory,
    CountEntry,
    ProjectDirectory,
    SkillInventorySource,
    StatisticsReport,
    StatisticsService,
)
from core.tools import tool_failure, tool_success

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _FakeAgent:
    id: str


class _FakeAgents:
    """Minimal :class:`AgentDirectory` stand-in for the scan."""

    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_FakeAgent(agent_id) for agent_id in agent_ids]

    def list(self) -> list[_FakeAgent]:
        return list(self._agents)


@dataclass(frozen=True)
class _FakeProject:
    project_id: str


class _FakeProjects:
    """Minimal :class:`ProjectDirectory` stand-in for project-scope discovery.

    Maps each project id to the agents that own sessions under its anchor,
    mirroring ``ProjectStore.session_owning_agents``.
    """

    def __init__(self, owners_by_project: dict[str, list[str]]) -> None:
        self._owners = {pid: list(agents) for pid, agents in owners_by_project.items()}

    # ``session_owning_agents`` references ``list[str]`` in its annotation; with a
    # method named ``list`` in this class, ``builtins.list`` keeps that resolving
    # to the builtin (mirrors the ProjectDirectory protocol in core/statistics).
    def list(self) -> builtins.list[_FakeProject]:
        return [_FakeProject(pid) for pid in sorted(self._owners)]

    def session_owning_agents(self, project_id: str) -> builtins.list[str]:
        return sorted(self._owners.get(project_id, []))


def _timing(start: datetime, duration_ms: int) -> dict:
    completed = start + timedelta(milliseconds=duration_ms)
    return {
        "started_at": start.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_ms": duration_ms,
    }


def _assistant(
    *,
    model: str,
    at: datetime,
    content: str | None = "ok",
    reasoning: str | None = None,
    usage: dict | None = None,
    tool_calls: list | None = None,
) -> ChatMessage:
    return ChatMessage.assistant(
        model=model,
        content=content,
        reasoning=reasoning,
        usage=usage,
        tool_calls=tool_calls,
        timestamp=at,
    )


def _tool(*, name: str, at: datetime, envelope: dict, duration_ms: int) -> ChatMessage:
    return ChatMessage.tool(
        tool_call_id=f"call-{name}-{at.isoformat()}",
        name=name,
        content=json.dumps(envelope),
        timing=_timing(at, duration_ms),
        timestamp=at,
    )


def _run_summary(
    *,
    status: str,
    at: datetime,
    duration_ms: int,
    run_id: str,
    model_step_count: int = 1,
) -> ChatMessage:
    return ChatMessage.run_summary(
        run_id=run_id,
        status=status,
        timing=_timing(at, duration_ms),
        model_step_count=model_step_count,
        timestamp=at,
    )


def _compaction(
    *,
    at: datetime,
    before: int,
    after: int,
    strategy: str = "summary_tail",
) -> ChatMessage:
    return ChatMessage.compaction_checkpoint(
        summary=f"Summary at {at.isoformat()}",
        projection=[ChatMessage.user("preserved tail", timestamp=at)],
        compacted_token_count=before - after,
        context_tokens_before=before,
        context_tokens_after=after,
        strategy=strategy,
        timestamp=at,
    )


def _write_session(manager: ChatSessionManager, agent_id: str, messages: list[ChatMessage]) -> str:
    session = manager.create(agent_id)
    for message in messages:
        session.append(message)
    return session.id


def _write_project_session(
    manager: ChatSessionManager,
    agent_id: str,
    project_id: str,
    messages: list[ChatMessage],
    *,
    session_id: str | None = None,
) -> str:
    session = manager.create(agent_id, session_id=session_id, project_id=project_id)
    for message in messages:
        session.append(message)
    return session.id


def _service(tmp_path: Path, agent_ids: list[str]) -> tuple[StatisticsService, ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(agent_ids)))
    return service, manager


def test_empty_data_returns_zeroed_report(tmp_path: Path) -> None:
    service, _manager = _service(tmp_path, [])
    report = service.report()

    assert isinstance(report, StatisticsReport)
    assert report.overview.total_agents == 0
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
    # Fully JSON-serializable.
    assert json.loads(json.dumps(report.to_dict()))["overview"]["total_runs"] == 0


def test_agent_with_no_sessions_counts_agent_only(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    manager.sessions_dir("main").mkdir(parents=True, exist_ok=True)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))

    report = service.report()

    assert report.overview.total_agents == 1
    assert report.overview.total_sessions == 0
    assert report.overview.agents[0].agent_id == "main"
    assert report.overview.agents[0].sessions == 0


def test_run_activity_returns_overlapping_runs_with_local_usage(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    session_id = _write_session(
        manager,
        "main",
        [
            _assistant(
                model="openai/gpt-5",
                at=BASE,
                usage={"input_tokens": 100, "output_tokens": 20},
            ),
            _assistant(
                model="openai/gpt-5",
                at=BASE + timedelta(minutes=1),
                usage={"input_tokens": 10, "output_tokens": 3, "estimated": True},
            ),
            _tool(
                name="read",
                at=BASE + timedelta(minutes=2),
                envelope=tool_success({"text": "ok"}),
                duration_ms=20,
            ),
            _run_summary(
                status="completed",
                at=BASE,
                duration_ms=10 * 60 * 1000,
                run_id="r1",
            ),
            _assistant(
                model="openai/gpt-5",
                at=BASE + timedelta(hours=2),
                usage={"input_tokens": 200, "output_tokens": 30},
            ),
            _run_summary(
                status="completed",
                at=BASE + timedelta(hours=2),
                duration_ms=100,
                run_id="r2",
            ),
        ],
    )

    report = service.run_activity(
        since=BASE + timedelta(minutes=5),
        until=BASE + timedelta(minutes=6),
    )

    assert report.total_runs == 1
    assert report.truncated is False
    run = report.runs[0]
    assert run.session_id == session_id
    assert run.run_id == "r1"
    assert run.models == ["openai/gpt-5"]
    assert run.tool_calls == 1
    assert run.measured_input_tokens == 100
    assert run.measured_output_tokens == 20
    assert run.estimated_input_tokens == 10
    assert run.estimated_output_tokens == 3


def test_chat_messages_and_session_records_are_separate(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    _write_session(
        manager,
        "main",
        [
            ChatMessage.user("hi", timestamp=BASE),
            _assistant(
                model="openrouter/anthropic/claude-sonnet-4", at=BASE + timedelta(seconds=1)
            ),
            ChatMessage.note("background", timestamp=BASE + timedelta(seconds=2)),
            _run_summary(
                status="completed",
                at=BASE + timedelta(seconds=3),
                duration_ms=1500,
                run_id="r1",
            ),
        ],
    )

    report = service.report()

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


def test_fork_counts_only_activity_appended_after_copied_history(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "openrouter/anthropic/claude-sonnet-4"
    source = manager.create("main")
    source_messages = [
        ChatMessage.user("source work", timestamp=BASE),
        _assistant(
            model=model,
            at=BASE + timedelta(seconds=1),
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
        _tool(
            name="edit",
            at=BASE + timedelta(seconds=2),
            envelope=tool_success({"changed": True}),
            duration_ms=4,
        ),
        ChatMessage.error(
            "source_error",
            "source failure",
            timestamp=BASE + timedelta(seconds=3),
        ),
        _run_summary(
            status="completed",
            at=BASE + timedelta(seconds=4),
            duration_ms=1000,
            run_id="source-run",
        ),
    ]
    for message in source_messages:
        source.append(message)

    fork = asyncio.run(manager.fork("main", source.id))
    fork_messages = [
        ChatMessage.user("fork work", timestamp=BASE + timedelta(minutes=1)),
        _assistant(
            model=model,
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
            "fork_error",
            "fork failure",
            timestamp=BASE + timedelta(minutes=1, seconds=3),
        ),
        _run_summary(
            status="failed",
            at=BASE + timedelta(minutes=1, seconds=4),
            duration_ms=500,
            run_id="fork-run",
        ),
    ]
    for message in fork_messages:
        fork.append(message)

    report = service.report()

    assert report.overview.total_sessions == 2
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


def test_interrupted_runs_have_distinct_count_rate_and_daily_bucket(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    session = manager.create("main")
    session.append(ChatMessage.user("work", timestamp=BASE))
    session.append(
        _run_summary(
            status="interrupted",
            at=BASE + timedelta(seconds=1),
            duration_ms=250,
            run_id="interrupted-run",
            model_step_count=0,
        )
    )

    report = service.report()

    assert report.overview.run_status.interrupted == 1
    assert report.overview.run_status.failed == 0
    assert report.overview.daily_trend[0].interrupted == 1
    assert report.runs.status.interrupted == 1
    assert report.runs.interruption_rate == pytest.approx(1.0)
    assert report.runs.failure_rate == pytest.approx(0.0)


def test_compactions_report_distribution_reclaim_strategy_window_and_forks(
    tmp_path: Path,
) -> None:
    service, manager = _service(tmp_path, ["main"])
    day_one = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    day_two = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    day_three = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)

    source = manager.create("main")
    source.append(_compaction(at=day_one, before=100_000, after=70_000))
    source.append(_compaction(at=day_two, before=90_000, after=30_000))

    fork = asyncio.run(manager.fork("main", source.id))
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
            _compaction(
                at=day_three + timedelta(hours=1),
                before=75_000,
                after=25_000,
            ),
        ],
    )

    report = service.report(since=day_two, until=day_three + timedelta(hours=2))
    compactions = report.compactions

    # The fork's copied source checkpoint is historical context, not new activity.
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
    assert {row.session_id for row in compactions.top_sessions[1:]} == {
        source.id,
        fork.id,
    }


def test_chat_messages_exclude_thinking_and_tool_only_model_steps(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "openrouter/anthropic/claude-sonnet-4"
    tool_call = ToolCall(id="call-read", name="read", arguments={"path": "README.md"})
    _write_session(
        manager,
        "main",
        [
            ChatMessage.user("inspect", timestamp=BASE),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=1),
                content=None,
                reasoning="I should inspect the file.",
                tool_calls=[tool_call],
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=2),
                content=None,
                tool_calls=[tool_call],
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=3),
                content="   ",
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=4),
                content="I found the cause; checking the fix.",
                reasoning="Summarize the finding.",
                tool_calls=[tool_call],
            ),
            _run_summary(
                status="completed",
                at=BASE + timedelta(seconds=5),
                duration_ms=5000,
                run_id="r1",
                model_step_count=4,
            ),
        ],
    )

    report = service.report()

    assert report.overview.chat_messages_by_role == {"user": 1, "assistant": 1}
    assert report.overview.total_chat_messages == 2
    assert report.overview.session_records_by_role["assistant"] == 4
    assert report.overview.agents[0].chat_messages == 2
    assert report.usage.totals.assistant_messages == 4
    assert report.runs.agent_messages == 1
    assert report.runs.model_steps == 4
    assert report.runs.average_agent_messages_per_run == 1.0
    assert report.runs.average_model_steps_per_run == 4.0


def test_run_segmentation_status_and_tool_calls(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "openrouter/anthropic/claude-sonnet-4"
    _write_session(
        manager,
        "main",
        [
            # Run 1 — completed, used a tool.
            _assistant(model=model, at=BASE),
            _tool(
                name="read",
                at=BASE + timedelta(seconds=1),
                envelope=tool_success({"text": "x"}),
                duration_ms=40,
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=2), duration_ms=2000, run_id="r1"
            ),
            # Run 2 — failed, no tools.
            _assistant(model=model, at=BASE + timedelta(seconds=3)),
            _run_summary(
                status="failed", at=BASE + timedelta(seconds=4), duration_ms=500, run_id="r2"
            ),
        ],
    )

    report = service.report()

    assert report.runs.total_runs == 2
    assert report.runs.status.completed == 1
    assert report.runs.status.failed == 1
    assert report.runs.runs_with_tool_calls == 1
    assert report.runs.total_tool_calls == 1
    assert report.runs.agent_messages == 2
    assert report.runs.model_steps == 2
    assert report.runs.average_agent_messages_per_run == 1.0
    assert report.runs.average_model_steps_per_run == 1.0
    assert report.runs.failure_rate == pytest.approx(0.5)
    assert report.overview.run_status.completed == 1


def test_derived_fallback_detects_mid_run_model_switch(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    _write_session(
        manager,
        "main",
        [
            _assistant(model="openrouter/anthropic/claude-sonnet-4", at=BASE),
            _assistant(model="openai/gpt-5", at=BASE + timedelta(seconds=1)),
            _run_summary(
                status="completed",
                at=BASE + timedelta(seconds=2),
                duration_ms=1000,
                run_id="r1",
                model_step_count=2,
            ),
            # Single-model run — no fallback.
            _assistant(model="openai/gpt-5", at=BASE + timedelta(seconds=3)),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=4), duration_ms=1000, run_id="r2"
            ),
        ],
    )

    report = service.report()

    assert report.runs.derived_fallback_runs == 1
    assert report.runs.total_runs == 2


def test_measured_and_estimated_tokens_stay_separate(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "openrouter/anthropic/claude-sonnet-4"
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 30,
                    "reasoning_tokens": 12,
                },
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=1),
                usage={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "reasoning_tokens": 2,
                    "estimated": True,
                },
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=2), duration_ms=1000, run_id="r1"
            ),
        ],
    )

    report = service.report()
    totals = report.usage.totals

    assert totals.measured_input_tokens == 100
    assert totals.measured_output_tokens == 20
    assert totals.estimated_input_tokens == 7
    assert totals.estimated_output_tokens == 3
    assert totals.measured_turns == 1
    assert totals.estimated_turns == 1
    assert totals.cache_read_tokens == 30
    assert totals.reasoning_tokens == 12
    assert totals.reasoning_turns == 1

    model_usage = report.usage.models[0]
    assert model_usage.provider == "openrouter"
    assert model_usage.model == "openrouter/anthropic/claude-sonnet-4"
    assert model_usage.measured_input_tokens == 100
    assert model_usage.estimated_input_tokens == 7
    assert model_usage.reasoning_tokens == 12
    assert model_usage.reasoning_turns == 1
    # Reasoning is already included in measured output, so it is not added.
    assert model_usage.total_tokens == 130
    assert model_usage.runs == 1

    provider_usage = report.usage.providers[0]
    assert provider_usage.reasoning_tokens == 12
    assert provider_usage.reasoning_turns == 1
    assert provider_usage.total_tokens == 130

    day = report.usage.daily[0]
    assert day.reasoning_tokens == 12
    assert day.reasoning_turns == 1


def test_cache_totals_split_per_provider_model_and_day(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    cached_model = "anthropic/claude-sonnet-4"
    plain_model = "ollama/llama3"
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=cached_model,
                at=BASE,
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 20,
                    "cache_read_tokens": 700,
                    "cache_write_tokens": 100,
                },
            ),
            _assistant(
                model=cached_model,
                at=BASE + timedelta(seconds=10),
                usage={
                    "input_tokens": 2000,
                    "output_tokens": 30,
                    "cache_read_tokens": 1800,
                    "cache_write_tokens": 50,
                },
            ),
            # Measured turn without any cache fields: counts into measured
            # totals but never into cache-turn denominators.
            _assistant(
                model=plain_model,
                at=BASE + timedelta(seconds=20),
                usage={"input_tokens": 500, "output_tokens": 10},
            ),
            _assistant(
                model=cached_model,
                at=BASE + timedelta(seconds=30),
                usage={"input_tokens": 9, "output_tokens": 1, "estimated": True},
            ),
        ],
    )

    report = service.report()
    totals = report.usage.totals

    assert totals.cache_turns == 2
    assert totals.cache_input_tokens == 3000
    assert totals.cache_read_tokens == 2500
    assert totals.cache_write_tokens == 150

    cached_provider = next(p for p in report.usage.providers if p.provider == "anthropic")
    assert cached_provider.cache_turns == 2
    assert cached_provider.cache_input_tokens == 3000
    assert cached_provider.cache_read_tokens == 2500
    assert cached_provider.cache_write_tokens == 150

    plain_provider = next(p for p in report.usage.providers if p.provider == "ollama")
    assert plain_provider.cache_turns == 0
    assert plain_provider.cache_input_tokens == 0
    assert plain_provider.cache_read_tokens == 0

    cached_model_usage = next(m for m in report.usage.models if m.model == cached_model)
    assert cached_model_usage.cache_turns == 2
    assert cached_model_usage.cache_read_tokens == 2500

    day = report.usage.daily[0]
    assert day.cache_input_tokens == 3000
    assert day.cache_read_tokens == 2500
    assert day.cache_write_tokens == 150


def test_session_cache_records_sorted_worst_hit_rate_first(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "anthropic/claude-sonnet-4"
    good_session = _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 900},
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=5),
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 900},
            ),
        ],
    )
    bad_session = _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 100},
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=5),
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 100},
            ),
        ],
    )
    # A single cache-reporting turn is not enough for a meaningful hit rate.
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 0},
            ),
        ],
    )

    report = service.report()
    records = report.usage.cache.lowest_hit_rate_sessions

    assert [record.session_id for record in records] == [bad_session, good_session]
    assert records[0].hit_rate == pytest.approx(0.1)
    assert records[0].cache_turns == 2
    assert records[0].input_tokens == 2000
    assert records[0].cache_read_tokens == 200
    assert records[1].hit_rate == pytest.approx(0.9)
    assert records[0].last_activity is not None


def test_suspected_cache_break_detected_for_prefix_collapse(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "anthropic/claude-sonnet-4"
    session_id = _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 10000, "output_tokens": 50, "cache_read_tokens": 9000},
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=30),
                usage={"input_tokens": 11000, "output_tokens": 60, "cache_read_tokens": 500},
            ),
        ],
    )

    report = service.report()
    breaks = report.usage.cache.suspected_breaks

    assert breaks.evaluated_turns == 1
    assert breaks.suspected_turns == 1
    incident = breaks.incidents[0]
    assert incident.agent_id == "main"
    assert incident.session_id == session_id
    assert incident.model == model
    assert incident.previous_input_tokens == 10000
    assert incident.cache_read_tokens == 500


def test_cache_break_heuristic_skips_legitimate_prefix_changes(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "anthropic/claude-sonnet-4"
    other_model = "anthropic/claude-haiku-4"

    def cached_turn(at: datetime, *, model_id: str = model, cache_read: int = 0) -> ChatMessage:
        return _assistant(
            model=model_id,
            at=at,
            usage={"input_tokens": 10000, "output_tokens": 10, "cache_read_tokens": cache_read},
        )

    # Model switch between turns: not evaluated.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE),
            cached_turn(BASE + timedelta(seconds=10), model_id=other_model),
        ],
    )
    # Idle gap beyond the provider cache TTL: not evaluated.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE),
            cached_turn(BASE + timedelta(seconds=301)),
        ],
    )
    # Compaction checkpoint between turns rebuilds the prefix: not evaluated.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE),
            ChatMessage.compaction_checkpoint(
                summary="compacted",
                projection=[ChatMessage.user("tail")],
                compacted_token_count=100,
                timestamp=BASE + timedelta(seconds=5),
            ),
            cached_turn(BASE + timedelta(seconds=10)),
        ],
    )
    # Previous prompt below the minimum cacheable size: not evaluated.
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 500, "output_tokens": 5, "cache_read_tokens": 0},
            ),
            cached_turn(BASE + timedelta(seconds=10)),
        ],
    )
    # Healthy continuation: evaluated, not suspected.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE, cache_read=9000),
            cached_turn(BASE + timedelta(seconds=10), cache_read=9800),
        ],
    )

    report = service.report()
    breaks = report.usage.cache.suspected_breaks

    assert breaks.evaluated_turns == 1
    assert breaks.suspected_turns == 0
    assert breaks.incidents == []


def test_tool_success_failure_envelopes_and_p95(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    # ten read calls: nine fast successes, one slow failure with an error code.
    messages: list[ChatMessage] = []
    for index in range(9):
        messages.append(
            _tool(
                name="read",
                at=BASE + timedelta(seconds=index),
                envelope=tool_success({"text": "x"}),
                duration_ms=10,
            )
        )
    messages.append(
        _tool(
            name="read",
            at=BASE + timedelta(seconds=9),
            envelope=tool_failure("not_found", "missing"),
            duration_ms=1000,
        )
    )
    messages.append(
        _run_summary(
            status="completed", at=BASE + timedelta(seconds=10), duration_ms=500, run_id="r1"
        )
    )
    _write_session(manager, "main", messages)

    report = service.report()
    read = next(tool for tool in report.tools.tools if tool.name == "read")

    assert read.calls == 10
    assert read.successes == 9
    assert read.failures == 1
    assert read.success_rate == pytest.approx(0.9)
    assert read.top_error_code == "not_found"
    assert read.error_codes == [CountEntry(key="not_found", count=1)]
    # nearest-rank P95 of ten samples is the tenth (the 1000 ms outlier).
    assert read.p95_duration_ms == 1000.0
    assert report.tools.total_calls == 10


def test_errors_grouped_by_kind_provider_model_agent_hour(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "openrouter/anthropic/claude-sonnet-4"
    _write_session(
        manager,
        "main",
        [
            _assistant(model=model, at=BASE),
            ChatMessage.error("rate_limit", "slow down", timestamp=BASE + timedelta(seconds=1)),
            ChatMessage.error("timeout", "too slow", timestamp=BASE + timedelta(seconds=2)),
            _run_summary(
                status="failed", at=BASE + timedelta(seconds=3), duration_ms=100, run_id="r1"
            ),
        ],
    )

    report = service.report()
    errors = report.errors

    assert errors.total_errors == 2
    kinds = {entry.key: entry.count for entry in errors.by_kind}
    assert kinds == {"rate_limit": 1, "timeout": 1}
    providers = {entry.key: entry.count for entry in errors.by_provider}
    assert providers == {"openrouter": 2}
    models = {entry.key: entry.count for entry in errors.by_model}
    assert models == {"openrouter/anthropic/claude-sonnet-4": 2}
    agents = {entry.key: entry.count for entry in errors.by_agent}
    assert agents == {"main": 2}
    assert errors.by_hour[12].count == 2
    assert report.usage.models[0].errors == 2


def test_error_without_preceding_model_is_unknown(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    _write_session(
        manager,
        "main",
        [ChatMessage.error("config_error", "bad config", timestamp=BASE)],
    )

    report = service.report()

    assert {entry.key for entry in report.errors.by_model} == {"unknown"}
    assert {entry.key for entry in report.errors.by_kind} == {"config_error"}


def test_percentiles_over_known_run_durations(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    messages: list[ChatMessage] = []
    for index in range(10):
        duration = (index + 1) * 100  # 100..1000
        messages.append(
            _run_summary(
                status="completed",
                at=BASE + timedelta(minutes=index),
                duration_ms=duration,
                run_id=f"r{index}",
            )
        )
    _write_session(manager, "main", messages)

    report = service.report()
    duration_stats = report.runs.duration

    assert duration_stats.count == 10
    assert duration_stats.average_ms == pytest.approx(550.0)
    assert duration_stats.p50_ms == 500.0
    assert duration_stats.p90_ms == 900.0
    assert duration_stats.p95_ms == 1000.0
    assert report.overview.median_run_duration_ms == 500.0


def test_since_until_windowing_filters_by_message_timestamp(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "openrouter/anthropic/claude-sonnet-4"
    day_one = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    day_two = datetime(2026, 6, 5, 9, 0, tzinfo=UTC)
    _write_session(
        manager,
        "main",
        [
            _assistant(model=model, at=day_one, usage={"input_tokens": 10, "output_tokens": 1}),
            _run_summary(
                status="completed", at=day_one + timedelta(seconds=1), duration_ms=111, run_id="r1"
            ),
            _assistant(model=model, at=day_two, usage={"input_tokens": 50, "output_tokens": 5}),
            _run_summary(
                status="completed", at=day_two + timedelta(seconds=1), duration_ms=222, run_id="r2"
            ),
        ],
    )

    full = service.report()
    assert full.runs.total_runs == 2

    windowed = service.report(
        since=datetime(2026, 6, 4, 0, 0, tzinfo=UTC),
        until=datetime(2026, 6, 6, 0, 0, tzinfo=UTC),
    )
    assert windowed.runs.total_runs == 1
    assert windowed.usage.totals.measured_input_tokens == 50
    assert windowed.window.since == "2026-06-04T00:00:00+00:00"
    # Daily series only holds in-window days.
    assert [point.date for point in windowed.usage.daily] == ["2026-06-05"]


def test_open_run_group_detected_without_trailing_summary(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "openrouter/anthropic/claude-sonnet-4"
    _write_session(
        manager,
        "main",
        [
            _assistant(model=model, at=BASE),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=100, run_id="r1"
            ),
            # A second assistant turn with no terminal run_summary → open group.
            _assistant(model=model, at=BASE + timedelta(seconds=2)),
        ],
    )

    report = service.report()

    assert report.runs.total_runs == 1
    assert report.overview.open_run_groups == 1


def test_multiple_agents_and_daily_trend(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main", "research"])
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

    report = service.report()

    assert report.overview.total_agents == 2
    assert {entry.agent_id for entry in report.runs.runs_per_agent} == {"main", "research"}
    trend = {
        point.date: (
            point.runs,
            point.completed,
            point.failed,
            point.cancelled,
        )
        for point in report.overview.daily_trend
    }
    assert trend["2026-06-01"] == (1, 0, 1, 0)
    assert trend["2026-06-02"] == (1, 1, 0, 0)


def test_project_session_appears_under_address_form(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    model = "openai/gpt-5"
    _write_project_session(
        manager,
        "builder",
        "vbot",
        [
            _assistant(model=model, at=BASE),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=400, run_id="p1"
            ),
        ],
    )
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents([])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
    )

    report = service.report()

    # The project agent is keyed by its outer address form, distinct from a bare id.
    assert report.overview.total_agents == 1
    assert report.overview.agents[0].agent_id == "builder@vbot"
    assert report.overview.total_runs == 1
    assert {entry.agent_id for entry in report.runs.runs_per_agent} == {"builder@vbot"}


def test_identity_and_project_agents_coexist_distinctly(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    model = "openai/gpt-5"
    # Same bare agent id "builder" both as an identity agent and inside a project.
    _write_session(
        manager,
        "builder",
        [
            _assistant(model=model, at=BASE),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=100, run_id="i1"
            ),
        ],
    )
    _write_project_session(
        manager,
        "builder",
        "vbot",
        [
            _assistant(model=model, at=BASE + timedelta(seconds=2)),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=3), duration_ms=200, run_id="p1"
            ),
        ],
    )
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["builder"])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
    )

    report = service.report()

    per_agent = {entry.agent_id: entry.runs for entry in report.runs.runs_per_agent}
    assert per_agent == {"builder": 1, "builder@vbot": 1}
    assert report.overview.total_agents == 2
    assert report.overview.total_runs == 2


def test_no_projects_report_matches_identity_only_scan(tmp_path: Path) -> None:
    # Same data dir, same files: an empty project source must not change a single
    # figure versus the identity-only service (no double counting, no new keys).
    manager = ChatSessionManager(tmp_path)
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

    baseline = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))
    with_projects = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        cast(ProjectDirectory, _FakeProjects({})),
    )

    baseline_report = baseline.report().to_dict()
    project_report = with_projects.report().to_dict()
    # generated_at is a wall-clock field — compare everything else.
    baseline_report.pop("generated_at")
    project_report.pop("generated_at")
    assert project_report == baseline_report


def test_same_session_id_across_scopes_is_not_double_counted(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    model = "openai/gpt-5"
    shared_id = "shared-session"
    # One identity session and one project session deliberately share a session id;
    # they are different files under different anchors and must both count once.
    _write_project_session(
        manager,
        "builder",
        "vbot",
        [
            _assistant(model=model, at=BASE),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=100, run_id="p1"
            ),
        ],
        session_id=shared_id,
    )
    identity_session = manager.create("builder", session_id=shared_id)
    for message in [
        _assistant(model=model, at=BASE + timedelta(seconds=2)),
        _run_summary(
            status="completed", at=BASE + timedelta(seconds=3), duration_ms=100, run_id="i1"
        ),
    ]:
        identity_session.append(message)

    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["builder"])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
    )

    report = service.report()

    # Two distinct files → two sessions, two runs, no collision/double count.
    assert report.overview.total_sessions == 2
    assert report.overview.total_runs == 2
    per_agent = {entry.agent_id: entry.runs for entry in report.runs.runs_per_agent}
    assert per_agent == {"builder": 1, "builder@vbot": 1}


# ---------------------------------------------------------------------------
# Skills section — end-to-end through the service (offered from seen_skills
# metadata, activated from persisted notes, joined against an injected inventory).
# ---------------------------------------------------------------------------


class _FakeInventory:
    """Minimal :class:`SkillInventorySource` for the service-level skills tests."""

    def __init__(
        self,
        *,
        global_skills: list[tuple[str, str | None]] | None = None,
        agent_skills: dict[str, frozenset[str]] | None = None,
        project_skills: dict[str, list[tuple[str, str | None]]] | None = None,
    ) -> None:
        self._global = list(global_skills or [])
        self._agent = dict(agent_skills or {})
        self._project = dict(project_skills or {})

    def global_skills(self) -> list[tuple[str, str | None]]:
        return list(self._global)

    def agent_skill_names(self, agent_id: str) -> frozenset[str]:
        return self._agent.get(agent_id, frozenset())

    def project_skills(self, project_id: str) -> list[tuple[str, str | None]]:
        return list(self._project.get(project_id, []))


def _skill_note(name: str, at: datetime) -> ChatMessage:
    return ChatMessage.note(
        SKILL_CONTEXT_NOTE_PREFIX + json.dumps({"name": name, "content": f"{name} body"}),
        timestamp=at,
    )


def _skills_row(report: StatisticsReport, name: str):
    return next(row for row in report.skills.skills if row.name == name)


def test_skills_offered_from_metadata_and_activated_from_notes(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled"), ("teach", "global")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("main")
    session.append(ChatMessage.user("hi", timestamp=BASE))
    session.append(_skill_note("deploy", BASE + timedelta(seconds=1)))
    manager.set_metadata("main", session.id, {"seen_skills": ["deploy", "teach"]})

    report = service.report()

    deploy = _skills_row(report, "deploy")
    teach = _skills_row(report, "teach")
    assert deploy.offered_sessions == 1
    assert deploy.activated_sessions == 1
    assert deploy.activated_offered_sessions == 1
    assert deploy.usage_rate == 1.0
    assert deploy.by_agent == [type(deploy.by_agent[0])(key="main", count=1)]
    # teach was offered but never activated.
    assert teach.offered_sessions == 1
    assert teach.activated_sessions == 0
    assert report.skills.total_skills == 2
    assert report.skills.used_skills == 1
    assert report.skills.never_used_skills == 1
    assert report.skills.offered_unactivated_skills == 1
    assert report.skills.skills_without_offer_data == 0


def test_skills_usage_for_deleted_name_is_dropped(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    # Inventory holds only "deploy"; the session used a now-deleted "legacy" skill.
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("main")
    session.append(_skill_note("legacy", BASE + timedelta(seconds=1)))
    manager.set_metadata("main", session.id, {"seen_skills": ["legacy", "deploy"]})

    report = service.report()

    assert {row.name for row in report.skills.skills} == {"deploy"}
    assert _skills_row(report, "deploy").offered_sessions == 1
    assert _skills_row(report, "deploy").activated_sessions == 0


def test_skills_default_service_has_empty_section(tmp_path: Path) -> None:
    # No injected inventory → every usage drops, zero counts, valid section.
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))
    session = manager.create("main")
    session.append(_skill_note("deploy", BASE))
    manager.set_metadata("main", session.id, {"seen_skills": ["deploy"]})

    report = service.report()

    assert report.skills.skills == []
    assert report.skills.total_skills == 0
    assert report.skills.offered_unactivated_skills == 0
    assert report.skills.skills_without_offer_data == 0
    # Fully JSON-serializable with the skills section present.
    assert json.loads(json.dumps(report.to_dict()))["skills"]["total_skills"] == 0


def test_skills_project_agent_keyed_by_address_form(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents([])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("builder", project_id="vbot")
    session.append(_skill_note("deploy", BASE + timedelta(seconds=1)))
    manager.set_metadata("builder", session.id, {"seen_skills": ["deploy"]}, project_id="vbot")

    report = service.report()
    deploy = _skills_row(report, "deploy")

    assert [entry.key for entry in deploy.by_agent] == ["builder@vbot"]
    assert deploy.by_agent[0].count == 1


def test_skills_window_filters_offered_and_activated(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    # Session created (first message) before the window; an activation note fires
    # inside it. Offered filters by created_at (excluded); activated by note
    # timestamp (included) — and never-used is window-independent.
    session = manager.create("main")
    session.append(ChatMessage.user("hi", timestamp=BASE))
    session.append(_skill_note("deploy", BASE + timedelta(hours=2)))
    manager.set_metadata("main", session.id, {"seen_skills": ["deploy"]})

    report = service.report(since=BASE + timedelta(hours=1))
    deploy = _skills_row(report, "deploy")

    assert deploy.offered_sessions == 0
    assert deploy.activated_sessions == 1
    assert deploy.activated_offered_sessions == 0
    assert deploy.usage_rate is None
    assert report.skills.never_used_skills == 0


def test_skills_malformed_skill_context_note_is_ignored(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("main")
    # A [skill-context] note with a broken JSON payload must not crash the scan
    # nor count as an activation.
    session.append(ChatMessage.note(SKILL_CONTEXT_NOTE_PREFIX + "{broken", timestamp=BASE))
    manager.set_metadata("main", session.id, {"seen_skills": ["deploy"]})

    report = service.report()

    assert _skills_row(report, "deploy").activated_sessions == 0
    assert _skills_row(report, "deploy").offered_sessions == 1
