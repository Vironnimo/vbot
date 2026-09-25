"""Tests for session methods fork."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.prompts.pinned_context import PINNED_SKILL_CATALOG_SLOT
from core.sessions import FORK_SOURCE_META_KEY, SeenSkillsUpdate, SessionAddress
from server.rpc.errors import RpcError
from server.rpc.methods import dispatch_rpc
from server.rpc.session_methods import (
    _fork_session,
)
from tests.server.rpc.agent_methods_test_support import (
    _make_state,
    _sessions_resource_events,
)
from tests.server.rpc_test_support import StubAdapter, make_state


async def _rpc_fork(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    response = await dispatch_rpc(state, {"method": "session.fork", "params": params})
    assert response["ok"] is True, response
    result: dict[str, Any] = response["result"]
    return result


@pytest.mark.asyncio
async def test_fork_same_agent_returns_new_id_with_provenance() -> None:
    state, resolver, sessions = _make_state()

    result = await _fork_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result["session"]["id"] == "fork-1"
    assert result["session"]["agent_id"] == "builder"
    assert result["session"]["fork_source"]["session_id"] == "s1"
    # Without a target the fork stays in the source's scope; Sessions owns which
    # bindings stay behind, so the RPC passes no label or policy of its own.
    assert sessions.forked[0]["target_agent_id"] is None
    assert sessions.forked[0]["target_project_id"] is None
    assert sessions.forked[0]["title"] is None
    assert sessions.forked[0]["run_kind"] is None
    assert resolver.resolved == [(None, "builder")]


@pytest.mark.asyncio
async def test_fork_strips_channel_and_subagent_bindings_but_keeps_title(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="s1")
    source.append(ChatMessage.user("hello"))
    sessions.mutate_metadata(
        source.address,
        lambda metadata: metadata.update(
            {
                "title": "Keep",
                "source_channel_id": "chan",
                "platform": "telegram",
                "platform_conv_id": "conversation",
                "is_subagent_session": True,
            }
        ),
    )

    result = await _rpc_fork(state, {"agent_id": "coder", "session_id": "s1"})

    fork_address = SessionAddress(None, "coder", result["session"]["id"])
    metadata = sessions.get_metadata(fork_address)
    assert metadata["title"] == "Keep"
    assert "source_channel_id" not in metadata
    assert "platform" not in metadata
    assert "platform_conv_id" not in metadata
    assert "is_subagent_session" not in metadata
    assert result["session"]["fork_source"] == metadata[FORK_SOURCE_META_KEY]
    assert result["session"]["fork_source"]["session_id"] == "s1"
    assert [message.content for message in sessions.get(fork_address).load_active()] == ["hello"]


@pytest.mark.asyncio
async def test_fork_of_a_project_session_stays_in_its_project(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    resolved: list[tuple[str | None, str]] = []

    async def resolve_agent_async(project_id: str | None, agent_id: str) -> None:
        resolved.append((project_id, agent_id))

    state.runtime.agent_resolver = SimpleNamespace(resolve_agent_async=resolve_agent_async)
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="s1", project_id="proj")
    source.append(ChatMessage.user("hello"))

    result = await _rpc_fork(state, {"agent_id": "coder@proj", "session_id": "s1"})

    fork_address = SessionAddress("proj", "coder", result["session"]["id"])
    assert [message.content for message in sessions.get(fork_address).load_active()] == ["hello"]
    assert result["session"]["fork_source"]["session_id"] == "s1"
    assert resolved == [("proj", "coder")]
    # Nothing landed outside the Project.
    assert sessions.list_addresses(None, agent_id="coder") == []


@pytest.mark.asyncio
async def test_fork_to_other_agent_strips_catalog_and_lands_under_target() -> None:
    state, resolver, sessions = _make_state()

    result = await _fork_session(
        state,
        {"agent_id": "builder", "session_id": "s1", "target_agent_id": "reviewer"},
    )

    assert result["session"]["agent_id"] == "reviewer"
    assert sessions.forked[0]["target_agent_id"] == "reviewer"
    assert sessions.forked[0]["target_project_id"] is None
    # Both endpoints are resolved before any file work.
    assert resolver.resolved == [(None, "builder"), (None, "reviewer")]
    # The refresh event names the fork under the target agent.
    assert _sessions_resource_events(state) == [
        {
            "kind": "sessions",
            "scope": {"project_id": None, "agent_id": "reviewer", "session_id": "fork-1"},
        }
    ]


@pytest.mark.asyncio
async def test_fork_to_other_agent_leaves_the_pinned_skill_catalog_behind(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("reviewer")
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="s1")
    source.append(ChatMessage.user("hello"))
    catalog = {"skills": ["deploy"]}
    sessions.ensure_prompt_pin(source.address, PINNED_SKILL_CATALOG_SLOT, catalog, lambda _: True)
    sessions.record_seen_skills(source.address, SeenSkillsUpdate(("deploy",)))

    same_agent = await _rpc_fork(state, {"agent_id": "coder", "session_id": "s1"})
    other_agent = await _rpc_fork(
        state, {"agent_id": "coder", "session_id": "s1", "target_agent_id": "reviewer"}
    )

    # A fork on the same Agent keeps the prompt-cache-warm catalog; a fork into
    # another Agent leaves it (and the seen Skills) behind so the target pins its own.
    kept = SessionAddress(None, "coder", same_agent["session"]["id"])
    assert sessions.prompt_pin(kept, PINNED_SKILL_CATALOG_SLOT) == catalog
    assert sessions.seen_skills(kept) == frozenset({"deploy"})
    assert other_agent["session"]["agent_id"] == "reviewer"
    moved = SessionAddress(None, "reviewer", other_agent["session"]["id"])
    assert sessions.prompt_pin(moved, PINNED_SKILL_CATALOG_SLOT) is None
    assert sessions.seen_skills(moved) is None
    assert [message.content for message in sessions.get(moved).load_active()] == ["hello"]


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
