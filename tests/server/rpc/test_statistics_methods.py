"""Tests for the ``statistics.report`` RPC handler.

Coverage:
- returns the full report shape for a seeded data dir,
- rejects unknown params and malformed / inverted time windows,
- empty-data returns a zeroed report without error,
- the handler is registered in the method table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.chat.messages import ChatMessage
from core.projects import ProjectStore
from core.sessions import ChatSessionManager
from core.sessions.sessions import SKILL_CONTEXT_NOTE_PREFIX
from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.statistics_methods import (
    _RuntimeSkillInventory,
    _statistics_report,
    _statistics_run_activity,
)

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _FakeAgent:
    id: str


class _FakeAgents:
    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_FakeAgent(agent_id) for agent_id in agent_ids]

    def list(self) -> list[_FakeAgent]:
        return list(self._agents)


class _FakeSkillRegistry:
    """Stand-in for the runtime's global skill registry (``skills_for`` result)."""

    def __init__(self, skills: list) -> None:
        self._skills = skills

    def list_all(self) -> list:
        return list(self._skills)


class _RuntimeStub:
    """A runtime with the skill-inventory surface the RPC adapter reads.

    The default ``StatisticsService`` wiring in ``_statistics_service`` now builds
    a ``_RuntimeSkillInventory`` over the runtime, so the fake runtime must answer
    ``skills_for`` / ``agent_skills_dir`` / ``project_own_skills``. Empty by
    default (no skills), so pre-existing assertions on the other sections stay
    valid while the skills section builds cleanly.
    """

    def __init__(self, data_dir: Path, manager: ChatSessionManager, agent_ids: list[str]) -> None:
        self._data_dir = data_dir
        self.chat_sessions = manager
        self.agents = _FakeAgents(agent_ids)
        self.projects = ProjectStore(data_dir)
        self.global_skills: list = []

    def skills_for(self, project_id, agent_id=None) -> _FakeSkillRegistry:
        return _FakeSkillRegistry(self.global_skills)

    def agent_skills_dir(self, agent_id: str) -> Path:
        return self._data_dir / "agents" / agent_id / "skills"

    def project_own_skills(self, project_id: str) -> list:
        return []


def _timing(start: datetime, duration_ms: int) -> dict:
    return {
        "started_at": start.isoformat(),
        "completed_at": (start + timedelta(milliseconds=duration_ms)).isoformat(),
        "duration_ms": duration_ms,
    }


