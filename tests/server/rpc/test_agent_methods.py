"""Tests for project-aware addressing on the session RPC handlers.

Coverage (AAA):
- ``session.create`` with a bare agent id creates an identity session and may set
  ``current_session_id`` (byte-identical to before),
- ``session.create`` with ``agent@projekt`` validates through the resolver, creates
  the session under the project anchor, and does NOT touch identity
  ``current_session_id``,
- ``session.create`` with a malformed address is ``invalid_request``,
- ``session.list`` threads the parsed ``project_id`` into the session listing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatSessionError
from server.events import ServerEventBus
from server.rpc.agent_methods import (
    _create_session,
    _delete_session,
    _list_sessions,
    _rename_session,
)
from server.rpc.errors import RpcError


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
        self.archived: list[tuple[str, str, str | None]] = []
        self.got: list[tuple[str, str, str | None]] = []
        # Rows returned by list_with_metadata; default keeps the existing
        # listing tests byte-identical. Delete tests override it.
        self.metadata_rows: list[dict[str, Any]] = [{"id": "s1"}]
        # Session ids that ``get`` should treat as nonexistent.
        self.missing: set[str] = set()

    def create(self, agent_id: str, *, session_id: Any = None, project_id: Any = None) -> Any:
        self.created.append(
            {"agent_id": agent_id, "session_id": session_id, "project_id": project_id}
        )
        return SimpleNamespace(id="new-session")

    def get(self, agent_id: str, session_id: str, project_id: str | None = None) -> Any:
        self.got.append((agent_id, session_id, project_id))
        if session_id in self.missing:
            raise ChatSessionError(f"session does not exist: {session_id}")
        return SimpleNamespace(id=session_id)

    async def archive(self, agent_id: str, session_id: str, project_id: str | None = None) -> Any:
        self.archived.append((agent_id, session_id, project_id))
        return SimpleNamespace(id=session_id)

    def list_with_metadata(self, agent_id: str, project_id: str | None = None) -> list[Any]:
        self.listed.append((agent_id, project_id))
        return self.metadata_rows

    def set_title(
        self, agent_id: str, session_id: str, title: str, project_id: str | None = None
    ) -> str | None:
        self.renamed.append((agent_id, session_id, title, project_id))
        # Mirror the real primitive's blank→None clear so the handler response is realistic.
        normalized = " ".join(title.split())
        return normalized or None


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

    runtime = SimpleNamespace(
        agent_resolver=resolver,
        chat_sessions=sessions,
        agents=SimpleNamespace(
            update=lambda agent_id, **k: updates.append({agent_id: k}),
            reset_current_after_session_removed=_reset_current,
            get=lambda agent_id: SimpleNamespace(
                current_session_id=agent_current["current_session_id"]
            ),
        ),
        remove_session_from_recall=(
            lambda agent_id, session_id, project_id=None: recall_removals.append(
                (agent_id, session_id, project_id)
            )
        ),
    )
    state = SimpleNamespace(
        runtime=runtime,
        event_bus=ServerEventBus(),
        # _state_chat_runs reads state.chat_runs directly (not under runtime).
        chat_runs=SimpleNamespace(has_activity_for_session=lambda agent_id, session_id: False),
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


def test_create_bare_agent_creates_identity_session() -> None:
    state, resolver, sessions = _make_state()

    result = _create_session(state, {"agent_id": "builder", "make_current": True})

    assert result == {"agent_id": "builder", "session_id": "new-session"}
    assert resolver.resolved == [(None, "builder")]
    assert sessions.created[0]["project_id"] is None
    # Identity make-current writes the agent's current_session_id.
    assert state._updates == [{"builder": {"current_session_id": "new-session"}}]


def test_create_qualified_agent_creates_project_session() -> None:
    state, resolver, sessions = _make_state()

    result = _create_session(state, {"agent_id": "builder@vbot", "make_current": True})

    assert result == {"agent_id": "builder", "session_id": "new-session"}
    assert resolver.resolved == [("vbot", "builder")]
    assert sessions.created[0]["project_id"] == "vbot"
    # A project config agent has no identity current-session pointer to write.
    assert state._updates == []


def test_create_invalid_address_is_invalid_request() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        _create_session(state, {"agent_id": "builder@bad project"})

    assert exc_info.value.code == "invalid_request"
    assert sessions.created == []


def test_list_qualified_agent_scopes_to_project() -> None:
    state, _resolver, sessions = _make_state()

    result = _list_sessions(state, {"agent_id": "builder@vbot"})

    assert result == {"sessions": [{"id": "s1"}]}
    assert sessions.listed == [("builder", "vbot")]


def test_list_bare_agent_is_identity() -> None:
    state, _resolver, sessions = _make_state()

    _list_sessions(state, {"agent_id": "builder"})

    assert sessions.listed == [("builder", None)]


def test_create_session_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    _create_session(state, {"agent_id": "builder", "make_current": True})

    # The single sessions emit point: other windows refresh this agent's session
    # list/marking. Scoped to the agent so windows on a different agent ignore it.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


def test_create_session_scope_uses_bare_agent_id_for_project_address() -> None:
    state, _resolver, _sessions = _make_state()

    _create_session(state, {"agent_id": "builder@vbot"})

    # The scope carries the bare agent id (the project rides separately), matching
    # how the queue/session channels are keyed on the client.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


def test_rename_bare_agent_sets_title() -> None:
    state, _resolver, sessions = _make_state()

    result = _rename_session(
        state, {"agent_id": "builder", "session_id": "s1", "title": "Release planning"}
    )

    assert result == {"agent_id": "builder", "session_id": "s1", "title": "Release planning"}
    assert sessions.renamed == [("builder", "s1", "Release planning", None)]


def test_rename_qualified_agent_scopes_to_project() -> None:
    state, _resolver, sessions = _make_state()

    result = _rename_session(
        state, {"agent_id": "builder@vbot", "session_id": "s1", "title": "Release planning"}
    )

    assert result["agent_id"] == "builder"
    assert sessions.renamed == [("builder", "s1", "Release planning", "vbot")]


def test_rename_without_title_clears() -> None:
    state, _resolver, sessions = _make_state()

    # An absent title field is the clear signal: the handler passes through "".
    result = _rename_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result == {"agent_id": "builder", "session_id": "s1", "title": None}
    assert sessions.renamed == [("builder", "s1", "", None)]


def test_rename_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    _rename_session(state, {"agent_id": "builder", "session_id": "s1", "title": "Hi"})

    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


def test_rename_rejects_unsupported_field() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        _rename_session(
            state, {"agent_id": "builder", "session_id": "s1", "title": "Hi", "bogus": 1}
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.renamed == []


def test_rename_rejects_non_string_title() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        _rename_session(state, {"agent_id": "builder", "session_id": "s1", "title": 42})

    assert exc_info.value.code == "invalid_request"
    assert sessions.renamed == []


@pytest.mark.asyncio
async def test_delete_bare_agent_archives_and_lands_on_reaimed_current() -> None:
    state, resolver, sessions = _make_state()

    result = await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result == {"agent_id": "builder", "session_id": "s1", "next_session_id": "landing"}
    assert resolver.resolved == [(None, "builder")]
    # Archived (not hard-deleted) under the identity scope.
    assert sessions.archived == [("builder", "s1", None)]
    # Identity pointer re-aimed through the shared seam; the landing is its result.
    assert state._resets == [("builder", "s1")]
    # Dropped from the recall index immediately (#6).
    assert state._recall_removals == [("builder", "s1", None)]


@pytest.mark.asyncio
async def test_delete_qualified_agent_lands_on_most_recent_remaining() -> None:
    state, _resolver, sessions = _make_state()
    sessions.metadata_rows = [
        {"id": "old", "last_active_at": "2026-01-01T00:00:00+00:00"},
        {"id": "recent", "last_active_at": "2026-06-01T00:00:00+00:00"},
    ]

    result = await _delete_session(state, {"agent_id": "builder@vbot", "session_id": "s1"})

    assert result["next_session_id"] == "recent"
    assert sessions.archived == [("builder", "s1", "vbot")]
    # A project config agent has no identity current pointer to re-aim.
    assert state._resets == []
    assert state._recall_removals == [("builder", "s1", "vbot")]


@pytest.mark.asyncio
async def test_delete_project_session_creates_fresh_when_none_remain() -> None:
    state, _resolver, sessions = _make_state()
    sessions.metadata_rows = []

    result = await _delete_session(state, {"agent_id": "builder@vbot", "session_id": "s1"})

    assert result["next_session_id"] == "new-session"
    assert sessions.created[0]["project_id"] == "vbot"


@pytest.mark.asyncio
async def test_delete_busy_session_is_rejected() -> None:
    state, _resolver, sessions = _make_state()
    state.chat_runs.has_activity_for_session = lambda agent_id, session_id: True

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert exc_info.value.code == "session_busy"
    # The guard fires before any file work — nothing archived, nothing re-aimed.
    assert sessions.archived == []
    assert state._resets == []


@pytest.mark.asyncio
async def test_delete_missing_session_is_domain_error() -> None:
    state, _resolver, sessions = _make_state()
    sessions.missing = {"gone"}

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "gone"})

    assert exc_info.value.code == "domain_error"
    assert sessions.archived == []


@pytest.mark.asyncio
async def test_delete_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_delete_current_identity_session_refreshes_agents() -> None:
    state, _resolver, _sessions = _make_state()
    # The deleted session is the identity agent's current one.
    state._agent_current["current_session_id"] = "s1"

    await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    # Re-aiming the current pointer is an agent-config change, so both the session
    # list and agent state refresh; a non-current delete emits only sessions.
    events = _sessions_resource_events(state)
    assert [event["kind"] for event in events] == ["sessions", "agents"]


@pytest.mark.asyncio
async def test_delete_rejects_unsupported_field() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "s1", "bogus": 1})

    assert exc_info.value.code == "invalid_request"
    assert sessions.archived == []
