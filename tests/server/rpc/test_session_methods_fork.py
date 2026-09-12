"""Tests for session methods fork."""

from __future__ import annotations

import pytest

from core.chat import ChatSessionError
from core.sessions import (
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS,
    SessionAddress,
)
from server.rpc.errors import RpcError
from server.rpc.session_methods import (
    _fork_session,
)
from tests.server.rpc.agent_methods_test_support import (
    _make_state,
    _sessions_resource_events,
)


@pytest.mark.asyncio
async def test_fork_same_agent_returns_new_id_with_provenance() -> None:
    state, resolver, sessions = _make_state()
    sessions.source_metadata = {"title": "Keep"}

    result = await _fork_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result["session"]["id"] == "fork-1"
    assert result["session"]["agent_id"] == "builder"
    assert result["session"]["fork_source"]["session_id"] == "s1"
    # Same agent, so only the always-strip policy applies (catalog keys kept).
    assert sessions.forked[0]["strip_meta_keys"] == SESSION_FORK_ALWAYS_STRIP_META_KEYS
    assert resolver.resolved == [(None, "builder")]


@pytest.mark.asyncio
async def test_fork_strips_channel_and_subagent_bindings_but_keeps_title() -> None:
    state, _resolver, sessions = _make_state()
    sessions.source_metadata = {
        "title": "Keep",
        "source_channel_id": "chan",
        "platform": "telegram",
        "is_subagent_session": True,
    }

    result = await _fork_session(state, {"agent_id": "builder", "session_id": "s1"})

    metadata = sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="builder", session_id=result["session"]["id"])
    )
    assert metadata["title"] == "Keep"
    assert "source_channel_id" not in metadata
    assert "platform" not in metadata
    assert "is_subagent_session" not in metadata


@pytest.mark.asyncio
async def test_fork_to_other_agent_strips_catalog_and_lands_under_target() -> None:
    state, resolver, sessions = _make_state()

    result = await _fork_session(
        state,
        {"agent_id": "builder", "session_id": "s1", "target_agent_id": "reviewer"},
    )

    assert result["session"]["agent_id"] == "reviewer"
    assert sessions.forked[0]["target_agent_id"] == "reviewer"
    # A cross-agent fork additionally strips the pinned-catalog keys.
    assert (
        sessions.forked[0]["strip_meta_keys"]
        == SESSION_FORK_ALWAYS_STRIP_META_KEYS | SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS
    )
    # Both endpoints are resolved before any file work.
    assert resolver.resolved == [(None, "builder"), (None, "reviewer")]
    # The refresh event is scoped to the target agent.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "reviewer"}}
    ]


@pytest.mark.asyncio
async def test_fork_unknown_session_is_domain_error() -> None:
    state, _resolver, sessions = _make_state()
    sessions.missing = {"gone"}

    with pytest.raises(RpcError) as exc_info:
        await _fork_session(state, {"agent_id": "builder", "session_id": "gone"})

    assert exc_info.value.code == "domain_error"


@pytest.mark.asyncio
async def test_fork_owner_managed_session_is_domain_error_without_explicit_export() -> None:
    state, _resolver, sessions = _make_state()
    sessions.fork_error = ChatSessionError(
        "This Session is managed by an Extension. Use that Extension to resume it."
    )

    with pytest.raises(RpcError) as exc_info:
        await _fork_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert exc_info.value.code == "domain_error"
    assert (
        exc_info.value.message
        == "This Session is managed by an Extension. Use that Extension to resume it."
    )
    assert _sessions_resource_events(state) == []
    assert sessions.forked[0]["target_agent_id"] is None


@pytest.mark.asyncio
async def test_fork_rejects_unsupported_field() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _fork_session(state, {"agent_id": "builder", "session_id": "s1", "bogus": 1})

    assert exc_info.value.code == "invalid_request"
    assert sessions.forked == []