def _state(tmp_path: Path, agent_ids: list[str]) -> tuple[SimpleNamespace, ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    runtime = _RuntimeStub(tmp_path, manager, agent_ids)
    return SimpleNamespace(runtime=runtime), manager


def _seed_session(manager: ChatSessionManager, agent_id: str) -> None:
    session = manager.create(agent_id)
    session.append(
        ChatMessage.assistant(
            model="openrouter/anthropic/claude-sonnet-4",
            content="hi",
            usage={
                "input_tokens": 30,
                "output_tokens": 5,
                "cache_write_tokens": 4,
                "reasoning_tokens": 3,
            },
            timestamp=BASE,
        )
    )
    session.append(
        ChatMessage.run_summary(
            run_id="r1",
            status="completed",
            timing=_timing(BASE + timedelta(seconds=1), 1200),
            timestamp=BASE + timedelta(seconds=2),
        )
    )


def test_report_returns_full_shape_for_seeded_data(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")

    result = _statistics_report(state, {})

    assert set(result) == {
        "generated_at",
        "window",
        "overview",
        "usage",
        "runs",
        "compactions",
        "errors",
        "tools",
        "skills",
    }
    assert result["overview"]["total_agents"] == 1
    assert result["overview"]["total_runs"] == 1
    assert result["usage"]["totals"]["measured_input_tokens"] == 30
    assert result["usage"]["totals"]["cache_write_tokens"] == 4
    assert result["usage"]["totals"]["reasoning_tokens"] == 3
    assert result["usage"]["totals"]["reasoning_turns"] == 1
    assert result["usage"]["providers"][0]["reasoning_tokens"] == 3
    assert result["usage"]["models"][0]["reasoning_tokens"] == 3
    assert result["usage"]["daily"][0]["reasoning_tokens"] == 3
    assert result["runs"]["duration"]["p95_ms"] == 1200.0
    assert result["compactions"]["total_compactions"] == 0
    assert result["window"] == {"since": None, "until": None}


def test_report_applies_time_window(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")

    result = _statistics_report(
        state,
        {"since": "2026-07-01T00:00:00Z", "until": "2026-07-31T00:00:00Z"},
    )

    assert result["overview"]["total_runs"] == 0
    assert result["compactions"]["total_compactions"] == 0
    assert result["window"]["since"] == "2026-07-01T00:00:00+00:00"


def test_run_activity_returns_correlated_run_details(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")

    result = _statistics_run_activity(
        state,
        {
            "since": "2026-06-01T12:00:00Z",
            "until": "2026-06-01T12:01:00Z",
        },
    )

    assert result["total_runs"] == 1
    assert result["truncated"] is False
    assert result["runs"][0]["run_id"] == "r1"
    assert result["runs"][0]["measured_input_tokens"] == 30


def test_run_activity_requires_complete_window(tmp_path: Path) -> None:
    state, _manager = _state(tmp_path, ["main"])

    with pytest.raises(RpcError, match="params.until must be an ISO 8601 timestamp string"):
        _statistics_run_activity(state, {"since": "2026-06-01T12:00:00Z"})


def test_report_lazily_caches_service_on_state(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")

    _statistics_report(state, {})
    cached = state.statistics_service
    _statistics_report(state, {})

    assert state.statistics_service is cached


def test_report_rejects_unknown_params(tmp_path: Path) -> None:
    state, _manager = _state(tmp_path, ["main"])

    with pytest.raises(RpcError, match="unsupported statistics.report fields: bogus"):
        _statistics_report(state, {"bogus": 1})


def test_report_rejects_malformed_timestamp(tmp_path: Path) -> None:
    state, _manager = _state(tmp_path, ["main"])

    with pytest.raises(RpcError, match="params.since must be an ISO 8601 timestamp string"):
        _statistics_report(state, {"since": "not-a-date"})


def test_report_rejects_inverted_window(tmp_path: Path) -> None:
    state, _manager = _state(tmp_path, ["main"])

    with pytest.raises(RpcError, match="params.since must not be after params.until"):
        _statistics_report(
            state,
            {"since": "2026-06-10T00:00:00Z", "until": "2026-06-01T00:00:00Z"},
        )


def test_report_empty_data_returns_zeroed_report(tmp_path: Path) -> None:
    state, _manager = _state(tmp_path, [])

    result = _statistics_report(state, {})

    assert result["overview"]["total_agents"] == 0
    assert result["overview"]["total_runs"] == 0
    assert result["errors"]["total_errors"] == 0
    assert result["tools"]["tools"] == []


def test_report_includes_project_sessions_under_address_form(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    state.runtime.projects.create("vbot", "vBot", repo)
    project_session = manager.create("builder", project_id="vbot")
    project_session.append(
        ChatMessage.assistant(model="openai/gpt-5", content="hi", timestamp=BASE)
    )
    project_session.append(
        ChatMessage.run_summary(
            run_id="p1",
            status="completed",
            timing=_timing(BASE + timedelta(seconds=1), 800),
            timestamp=BASE + timedelta(seconds=2),
        )
    )

    result = _statistics_report(state, {})

    agent_ids = {agent["agent_id"] for agent in result["overview"]["agents"]}
    assert agent_ids == {"main", "builder@vbot"}
    assert result["overview"]["total_runs"] == 2


def _skill(name: str, origin: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, origin=origin)


def test_runtime_skill_inventory_reads_global_agent_and_project_scopes(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    runtime = _RuntimeStub(tmp_path, manager, ["assistant"])
    runtime.global_skills = [_skill("deploy", "bundled"), _skill("teach", "global")]
    # An agent private skills home on disk.
    agent_home = runtime.agent_skills_dir("assistant")
    (agent_home / "private").mkdir(parents=True)
    (agent_home / "private" / "SKILL.md").write_text(
        "---\nname: private\ndescription: A private skill.\n---\nBody.\n",
        encoding="utf-8",
    )
    # A registered project with its own skills.
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime.projects.create("vbot", "vBot", repo)
    runtime.project_own_skills = lambda project_id: [_skill("proj")]  # type: ignore[assignment]

    inventory = _RuntimeSkillInventory(runtime)

    assert inventory.global_skills() == [("deploy", "bundled"), ("teach", "global")]
    assert inventory.agent_skill_names("assistant") == frozenset({"private"})
    # Missing agent home → empty set (no crash).
    assert inventory.agent_skill_names("nobody") == frozenset()
    # Project skills tagged with the project display name.
    assert inventory.project_skills("vbot") == [("proj", "project:vBot")]


def test_report_skills_section_joins_usage_against_inventory(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    state.runtime.global_skills = [_skill("deploy", "bundled"), _skill("teach", "global")]
    session = manager.create("main")
    session.append(ChatMessage.user("hi", timestamp=BASE))
    session.append(
        ChatMessage.note(
            SKILL_CONTEXT_NOTE_PREFIX + '{"name":"deploy","content":"body"}',
            timestamp=BASE + timedelta(seconds=1),
        )
    )
    manager.set_metadata("main", session.id, {"seen_skills": ["deploy", "teach"]})

    result = _statistics_report(state, {})
    skills = result["skills"]
    by_name = {row["name"]: row for row in skills["skills"]}

    assert skills["total_skills"] == 2
    assert skills["used_skills"] == 1
    assert skills["never_used_skills"] == 1
    assert skills["offered_unactivated_skills"] == 1
    assert skills["skills_without_offer_data"] == 0
    assert by_name["deploy"]["offered_sessions"] == 1
    assert by_name["deploy"]["activated_sessions"] == 1
    assert by_name["deploy"]["activated_offered_sessions"] == 1
    assert by_name["deploy"]["usage_rate"] == 1.0
    assert by_name["deploy"]["by_agent"] == [{"key": "main", "count": 1}]
    assert by_name["teach"]["activated_sessions"] == 0


def test_report_skills_section_empty_when_no_inventory_skills(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")

    result = _statistics_report(state, {})

    assert result["skills"]["skills"] == []
    assert result["skills"]["total_skills"] == 0


def test_statistics_report_is_registered() -> None:
    handlers = build_method_handlers()

    assert "statistics.report" in handlers
    assert "statistics.run_activity" in handlers
