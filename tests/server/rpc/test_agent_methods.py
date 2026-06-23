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

from server.events import ServerEventBus
from server.rpc.agent_methods import _create_session, _list_sessions, _rename_session
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

    def create(self, agent_id: str, *, session_id: Any = None, project_id: Any = None) -> Any:
        self.created.append(
            {"agent_id": agent_id, "session_id": session_id, "project_id": project_id}
        )
        return SimpleNamespace(id="new-session")

    def list_with_metadata(self, agent_id: str, project_id: str | None = None) -> list[Any]:
        self.listed.append((agent_id, project_id))
        return [{"id": "s1"}]

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
    runtime = SimpleNamespace(
        agent_resolver=resolver,
        chat_sessions=sessions,
        agents=SimpleNamespace(update=lambda agent_id, **k: updates.append({agent_id: k})),
    )
    state = SimpleNamespace(runtime=runtime, event_bus=ServerEventBus())
    state._updates = updates  # type: ignore[attr-defined]
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
