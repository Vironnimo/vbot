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
from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.statistics_methods import _statistics_report

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _FakeAgent:
    id: str


class _FakeAgents:
    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_FakeAgent(agent_id) for agent_id in agent_ids]

    def list(self) -> list[_FakeAgent]:
        return list(self._agents)


def _timing(start: datetime, duration_ms: int) -> dict:
    return {
        "started_at": start.isoformat(),
        "completed_at": (start + timedelta(milliseconds=duration_ms)).isoformat(),
        "duration_ms": duration_ms,
    }


def _state(tmp_path: Path, agent_ids: list[str]) -> tuple[SimpleNamespace, ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    runtime = SimpleNamespace(
        chat_sessions=manager,
        agents=_FakeAgents(agent_ids),
        projects=ProjectStore(tmp_path),
    )
    return SimpleNamespace(runtime=runtime), manager


def _seed_session(manager: ChatSessionManager, agent_id: str) -> None:
    session = manager.create(agent_id)
    session.append(
        ChatMessage.assistant(
            model="openrouter/anthropic/claude-sonnet-4",
            content="hi",
            usage={"input_tokens": 30, "output_tokens": 5},
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

    assert set(result) == {"generated_at", "window", "overview", "usage", "runs", "errors", "tools"}
    assert result["overview"]["total_agents"] == 1
    assert result["overview"]["total_runs"] == 1
    assert result["usage"]["totals"]["measured_input_tokens"] == 30
    assert result["runs"]["duration"]["p95_ms"] == 1200.0
    assert result["window"] == {"since": None, "until": None}


def test_report_applies_time_window(tmp_path: Path) -> None:
    state, manager = _state(tmp_path, ["main"])
    _seed_session(manager, "main")

    result = _statistics_report(
        state,
        {"since": "2026-07-01T00:00:00Z", "until": "2026-07-31T00:00:00Z"},
    )

    assert result["overview"]["total_runs"] == 0
    assert result["window"]["since"] == "2026-07-01T00:00:00+00:00"


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


def test_statistics_report_is_registered() -> None:
    handlers = build_method_handlers()

    assert "statistics.report" in handlers
