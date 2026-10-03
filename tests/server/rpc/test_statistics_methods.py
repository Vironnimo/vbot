"""Tests for the ``statistics.*`` RPC handlers.

The report computations are owned by ``core.statistics``; these tests cover the
RPC edge: parameter validation, the payload form, the default timezone, and the
runtime wiring of the service (Sessions, Agents, Projects, the Skill inventory
and the index).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat.messages import ChatMessage
from core.database import write_bootstrap_marker
from core.projects import ProjectStore
from core.sessions import ChatSession, ChatSessionManager
from core.statistics import REPORT_SECTIONS, StatisticsIndex
from server.rpc.methods import dispatch_rpc
from server.rpc.statistics_methods import _RuntimeSkillInventory
from tests.core.sessions.history_fixtures import complete_run

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
JsonObject = dict[str, Any]


@dataclass(frozen=True)
class _FakeAgent:
    id: str


class _FakeAgents:
    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_FakeAgent(agent_id) for agent_id in agent_ids]

    def list_with_builtins(self) -> list[_FakeAgent]:
        return list(self._agents)


class _FakeSkillRegistry:
    """Stand-in for the runtime's global skill registry (``skills_for`` result)."""

    def __init__(self, skills: list[Any]) -> None:
        self._skills = skills

    def list_all(self) -> list[Any]:
        return list(self._skills)


class _RuntimeStub:
    """A runtime with the surface the statistics service wiring reads."""

    def __init__(self, data_dir: Path, manager: ChatSessionManager, agent_ids: list[str]) -> None:
        self._data_dir = data_dir
        self.chat_sessions = manager
        self.statistics_index = StatisticsIndex(data_dir)
        self.usage_recorder = None
        self.agents = _FakeAgents(agent_ids)
        self.projects = ProjectStore(data_dir, sessions=manager)
        self.global_skills: list[Any] = []
        self.models = SimpleNamespace(pricing_for=lambda _: None)

    def skills_for(self, project_id: str | None, agent_id: str | None = None) -> Any:
        return _FakeSkillRegistry(self.global_skills)

    def timezone_name(self) -> str:
        return "Europe/Berlin"

    def agent_skills_dir(self, agent_id: str) -> Path:
        return self._data_dir / "agents" / agent_id / "skills"

    def project_own_skills(self, project_id: str) -> list[Any]:
        return []


def _timing(start: datetime, duration_ms: int) -> dict[str, Any]:
    return {
        "started_at": start.isoformat(),
        "completed_at": (start + timedelta(milliseconds=duration_ms)).isoformat(),
        "duration_ms": duration_ms,
    }


def _state(tmp_path: Path, agent_ids: list[str]) -> tuple[SimpleNamespace, ChatSessionManager]:
    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    runtime = _RuntimeStub(tmp_path, manager, agent_ids)
    return SimpleNamespace(runtime=runtime), manager


def _complete(session: ChatSession, run_id: str, assistant: ChatMessage, duration_ms: int) -> None:
    session = session.start_run(run_id)
    session.append(assistant)
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id=run_id,
            status="completed",
            iteration_count=1,
            timing=_timing(BASE + timedelta(seconds=1), duration_ms),
            timestamp=BASE + timedelta(seconds=2),
        ),
    )


def _seed_session(manager: ChatSessionManager, agent_id: str) -> None:
    _complete(
        manager.create(agent_id),
        "r1",
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
        ),
        1200,
    )


def _skill(name: str, origin: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, origin=origin)


async def _result(state: Any, method: str, params: JsonObject) -> JsonObject:
    response = await dispatch_rpc(state, {"method": method, "params": params})
    assert response["ok"] is True, response
    result: JsonObject = response["result"]
    return result


