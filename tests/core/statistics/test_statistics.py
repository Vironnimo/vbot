"""Unit tests for the read-only statistics aggregation domain."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from core.chat.messages import ChatMessage
from core.sessions import ChatSessionManager
from core.statistics import (
    AgentDirectory,
    CountEntry,
    ProjectDirectory,
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

    def list(self) -> list[_FakeProject]:
        return [_FakeProject(pid) for pid in sorted(self._owners)]

    def session_owning_agents(self, project_id: str) -> list[str]:
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
    content: str = "ok",
    usage: dict | None = None,
    tool_calls: list | None = None,
) -> ChatMessage:
    return ChatMessage.assistant(
        model=model,
        content=content,
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


def _run_summary(*, status: str, at: datetime, duration_ms: int, run_id: str) -> ChatMessage:
    return ChatMessage.run_summary(
        run_id=run_id,
        status=status,
        timing=_timing(at, duration_ms),
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
    assert report.overview.messages_by_role["assistant"] == 0
    assert report.usage.providers == []
    assert report.runs.duration.p95_ms is None
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


def test_messages_by_role_and_last_activity(tmp_path: Path) -> None:
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

    assert report.overview.messages_by_role["user"] == 1
    assert report.overview.messages_by_role["assistant"] == 1
    assert report.overview.messages_by_role["note"] == 1
    assert report.overview.messages_by_role["run_summary"] == 1
    assert report.overview.total_messages == 4
    assert report.overview.total_sessions == 1
    assert report.overview.last_activity is not None
    assert report.overview.agents[0].runs == 1


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
                status="completed", at=BASE + timedelta(seconds=2), duration_ms=1000, run_id="r1"
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
                usage={"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 30},
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=1),
                usage={"input_tokens": 7, "output_tokens": 3, "estimated": True},
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

    model_usage = report.usage.models[0]
    assert model_usage.provider == "openrouter"
    assert model_usage.model == "openrouter/anthropic/claude-sonnet-4"
    assert model_usage.measured_input_tokens == 100
    assert model_usage.estimated_input_tokens == 7
    assert model_usage.runs == 1


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
    trend = {point.date: (point.runs, point.errors) for point in report.overview.daily_trend}
    assert trend["2026-06-01"] == (1, 1)
    assert trend["2026-06-02"] == (1, 0)


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
