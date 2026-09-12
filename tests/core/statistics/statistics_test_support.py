"""Shared fixtures and fakes for statistics behavior tests."""

from __future__ import annotations

import builtins
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from core.chat.messages import ChatMessage
from core.sessions import ChatSessionManager
from core.statistics import (
    AgentDirectory,
    StatisticsService,
)

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


def _run_summary(*, status: str, at: datetime, duration_ms: int, run_id: str) -> ChatMessage:
    return ChatMessage.run_summary(
        run_id=run_id,
        status=status,
        timing=_timing(at, duration_ms),
        iteration_count=1,
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


def _service(tmp_path: Path, agent_ids: list[str]) -> tuple[StatisticsService, ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(agent_ids)))
    return service, manager