@pytest.mark.asyncio
async def test_report_returns_the_requested_sections_for_the_window_and_timezone(
    tmp_path: Path,
) -> None:
    state, manager = _state(tmp_path, ["main"])
    state.runtime.global_skills = [_skill("deploy", "bundled")]
    _seed_session(manager, "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    state.runtime.projects.create("vbot", "vBot", repo)
    _complete(
        manager.create("builder", project_id="vbot"),
        "p1",
        ChatMessage.assistant(model="openai/gpt-5", content="hi", timestamp=BASE),
        1200,
    )

    result = await _result(state, "statistics.report", {})
    service = state.statistics_service
    windowed = await _result(
        state,
        "statistics.report",
        {
            "since": "2026-07-01T00:30:00+02:00",
            "until": "2026-07-31T00:00:00Z",
            "timezone": "UTC",
            "sections": ["runs", "overview", "runs"],
        },
    )

    assert set(result) == {"generated_at", "window", *REPORT_SECTIONS}
    # Without a timezone the Settings timezone applies.
    assert result["window"] == {
        "since": None,
        "until": None,
        "timezone": "Europe/Berlin",
        "bucket": "day",
    }
    assert result["extensions"] == {"extensions": []}
    # Project Sessions count under their ``agent@project`` address.
    assert result["overview"]["active_agents"] == 2
    assert {row["agent_id"] for row in result["runs"]["agents"]} == {"main", "builder@vbot"}
    assert result["runs"]["totals"]["completed"] == 2
    # The runtime Skill inventory is joined into the skills section.
    assert [row["name"] for row in result["skills"]["skills"]] == ["deploy"]
    # The window is normalized to whole UTC hours and applied; only the
    # requested sections are built; the service is built once.
    assert set(windowed) == {"generated_at", "window", "overview", "runs"}
    assert windowed["window"] == {
        "since": "2026-06-30T22:00:00.000000Z",
        "until": "2026-07-31T00:00:00.000000Z",
        "timezone": "UTC",
        "bucket": "day",
    }
    assert windowed["runs"]["totals"]["total"] == 0
    assert state.statistics_service is service
    assert service._index is state.runtime.statistics_index


@pytest.mark.asyncio
async def test_run_activity_returns_correlated_run_details(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")

    result = await _result(
        state,
        "statistics.run_activity",
        {"since": "2026-06-01T12:00:00Z", "until": "2026-06-01T12:01:00Z"},
    )

    assert result["total_runs"] == 1
    assert result["truncated"] is False
    assert result["runs"][0]["run_id"] == "r1"
    assert result["runs"][0]["measured_input_tokens"] == 30


_INVERTED = {"since": "2026-06-10T00:00:00Z", "until": "2026-06-01T00:00:00Z"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "fragment"),
    [
        pytest.param("statistics.report", {"bogus": 1}, "bogus", id="unknown-field"),
        pytest.param("statistics.report", {"since": "not-a-date"}, "since", id="malformed"),
        pytest.param("statistics.report", _INVERTED, "since", id="inverted"),
        pytest.param("statistics.report", {"timezone": "Mars/Olympus"}, "timezone", id="zone"),
        pytest.param("statistics.report", {"sections": ["bogus"]}, "bogus", id="section"),
        pytest.param("statistics.report", {"sections": []}, "sections", id="no-sections"),
        pytest.param("statistics.report", {"sections": "runs"}, "sections", id="section-text"),
        # Run activity is bounded: both ends of the window are required.
        pytest.param(
            "statistics.run_activity",
            {"since": "2026-06-01T12:00:00Z"},
            "until",
            id="activity-open-window",
        ),
        pytest.param("statistics.run_activity", _INVERTED, "since", id="activity-inverted"),
    ],
)
async def test_invalid_statistics_requests_are_rejected(
    tmp_path: Path, method: str, params: JsonObject, fragment: str
) -> None:
    state, _manager = _state(tmp_path, ["main"])

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert fragment in response["error"]["message"]


def test_runtime_skill_inventory_reads_global_agent_and_project_scopes(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
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
    runtime.project_own_skills = lambda project_id: [_skill("proj")]  # type: ignore[method-assign]

    inventory = _RuntimeSkillInventory(runtime)

    assert inventory.global_skills() == [("deploy", "bundled"), ("teach", "global")]
    assert inventory.agent_skill_names("assistant") == frozenset({"private"})
    # Missing agent home: an empty set, no crash.
    assert inventory.agent_skill_names("nobody") == frozenset()
    # Project skills are tagged with the project display name.
    assert inventory.project_skills("vbot") == [("proj", "project:vBot")]
