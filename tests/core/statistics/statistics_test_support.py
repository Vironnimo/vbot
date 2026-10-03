"""Shared fakes and history builders for Statistics behavior tests."""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.chat.messages import ChatMessage
from core.sessions import (
    ChatSession,
    ChatSessionManager,
    SessionAddress,
    SessionReadBatch,
    SessionReadCursor,
)
from core.sessions._types import SessionRunAdmission
from core.statistics import StatisticsService
from core.usage import UsageRecorder
from core.utils.timestamps import format_canonical_timestamp
from tests.core.sessions.history_fixtures import seed_history

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

# ``statistics(agent_ids=("main",), *, projects=None, index=None, **options)`` from
# the ``statistics`` fixture: a service over the test's ``manager`` whose index is
# closed after the test.
StatisticsFactory = Callable[..., StatisticsService]


@dataclass(frozen=True)
class _FakeAgent:
    id: str


class _FakeAgents:
    """Minimal :class:`AgentDirectory` stand-in for the scan."""

    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_FakeAgent(agent_id) for agent_id in agent_ids]

    def list_with_builtins(self) -> list[_FakeAgent]:
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


def _write_session(
    manager: ChatSessionManager,
    agent_id: str,
    messages: list[ChatMessage],
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> str:
    session = manager.create(agent_id, session_id=session_id, project_id=project_id)
    seed_history(session, messages)
    return session.id


def _admit(
    manager: ChatSessionManager,
    address: SessionAddress,
    run_id: str,
    kind: str = "user",
    *,
    at: datetime = BASE,
) -> None:
    """Admit a Run of ``kind`` started at ``at``; ``seed_history`` then writes and ends it."""
    manager._store.admit_run(
        address,
        SessionRunAdmission(
            run_id=run_id, run_kind=kind, started_at=format_canonical_timestamp(at)
        ),
    )


async def _call(
    ledger: UsageRecorder,
    usage: dict[str, Any] | None,
    *,
    kind: str = "chat",
    model: str = "chat/m",
    address: SessionAddress | None = None,
    run_id: str | None = None,
    owner_name: str | None = None,
    status: str = "completed",
    at: datetime | None = None,
) -> str:
    """Record one finished ledger request, started at ``at`` (default: the ledger clock)."""
    started = (
        nullcontext()
        if at is None
        else patch(
            "core.usage.usage.utc_now_timestamp", return_value=format_canonical_timestamp(at)
        )
    )
    with started:
        call = await ledger.start(
            model=model,
            kind=kind,
            agent_id=None if address is None else address.agent_id,
            project_id=None if address is None else address.project_id,
            session_id=None if address is None else address.session_id,
            run_id=run_id,
            owner_name=owner_name,
            group_id=None if owner_name is None else "group",
        )
    await ledger.finish(call, usage, status=status)
    return call


def _index_path(data_dir: Path) -> Path:
    return data_dir / "statistics" / "session-statistics.sqlite"


def _record_canonical_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, SessionReadCursor | None]]:
    """Record every canonical Session history read as ``(session_id, cursor)``."""
    reads: list[tuple[str, SessionReadCursor | None]] = []
    load_since = ChatSession.load_since

    def recording_load_since(
        self: ChatSession, cursor: SessionReadCursor | None = None
    ) -> SessionReadBatch | None:
        reads.append((self.id, cursor))
        return load_since(self, cursor)

    monkeypatch.setattr(ChatSession, "load_since", recording_load_since)
    return reads
