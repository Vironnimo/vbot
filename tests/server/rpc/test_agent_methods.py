"""Tests for agent methods."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.agents import AgentStore
from core.chat import ChatSessionError
from core.projects.resolver import AgentResolver
from core.projects.store import ProjectStore
from core.sessions import (
    SessionAddress,
)
from server.rpc.agent_methods import (
    _get_agent,
)
from server.rpc.errors import RpcError
from server.rpc.session_methods import (
    _create_session,
    _list_session_activity,
    _list_sessions,
    _mark_session_read,
    _rename_session,
    _set_session_compaction_policy,
)
from tests.server.rpc.agent_methods_test_support import (
    _make_state,
    _sessions_resource_events,
)


@pytest.mark.asyncio
async def test_create_bare_agent_creates_identity_session() -> None:
    state, resolver, sessions = _make_state()

    result = await _create_session(state, {"agent_id": "builder", "make_current": True})

    assert result == {"agent_id": "builder", "session_id": "new-session"}
    assert resolver.resolved == [(None, "builder")]
    assert sessions.created[0]["project_id"] is None
    # Identity make-current writes the agent's current_session_id.
    assert state._updates == [{"builder": {"current_session_id": "new-session"}}]


@pytest.mark.asyncio
async def test_create_qualified_agent_creates_project_session() -> None:
    state, resolver, sessions = _make_state()

    result = await _create_session(state, {"agent_id": "builder@vbot", "make_current": True})

    assert result == {"agent_id": "builder", "session_id": "new-session"}
    assert resolver.resolved == [("vbot", "builder")]
    assert sessions.created[0]["project_id"] == "vbot"
    # A project config agent has no identity current-session pointer to write.
    assert state._updates == []


@pytest.mark.asyncio
async def test_create_invalid_address_is_invalid_request() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _create_session(state, {"agent_id": "builder@bad project"})

    assert exc_info.value.code == "invalid_request"
    assert sessions.created == []


@pytest.mark.asyncio
async def test_list_qualified_agent_scopes_to_project() -> None:
    state, _resolver, sessions = _make_state()

    result = await _list_sessions(state, {"agent_id": "builder@vbot"})

    assert result["sessions"][0]["id"] == "s1"
    assert result["sessions"][0]["compaction_policy_override"] is None
    assert result["sessions"][0]["compaction_policy_effective"]["enabled"] is True
    assert sessions.listed == [("builder", "vbot")]


@pytest.mark.asyncio
async def test_list_bare_agent_is_identity() -> None:
    state, _resolver, sessions = _make_state()

    await _list_sessions(state, {"agent_id": "builder"})

    assert sessions.listed == [("builder", None)]


@pytest.mark.asyncio
async def test_list_batches_agents_and_passes_bounded_filter_contract() -> None:
    state, resolver, sessions = _make_state()
    sessions.metadata_rows = [
        {
            "id": "s1",
            "created_at": "2026-09-01T10:00:00+00:00",
            "last_active_at": "2026-09-01T10:00:00+00:00",
        }
    ]

    result = await _list_sessions(
        state,
        {
            "agent_ids": ["builder", "reviewer@vbot"],
            "limit": 35,
            "include_subagents": False,
            "include_memory_reflections": False,
            "include_skill_reflections": False,
            "include_cron": False,
            "required_session": {"agent_id": "builder", "session_id": "s1"},
        },
    )

    assert [session["agent_address"] for session in result["sessions"]] == [
        "builder",
        "reviewer@vbot",
    ]
    assert all("agent_id" not in session for session in result["sessions"])
    assert all("project_id" not in session for session in result["sessions"])
    assert result["next_cursor"] is None
    assert result["total_count"] == 2
    assert resolver.resolved == [(None, "builder"), ("vbot", "reviewer")]
    call = sessions.list_page_calls[0]
    assert call["scopes"] == [(None, "builder"), ("vbot", "reviewer")]
    assert call["limit"] == 35
    assert call["filters"].include_subagents is False
    assert call["required_address"] == SessionAddress(None, "builder", "s1")


@pytest.mark.asyncio
async def test_list_rejects_required_session_from_an_unlisted_agent() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _list_sessions(
            state,
            {
                "agent_id": "builder",
                "required_session": {
                    "agent_id": "reviewer@vbot",
                    "session_id": "s1",
                },
            },
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.list_page_calls == []


@pytest.mark.asyncio
async def test_activity_list_batches_identity_and_project_addresses_in_order() -> None:
    state, resolver, sessions = _make_state()

    result = await _list_session_activity(
        state,
        {
            "agent_ids": [
                "builder",
                "reviewer@vbot",
                "builder",
            ]
        },
    )

    assert result == {
        "agents": [
            {
                "agent_id": "builder",
                "project_id": None,
                "sessions": sessions.activity_rows,
            },
            {
                "agent_id": "reviewer",
                "project_id": "vbot",
                "sessions": sessions.activity_rows,
            },
        ]
    }
    assert resolver.resolved == [(None, "builder"), ("vbot", "reviewer")]
    assert sessions.listed == [("builder", None), ("reviewer", "vbot")]


@pytest.mark.asyncio
async def test_activity_list_accepts_an_empty_address_batch() -> None:
    state, resolver, sessions = _make_state()

    result = await _list_session_activity(state, {"agent_ids": []})

    assert result == {"agents": []}
    assert resolver.resolved == []
    assert sessions.listed == []


@pytest.mark.asyncio
async def test_activity_list_rejects_a_malformed_address_before_storage() -> None:
    state, resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _list_session_activity(
            state,
            {"agent_ids": ["builder", "reviewer@bad project"]},
        )

    assert exc_info.value.code == "invalid_request"
    assert resolver.resolved == []
    assert sessions.listed == []


@pytest.mark.asyncio
async def test_activity_list_maps_session_storage_failures() -> None:
    state, _resolver, sessions = _make_state()
    sessions.activity_error = ChatSessionError("activity sidecar unavailable")

    with pytest.raises(RpcError) as exc_info:
        await _list_session_activity(state, {"agent_ids": ["builder"]})

    assert exc_info.value.code == "domain_error"
    assert sessions.listed == [("builder", None)]


@pytest.mark.asyncio
async def test_mark_session_read_acknowledges_exact_project_run() -> None:
    state, resolver, sessions = _make_state()

    result = await _mark_session_read(
        state,
        {"agent_id": "builder@vbot", "session_id": "s1", "run_id": "run-one"},
    )

    assert resolver.resolved == [("vbot", "builder")]
    assert sessions.marked_read == [("builder", "s1", "run-one", "vbot")]
    assert result["agent_id"] == "builder@vbot"
    assert result["marked_read"] is True
    assert _sessions_resource_events(state) == []


@pytest.mark.asyncio
async def test_mark_session_read_stale_ack_does_not_invalidate_sessions() -> None:
    state, _resolver, sessions = _make_state()
    sessions.mark_read_result["marked_read"] = False
    sessions.mark_read_result["has_unread_completion"] = True
    sessions.mark_read_result["latest_completion_run_id"] = "run-newer"
    sessions.mark_read_result["unread_run_id"] = "run-newer"

    result = await _mark_session_read(
        state,
        {"agent_id": "builder", "session_id": "s1", "run_id": "run-old"},
    )

    assert result["unread_run_id"] == "run-newer"
    assert _sessions_resource_events(state) == []


@pytest.mark.asyncio
async def test_session_compaction_policy_override_and_clear() -> None:
    state, _resolver, sessions = _make_state()
    policy = {
        "enabled": True,
        "trigger": {"type": "input_tokens", "tokens": 100_000},
        "strategy": {"type": "continuation"},
    }

    set_result = await _set_session_compaction_policy(
        state,
        {"agent_id": "builder", "session_id": "s1", "policy": policy},
    )
    clear_result = await _set_session_compaction_policy(
        state,
        {"agent_id": "builder", "session_id": "s1", "policy": None},
    )

    assert set_result["override"] == policy
    assert set_result["source"] == "session"
    assert clear_result["override"] is None
    assert clear_result["source"] == "agent_or_global"
    assert sessions.saved_metadata[("builder", "s1", None)] == {}


@pytest.mark.asyncio
async def test_session_compaction_policy_rejects_invalid_shape() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _set_session_compaction_policy(
            state,
            {
                "agent_id": "builder",
                "session_id": "s1",
                "policy": {"enabled": True, "trigger": {"type": "unknown"}},
            },
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.saved_metadata == {}


@pytest.mark.asyncio
async def test_create_session_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    await _create_session(state, {"agent_id": "builder", "make_current": True})

    # The single sessions emit point: other windows refresh this agent's session
    # list/marking. Scoped to the agent so windows on a different agent ignore it.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_create_session_scope_uses_bare_agent_id_for_project_address() -> None:
    state, _resolver, _sessions = _make_state()

    await _create_session(state, {"agent_id": "builder@vbot"})

    # The scope carries the bare agent id (the project rides separately), matching
    # how the queue/session channels are keyed on the client.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_rename_bare_agent_sets_title() -> None:
    state, _resolver, sessions = _make_state()

    result = await _rename_session(
        state, {"agent_id": "builder", "session_id": "s1", "title": "Release planning"}
    )

    assert result == {"agent_id": "builder", "session_id": "s1", "title": "Release planning"}
    assert sessions.renamed == [("builder", "s1", "Release planning", None)]


@pytest.mark.asyncio
async def test_rename_qualified_agent_scopes_to_project() -> None:
    state, _resolver, sessions = _make_state()

    result = await _rename_session(
        state, {"agent_id": "builder@vbot", "session_id": "s1", "title": "Release planning"}
    )

    assert result["agent_id"] == "builder"
    assert sessions.renamed == [("builder", "s1", "Release planning", "vbot")]


@pytest.mark.asyncio
async def test_rename_without_title_clears() -> None:
    state, _resolver, sessions = _make_state()

    # An absent title field is the clear signal: the handler passes through "".
    result = await _rename_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result == {"agent_id": "builder", "session_id": "s1", "title": None}
    assert sessions.renamed == [("builder", "s1", "", None)]


@pytest.mark.asyncio
async def test_rename_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    await _rename_session(state, {"agent_id": "builder", "session_id": "s1", "title": "Hi"})

    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_rename_rejects_unsupported_field() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _rename_session(
            state, {"agent_id": "builder", "session_id": "s1", "title": "Hi", "bogus": 1}
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.renamed == []


@pytest.mark.asyncio
async def test_rename_rejects_non_string_title() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _rename_session(
            state,
            {"agent_id": "builder", "session_id": "s1", "title": 42},
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.renamed == []


# ---------------------------------------------------------------------------
# agent.get payload: config (raw own values) + effective (per-field value+source).
# Wired against a real AgentStore + AgentResolver so get_raw / effective_config
# are exercised end-to-end rather than stubbed.
# ---------------------------------------------------------------------------
class _UnrestrictedCatalogModel:
    """Catalog-model stub with no connection allowlist (every connection allowed)."""

    connections: tuple[str, ...] = ()

    def allows_connection(self, connection_id: str) -> bool:
        return True


class _PayloadCheckerModels:
    """Model existence probe the resolver's checker uses (unrestricted marker)."""

    def get(self, provider_id: str, model_id: str) -> _UnrestrictedCatalogModel:
        if (provider_id, model_id) == ("openai", "gpt-5.2"):
            return _UnrestrictedCatalogModel()
        raise KeyError(f"{provider_id}/{model_id}")


