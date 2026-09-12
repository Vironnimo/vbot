"""Shared fixtures and fakes for agent methods behavior tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from core.chat import ChatSessionError
from core.runs import ChatRunManager
from core.sessions import (
    FORK_SOURCE_META_KEY,
)
from server.events import ServerEventBus


class _FakeResolver:
    def __init__(self) -> None:
        self.resolved: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        self.resolved.append((project_id, agent_id))
        return SimpleNamespace(id=agent_id)


class _FakeSessions:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.listed: list[tuple[str, str | None]] = []
        self.renamed: list[tuple[str, str, str, str | None]] = []
        self.marked_read: list[tuple[str, str, str, str | None]] = []
        self.mark_read_result: dict[str, Any] = {
            "latest_completion_run_id": None,
            "has_unread_completion": False,
            "unread_run_id": None,
            "unread_run_status": None,
            "unread_run_at": None,
            "marked_read": True,
        }
        self.archived: list[tuple[str, str, str | None]] = []
        self.got: list[tuple[str, str, str | None]] = []
        self.forked: list[dict[str, Any]] = []
        # Rows returned by list_with_metadata; default keeps the existing
        # listing tests byte-identical. Delete tests override it.
        self.metadata_rows: list[dict[str, Any]] = [{"id": "s1"}]
        self.activity_rows: list[dict[str, Any]] = [
            {
                "id": "s1",
                "latest_completion_run_id": "run-one",
                "has_unread_completion": True,
                "unread_run_id": "run-one",
                "unread_run_status": "completed",
                "unread_run_at": "2026-07-20T10:00:00+00:00",
            }
        ]
        self.activity_error: Exception | None = None
        self.list_page_calls: list[dict[str, Any]] = []
        # Sidecar the source carries; ``fork`` strips the requested keys off it so
        # fork tests can assert what the fork retains. Fork tests override it.
        self.source_metadata: dict[str, Any] = {}
        # Session ids that ``get``/``fork`` should treat as nonexistent.
        self.missing: set[str] = set()
        # Fork metadata keyed by (agent_id, session_id, project_id) for get_metadata.
        self._fork_metadata: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.saved_metadata: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.archive_started: asyncio.Event | None = None
        self.archive_release: asyncio.Event | None = None
        self.archive_error: Exception | None = None
        self.fork_error: Exception | None = None

    def create(self, agent_id: str, *, session_id: Any = None, project_id: Any = None) -> Any:
        self.created.append(
            {"agent_id": agent_id, "session_id": session_id, "project_id": project_id}
        )
        return SimpleNamespace(id="new-session")

    def get(self, address: Any) -> Any:
        self.got.append((address.agent_id, address.session_id, address.project_id))
        if address.session_id in self.missing:
            raise ChatSessionError(f"session does not exist: {address.session_id}")
        return SimpleNamespace(id=address.session_id)

    async def archive(self, address: Any) -> Any:
        self.archived.append((address.agent_id, address.session_id, address.project_id))
        if self.archive_error is not None:
            raise self.archive_error
        if self.archive_started is not None:
            self.archive_started.set()
        if self.archive_release is not None:
            await self.archive_release.wait()
        return SimpleNamespace(id=address.session_id)

    def list_with_metadata(self, agent_id: str, project_id: str | None = None) -> list[Any]:
        self.listed.append((agent_id, project_id))
        return self.metadata_rows

    def list_summaries_page(
        self,
        scopes: list[tuple[str | None, str]],
        **kwargs: Any,
    ) -> Any:
        self.listed.extend((agent_id, project_id) for project_id, agent_id in scopes)
        self.list_page_calls.append({"scopes": scopes, **kwargs})
        rows: list[dict[str, Any]] = []
        for project_id, agent_id in scopes:
            rows.extend(
                {**row, "project_id": project_id, "agent_id": agent_id}
                for row in self.metadata_rows
            )
        return SimpleNamespace(sessions=tuple(rows), next_cursor=None, total_count=len(rows))

    def list_completion_activity(self, agent_id: str, project_id: str | None = None) -> list[Any]:
        self.listed.append((agent_id, project_id))
        if self.activity_error is not None:
            raise self.activity_error
        return self.activity_rows

    def mark_terminal_run_read(self, address: Any, run_id: str) -> dict[str, Any]:
        self.marked_read.append((address.agent_id, address.session_id, run_id, address.project_id))
        return dict(self.mark_read_result)

    async def fork(
        self,
        source: Any,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: Any = frozenset(),
    ) -> Any:
        source_agent_id = source.agent_id
        session_id = source.session_id
        source_project_id = source.project_id
        self.forked.append(
            {
                "source_agent_id": source_agent_id,
                "session_id": session_id,
                "target_agent_id": target_agent_id,
                "source_project_id": source_project_id,
                "target_project_id": target_project_id,
                "strip_meta_keys": frozenset(strip_meta_keys),
            }
        )
        if session_id in self.missing:
            raise ChatSessionError(f"session does not exist: {session_id}")
        if self.fork_error is not None:
            raise self.fork_error
        retained = {
            key: value for key, value in self.source_metadata.items() if key not in strip_meta_keys
        }
        retained[FORK_SOURCE_META_KEY] = {
            "agent_id": source_agent_id,
            "session_id": session_id,
            "project_id": source_project_id,
            "forked_at": "2026-07-04T00:00:00+00:00",
            "message_count": 2,
        }
        destination_agent_id = target_agent_id or source_agent_id
        self._fork_metadata[(destination_agent_id, "fork-1", target_project_id)] = retained
        return SimpleNamespace(id="fork-1")

    def get_metadata(self, address: Any) -> dict[str, Any]:
        key = (address.agent_id, address.session_id, address.project_id)
        return self.saved_metadata.get(key, self._fork_metadata.get(key, {}))

    def set_metadata(self, address: Any, metadata: dict[str, Any]) -> None:
        self.saved_metadata[(address.agent_id, address.session_id, address.project_id)] = dict(
            metadata
        )

    def set_title(self, address: Any, title: str) -> str | None:
        self.renamed.append((address.agent_id, address.session_id, title, address.project_id))
        # Mirror the real primitive's blank→None clear so the handler response is realistic.
        normalized = " ".join(title.split())
        return normalized or None


class _FakeTerminalManager:
    def __init__(self) -> None:
        self.closed_scopes: list[Any] = []
        self.closed_agents: list[tuple[str, str | None]] = []

    async def close_scope(self, owner: Any) -> None:
        self.closed_scopes.append(owner)

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> None:
        self.closed_agents.append((agent_id, project_id))


def _make_state() -> tuple[SimpleNamespace, _FakeResolver, _FakeSessions]:
    resolver = _FakeResolver()
    sessions = _FakeSessions()
    updates: list[dict[str, Any]] = []
    resets: list[tuple[str, str]] = []
    recall_removals: list[tuple[str, str, str | None]] = []
    # The identity current-session pointer agents.get reports; defaults to a
    # session other than the one tests delete, so a delete is not "the current"
    # unless a test opts in by setting it.
    agent_current = {"current_session_id": "other"}

    def _reset_current(agent_id: str, session_id: str) -> Any:
        resets.append((agent_id, session_id))
        return SimpleNamespace(current_session_id="landing")

    async def _remove_session_from_recall(
        agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        recall_removals.append((agent_id, session_id, project_id))

    runtime = SimpleNamespace(
        agent_resolver=resolver,
        chat_sessions=sessions,
        terminal_manager=_FakeTerminalManager(),
        agents=SimpleNamespace(
            update=lambda agent_id, **k: updates.append({agent_id: k}),
            reset_current_after_session_removed=_reset_current,
            get=lambda agent_id: SimpleNamespace(
                current_session_id=agent_current["current_session_id"]
            ),
        ),
        remove_session_from_recall=_remove_session_from_recall,
        storage=SimpleNamespace(
            load_compaction_settings=lambda: {
                "enabled": True,
                "trigger": {"type": "context_ratio", "threshold": 0.8},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 15_000,
                    "summary_model": None,
                },
            }
        ),
    )
    state = SimpleNamespace(
        runtime=runtime,
        event_bus=ServerEventBus(),
        # _state_chat_runs reads state.chat_runs directly (not under runtime).
        chat_runs=ChatRunManager(),
    )
    state._updates = updates  # type: ignore[attr-defined]
    state._resets = resets  # type: ignore[attr-defined]
    state._recall_removals = recall_removals  # type: ignore[attr-defined]
    state._agent_current = agent_current  # type: ignore[attr-defined]
    return state, resolver, sessions


def _sessions_resource_events(state: SimpleNamespace) -> list[dict[str, Any]]:
    return [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ]
