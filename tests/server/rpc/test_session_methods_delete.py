"""Tests for session methods delete."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.chat import ChatSessionError
from core.runs import RunAdmissionBlockedError
from core.sessions import (
    SessionAddress,
)
from core.tools.terminal_manager import TerminalOwner
from server.rpc.errors import RpcError
from server.rpc.session_methods import (
    _delete_session,
)
from tests.server.rpc.agent_methods_test_support import (
    _make_state,
    _sessions_resource_events,
)


@pytest.mark.asyncio
async def test_delete_bare_agent_archives_and_lands_on_reaimed_current() -> None:
    state, resolver, sessions = _make_state()

    result = await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result == {"agent_id": "builder", "session_id": "s1", "next_session_id": "landing"}
    assert resolver.resolved == [(None, "builder")]
    # Archived (not hard-deleted) under the identity scope.
    assert sessions.archived == [("builder", "s1", None)]
    assert state.runtime.terminal_manager.closed_scopes == [TerminalOwner(None, "builder", "s1")]
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

    async with state.chat_runs.session_admission_guard(
        SessionAddress(project_id=None, agent_id="builder", session_id="s1")
    ):
        with pytest.raises(RpcError) as exc_info:
            await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert exc_info.value.code == "session_busy"
    # The guard fires before any file work — nothing archived, nothing re-aimed.
    assert sessions.archived == []
    assert state._resets == []


@pytest.mark.asyncio
async def test_delete_session_referenced_by_bootstrap_is_rejected() -> None:
    state, _resolver, sessions = _make_state()
    state.runtime.bootstrap_service = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="boot-1",
                agent_id="builder",
                project_id=None,
                session_id="s1",
                status="active",
            )
        ]
    )

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert exc_info.value.code == "session_busy"
    assert "bootstrap:boot-1" in exc_info.value.message
    assert sessions.archived == []


@pytest.mark.asyncio
async def test_delete_guard_rejects_run_while_archive_is_waiting() -> None:
    state, _resolver, sessions = _make_state()
    sessions.archive_started = asyncio.Event()
    sessions.archive_release = asyncio.Event()

    delete_task = asyncio.create_task(
        _delete_session(state, {"agent_id": "builder", "session_id": "s1"})
    )
    await asyncio.wait_for(sessions.archive_started.wait(), timeout=1)

    with pytest.raises(RunAdmissionBlockedError):
        await state.chat_runs.start(
            SessionAddress(project_id=None, agent_id="builder", session_id="s1"),
            lambda _run: asyncio.sleep(0),
        )
    sessions.archive_release.set()
    result = await asyncio.wait_for(delete_task, timeout=1)
    assert result["session_id"] == "s1"


@pytest.mark.asyncio
async def test_delete_missing_session_is_domain_error() -> None:
    state, _resolver, sessions = _make_state()
    sessions.missing = {"gone"}

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "gone"})

    assert exc_info.value.code == "domain_error"
    assert sessions.archived == []


@pytest.mark.asyncio
async def test_delete_owner_managed_session_is_domain_error() -> None:
    state, _resolver, sessions = _make_state()
    sessions.archive_error = ChatSessionError(
        "This Session is managed by an Extension. Use that Extension to resume it."
    )

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert exc_info.value.code == "domain_error"
    assert (
        exc_info.value.message
        == "This Session is managed by an Extension. Use that Extension to resume it."
    )
    assert _sessions_resource_events(state) == []


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