class _PayloadRuntimeModels:
    """The runtime model registry the context-window lookup reads.

    It always raises ``KeyError`` so ``_resolve_context_window`` degrades to
    ``None`` — the payload test does not assert the window, and a bare-object
    return would trip ``.context_window``.
    """

    def get(self, provider_id: str, model_id: str) -> object:
        raise KeyError(f"{provider_id}/{model_id}")


def _agent_payload_state(tmp_path: Path, defaults: dict[str, Any]) -> SimpleNamespace:
    """Build a real-store state for the agent.get payload path.

    ``defaults`` is the ``defaults.agent`` map both the store (for baking) and the
    resolver's global tier read, so the baked top-level keys and the effective
    ``global_default`` source agree.
    """
    from core.projects.resolver import ModelConfigurationChecker

    data_dir = tmp_path / "data"
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True)
    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        (template_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")

    agents = AgentStore(data_dir, template_dir=template_dir, defaults_provider=lambda: defaults)
    projects = ProjectStore(data_dir)
    checker = ModelConfigurationChecker(
        _PayloadCheckerModels(),
        _PayloadProviders(),
        _PayloadCredentials(),
    )
    resolver = AgentResolver(agents, projects, checker, lambda: defaults)
    runtime = SimpleNamespace(
        agents=agents, agent_resolver=resolver, models=_PayloadRuntimeModels()
    )
    return SimpleNamespace(runtime=runtime)


class _PayloadProviders:
    def get(self, provider_id: str) -> object:
        if provider_id == "openai":
            return SimpleNamespace(connections=[SimpleNamespace(id="api-key")])
        raise KeyError(provider_id)


class _PayloadCredentials:
    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id == "openai:api-key"

    def is_connection_enabled(self, provider_id: str, connection_id: str | None = None) -> bool:
        return True

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)


def test_agent_get_reports_config_and_effective_for_own_value(tmp_path: Path) -> None:
    state = _agent_payload_state(tmp_path, defaults={})
    state.runtime.agents.create("orchestrator", "Orchestrator", model="openai/gpt-5.2")

    result = _get_agent(state, {"id": "orchestrator"})

    # config = raw own values (pre-default-bake); shape check.
    assert set(result["config"]) == {
        "model",
        "fallback_models",
        "temperature",
        "thinking_effort",
        "compaction_policy",
    }
    assert result["config"]["model"] == "openai/gpt-5.2"
    assert result["config"]["fallback_models"] == []
    assert result["config"]["temperature"] is None
    # effective = per-field {value, source}; the own model wins as source "agent".
    assert result["effective"]["model"] == {"value": "openai/gpt-5.2", "source": "agent"}
    assert result["effective"]["temperature"] == {"value": None, "source": None}


def test_agent_get_effective_reports_global_default_when_own_empty(tmp_path: Path) -> None:
    # With a global default set, the top-level model is baked while config keeps the
    # raw "", and effective attributes the value to the global_default tier.
    state = _agent_payload_state(tmp_path, defaults={"model": "openai/gpt-5.2"})
    state.runtime.agents.create("orchestrator", "Orchestrator")

    result = _get_agent(state, {"id": "orchestrator"})

    assert result["model"] == "openai/gpt-5.2"  # baked top-level key
    assert result["config"]["model"] == ""  # raw own value
    assert result["effective"]["model"] == {"value": "openai/gpt-5.2", "source": "global_default"}
