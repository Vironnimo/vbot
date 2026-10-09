"""Identity Agent RPCs: payload, mutable fields, validation, ordering and custom prompts.

Rename and delete live in ``test_agent_methods_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.agents import AgentStore
from core.database import write_bootstrap_marker
from core.models import Capabilities, Model, ReasoningCapabilities
from core.projects.resolver import AgentResolver, ModelConfigurationChecker
from core.projects.store import ProjectStore
from core.prompts import LayoutEntry, load_bundled_default_layout
from core.providers.providers import GLOBAL_CONTEXT_WINDOW_FLOOR
from core.sessions import ChatSessionManager
from core.storage import StorageManager
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)

__all__ = ["_no_models_dev_fetch"]


def _add_model(state: Any, model_id: str, **fields: Any) -> None:
    """Register one extra OpenAI catalog model on this test's state only."""
    fields.setdefault("context_window", 256000)
    fields.setdefault("max_output_tokens", 32000)
    state.runtime.models._models["openai"].append(
        Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            **fields,
        )
    )


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_crud_round_trip(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="current-one")
    state.runtime.agents.update("coder", current_session_id="current-one")

    [listed] = (await rpc_result(state, "agent.list"))["agents"]
    created = await rpc_result(state, "agent.create", id="writer")
    updated = await rpc_result(
        state, "agent.update", id="writer", name="Updated Writer", librarian_enabled=False
    )
    deleted = await rpc_result(state, "agent.delete", id="writer")

    assert listed["current_session_id"] == "current-one"
    # Connections are chosen per Model binding, never stored on the Agent.
    assert "connection" not in listed
    assert "fallback_connection" not in listed
    assert created["id"] == "writer"
    assert created["name"] == "writer"
    assert created["custom_system_prompt_enabled"] is False
    assert created["librarian_enabled"] is True
    assert created["memory_prompt_mode"] == "agent_user"
    assert created["tools"] == {}
    assert created["excluded_skills"] == []
    assert (updated["name"], updated["librarian_enabled"]) == ("Updated Writer", False)
    assert deleted["agent_id"] == "writer"
    # The remaining Agents ride on the response; each change is a bare reload signal.
    assert [agent["id"] for agent in deleted["remaining_agents"]] == ["coder"]
    assert resource_changes(state) == [{"kind": "agents"}] * 3


@pytest.mark.asyncio
async def test_agent_create_returns_resolved_defaults_but_keeps_raw_values(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_settings_sections(
        {"defaults": {"agent": {"model": "openai/gpt-5.2", "temperature": 0.6}}}
    )
    state.runtime.storage.update_settings_sections(
        {"defaults": {"agent": {"thinking_effort": "high"}}}
    )

    created = await rpc_result(
        state,
        "agent.create",
        id="writer",
        name="Writer",
        model="",
        temperature=None,
        thinking_effort=None,
    )
    # The generic channel signals a reload; it carries no Agent data.
    signals = resource_changes(state)
    stored = await rpc_result(state, "agent.get", id="writer")

    assert created["model"] == "openai/gpt-5.2"
    assert created["temperature"] == 0.6
    assert created["thinking_effort"] == "high"
    assert created["context_window"] == 256000
    assert signals == [{"kind": "agents"}]
    # Inherited values stay inherited: the Agent's own configuration is empty.
    assert stored["config"]["model"] == ""
    assert stored["config"]["temperature"] is None
    assert stored["config"]["thinking_effort"] is None


class _CatalogModel:
    connections: tuple[str, ...] = ()

    def allows_connection(self, connection_id: str) -> bool:
        return True


class _CheckerModels:
    def get(self, provider_id: str, model_id: str) -> _CatalogModel:
        if (provider_id, model_id) == ("openai", "gpt-5.2"):
            return _CatalogModel()
        raise KeyError(f"{provider_id}/{model_id}")


class _CheckerProviders:
    def get(self, provider_id: str) -> object:
        if provider_id == "openai":
            return SimpleNamespace(connections=[SimpleNamespace(id="api-key")])
        raise KeyError(provider_id)


class _CheckerCredentials:
    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id == "openai:api-key"

    def is_connection_enabled(self, provider_id: str, connection_id: str | None = None) -> bool:
        return True

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)


class _NoContextWindows:
    def get(self, provider_id: str, model_id: str) -> object:
        raise KeyError(f"{provider_id}/{model_id}")


def _real_agent_state(tmp_path: Path, defaults: JsonObject) -> SimpleNamespace:
    """A real Agent store and resolver sharing one ``defaults.agent`` map."""
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True)
    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        (template_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    sessions = ChatSessionManager(data_dir)
    agents = AgentStore(
        data_dir, template_dir=template_dir, defaults_provider=lambda: defaults, sessions=sessions
    )
    checker = ModelConfigurationChecker(
        _CheckerModels(), _CheckerProviders(), _CheckerCredentials()
    )
    resolver = AgentResolver(agents, ProjectStore(data_dir), checker, lambda: defaults)
    return SimpleNamespace(
        runtime=SimpleNamespace(
            agents=agents,
            agent_resolver=resolver,
            chat_sessions=sessions,
            models=_NoContextWindows(),
            storage=StorageManager(data_dir),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("defaults", "own_model", "source"),
    [
        pytest.param({}, "openai/gpt-5.2", "agent", id="own-value"),
        pytest.param({"model": "openai/gpt-5.2"}, None, "global_default", id="global-default"),
    ],
)
async def test_agent_get_reports_raw_config_and_the_effective_source(
    tmp_path: Path, defaults: JsonObject, own_model: str | None, source: str
) -> None:
    state = _real_agent_state(tmp_path, defaults)
    if own_model is None:
        state.runtime.agents.create("orchestrator", "Orchestrator")
    else:
        state.runtime.agents.create("orchestrator", "Orchestrator", model=own_model)

    result = await rpc_result(state, "agent.get", id="orchestrator")

    assert set(result["config"]) == {
        "model",
        "fallback_models",
        "temperature",
        "top_p",
        "thinking_effort",
        "compaction_policy",
    }
    # ``config`` is the Agent's own value; the top-level value is the resolved one.
    assert result["config"]["model"] == (own_model or "")
    assert result["config"]["fallback_models"] == []
    assert result["model"] == "openai/gpt-5.2"
    assert result["effective"]["model"] == {"value": "openai/gpt-5.2", "source": source}
    assert result["effective"]["temperature"] == {"value": None, "source": None}
    assert result["effective"]["top_p"] == {"value": None, "source": None}


@pytest.mark.asyncio
async def test_the_builtin_librarian_is_hidden_and_only_its_model_settings_change(
    tmp_path: Path,
) -> None:
    state = _real_agent_state(tmp_path, {})
    # Agent mutations take the reference lock and announce the change.
    state.agent_delete_lock = asyncio.Lock()
    state.event_bus = SimpleNamespace(publish=lambda _event, _payload: None)
    state.runtime.agents.create("coder", "Coder")
    state.runtime.agents.ensure_builtin_agents()

    listed = (await rpc_result(state, "agent.list"))["agents"]
    updated = await rpc_result(
        state, "agent.update", id="librarian", thinking_effort="high", temperature=0.3
    )
    refusals = [
        await rpc_error(state, "agent.update", id="librarian", name="Curator"),
        await rpc_error(state, "agent.create", id="librarian"),
    ]

    assert [agent["id"] for agent in listed] == ["coder"]
    assert listed[0]["builtin"] is None
    assert (updated["builtin"], updated["thinking_effort"], updated["temperature"]) == (
        "librarian",
        "high",
        0.3,
    )
    assert updated["tool_access"] == {"mode": "selected", "allowed": ["skill", "skill_manage"]}
    assert [error["code"] for error in refusals] == ["domain_error"] * 2
    assert "built into vBot" in refusals[0]["message"]
    assert "reserved" in refusals[1]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "window"),
    [
        pytest.param("openai/gpt-5.2", 256000, id="catalog-window"),
        pytest.param("openai/gpt-5.2::api-key", 256000, id="connection-suffix"),
        pytest.param("unknown/missing-model", None, id="unknown-model"),
        pytest.param("bare-model-id", None, id="no-provider-prefix"),
        # The window drives the WebUI token badge, so a catalog model without one
        # resolves through the default chain (here the global floor).
        pytest.param("openai/windowless", GLOBAL_CONTEXT_WINDOW_FLOOR, id="catalog-without-window"),
    ],
)
async def test_agent_list_reports_the_effective_context_window(
    tmp_path: Path, model: str, window: int | None
) -> None:
    state = make_state(tmp_path, StubAdapter())
    _add_model(state, "windowless", context_window=None, max_output_tokens=None)
    state.runtime.agents.update("coder", model=model)

    [agent] = (await rpc_result(state, "agent.list"))["agents"]

    assert agent["model"] == model
    assert agent["context_window"] == window


# ---------------------------------------------------------------------------
# Mutable fields and validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "field", "expected"),
    [
        pytest.param({"name": None}, "name", "coder", id="empty-name-restores-id"),
        pytest.param({"temperature": None}, "temperature", None, id="clear-temperature"),
        pytest.param({"memory_prompt_mode": "off"}, "memory_prompt_mode", "off", id="memory-mode"),
        pytest.param(
            {"tool_access": {"mode": "selected", "allowed": ["read"], "denied": ["memory"]}},
            "tool_access",
            {"mode": "selected", "allowed": ["read"], "denied": ["memory"]},
            id="tool-access",
        ),
        pytest.param(
            {"tools": {"bash": {"allowed_env": ["OPENAI_API_KEY", "OPENAI_API_KEY"]}}},
            "tools",
            {"bash": {"allowed_env": ["OPENAI_API_KEY"]}},
            id="bash-env-grants-normalized",
        ),
        pytest.param(
            {"excluded_skills": ["pdf"]}, "excluded_skills", ["pdf"], id="excluded-skills"
        ),
        # "All Skills except ..." is one partial patch of both Skill fields.
        pytest.param(
            {"allowed_skills": ["*"], "excluded_skills": ["pdf", "xlsx"]},
            "excluded_skills",
            ["pdf", "xlsx"],
            id="all-skills-except",
        ),
        pytest.param(
            {"tool_loading": {"on_demand": False, "always_loaded": ["read"]}},
            "tool_loading",
            {"on_demand": False, "always_loaded": ["read"]},
            id="tool-loading",
        ),
        pytest.param({"tool_loading": None}, "tool_loading", None, id="tool-loading-removed"),
    ],
)
async def test_agent_update_applies_a_mutable_field(
    tmp_path: Path, params: JsonObject, field: str, expected: Any
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update(
        "coder", name="Coder", temperature=0.9, tool_loading={"on_demand": True}
    )

    updated = await rpc_result(state, "agent.update", id="coder", **params)
    stored = await rpc_result(state, "agent.get", id="coder")

    assert updated[field] == expected
    assert stored[field] == expected


@pytest.mark.asyncio
async def test_workspace_is_set_by_update_only(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    workspace = tmp_path / "updated-workspace"

    refused = await rpc_error(state, "agent.create", id="writer", workspace="C:/escape")
    updated = await rpc_result(state, "agent.update", id="coder", workspace=str(workspace))

    assert refused["code"] == "invalid_request"
    assert updated["workspace"] == str(workspace.resolve())
    assert state.runtime.agents.get("coder").workspace == str(workspace.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        ("agent.create", {"id": "writer", "allowed_tools": ["read_file"]}, "allowed_tools"),
        ("agent.create", {"id": "writer", "connection": "openai:api-key"}, "connection"),
        ("agent.update", {"id": "coder", "fallback_connection": ""}, "fallback_connection"),
        ("agent.create", {"id": "writer", "name": 5}, "name"),
        ("agent.update", {"id": "coder", "model": 5}, "model"),
        ("agent.update", {"id": "coder", "temperature": "0.7"}, "temperature"),
        ("agent.create", {"id": "writer", "temperature": 2.1}, "temperature"),
        ("agent.update", {"id": "coder", "thinking_effort": "extreme"}, "thinking_effort"),
        ("agent.update", {"id": "coder", "memory_prompt_mode": "sometimes"}, "memory_prompt_mode"),
        (
            "agent.update",
            {
                "id": "coder",
                "tool_access": {"mode": "selected", "allowed": ["read"], "denied": ["read"]},
            },
            "",
        ),
        ("agent.create", {"id": "writer", "allowed_skills": ["debugging", None]}, "allowed_skills"),
        ("agent.create", {"id": "writer", "excluded_skills": "pdf"}, "excluded_skills"),
        ("agent.update", {"id": "coder", "excluded_skills": ["pdf", ""]}, "excluded_skills"),
        ("agent.update", {"id": "coder", "excluded_skills": ["*"]}, "allowed_skills to []"),
        ("agent.update", {"id": "coder", "tools": "worker"}, "tools"),
        (
            "agent.create",
            {"id": "writer", "tools": {"subagent": {"allowed_agents": ["worker", None]}}},
            "allowed_agents",
        ),
        ("agent.create", {"id": "writer", "tools": {"bash": {"allowed_env": ["bad-key"]}}}, ""),
        (
            "agent.update",
            {"id": "coder", "custom_system_prompt_enabled": "yes"},
            "custom_system_prompt_enabled",
        ),
        ("agent.update", {"id": "coder", "librarian_enabled": None}, "librarian_enabled"),
        ("agent.create", {"id": "writer", "tool_loading": {"on_demand": 1}}, "on_demand"),
        (
            "agent.update",
            {"id": "coder", "tool_loading": {"on_demand": True, "always_loaded": "read"}},
            "tool_loading.always_loaded",
        ),
        ("agent.reorder", {"agent_ids": ["coder", "coder"], "expected_revision": 1}, ""),
    ],
)
async def test_malformed_agent_payloads_are_rejected(
    tmp_path: Path, method: str, params: JsonObject, named: str
) -> None:
    state = make_state(tmp_path, StubAdapter())

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]
    assert [agent.id for agent in state.runtime.agents.list()] == ["coder"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        ("agent.create", {"id": "writer", "model": "openai/gpt-5.4::api-key"}, "api-key"),
        (
            "agent.update",
            {"id": "coder", "fallback_models": ["openai/gpt-5.4::api-key"]},
            "openai/gpt-5.4",
        ),
        (
            "settings.update",
            {"defaults": {"agent": {"model": "openai/gpt-5.4::api-key"}}},
            "defaults.agent.model",
        ),
        (
            "settings.update",
            {
                "compaction": {
                    "enabled": True,
                    "trigger": {"type": "context_ratio", "threshold": 0.8},
                    "strategy": {
                        "type": "summary_tail",
                        "tail_tokens": 15000,
                        "summary_model": "openai/gpt-5.4::api-key",
                    },
                }
            },
            "compaction.summary_model",
        ),
    ],
)
async def test_a_model_pinned_to_a_connection_it_forbids_is_rejected(
    tmp_path: Path, method: str, params: JsonObject, named: str
) -> None:
    state = make_state(tmp_path, StubAdapter())
    _add_model(state, "gpt-5.4", connections=("subscription",))

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]
    assert state.runtime.storage.load_defaults() == {}
    assert state.runtime.agents.get("coder").fallback_models == []


@pytest.mark.asyncio
async def test_a_model_pinned_to_a_connection_it_permits_is_accepted(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    _add_model(state, "gpt-5.4", connections=("subscription",))

    created = await rpc_result(
        state, "agent.create", id="writer", model="openai/gpt-5.4::subscription"
    )

    assert created["model"] == "openai/gpt-5.4::subscription"


@pytest.mark.asyncio
async def test_agent_reorder_persists_the_order_and_rejects_a_stale_revision(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    await rpc_result(state, "agent.create", id="writer")
    revision = (await rpc_result(state, "agent.list"))["order_revision"]

    reordered = await rpc_result(
        state, "agent.reorder", agent_ids=["writer", "coder"], expected_revision=revision
    )
    stale = await rpc_error(
        state, "agent.reorder", agent_ids=["coder", "writer"], expected_revision=revision
    )

    assert [agent["id"] for agent in reordered["agents"]] == ["writer", "coder"]
    assert reordered["order_revision"] == revision + 1
    assert stale["code"] == "agent_order_conflict"
    assert [agent.id for agent in state.runtime.agents.list()] == ["writer", "coder"]


# ---------------------------------------------------------------------------
# Custom system prompt
# ---------------------------------------------------------------------------


def _write_prompt_copy(prompts_dir: Path, content: str) -> None:
    """Place a hand-edited ``runtime.md`` copy, as a user does in the data directory."""

    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "runtime.md").write_text(content, encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("default_layout_saved", [True, False], ids=["saved", "bundled"])
async def test_enabling_a_custom_prompt_seeds_the_agent_from_the_effective_defaults(
    tmp_path: Path, default_layout_saved: bool
) -> None:
    state = make_state(tmp_path, StubAdapter())
    storage = state.runtime.storage
    _write_prompt_copy(storage.prompts_dir, "custom default runtime")
    default_layout = [
        LayoutEntry(id="core:intro", enabled=True, source="core"),
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
    ]
    if default_layout_saved:
        storage.write_block_layout(None, default_layout)

    updated = await rpc_result(state, "agent.update", id="coder", custom_system_prompt_enabled=True)

    assert updated["custom_system_prompt_enabled"] is True
    assert storage.read_agent_prompt_fragment("coder", "runtime.md") == "custom default runtime"
    assert (storage.agent_prompts_dir("coder") / "layout.json").exists()
    assert storage.read_block_layout("coder") == (
        default_layout if default_layout_saved else load_bundled_default_layout()
    )


@pytest.mark.asyncio
async def test_reenabling_a_custom_prompt_preserves_the_agent_customizations(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    storage = state.runtime.storage
    customized = [LayoutEntry(id="user:house-rules", enabled=True, source="user")]
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)
    _write_prompt_copy(storage.agent_prompts_dir("coder"), "agent custom")
    storage.write_block_layout("coder", customized)
    state.runtime.agents.update("coder", custom_system_prompt_enabled=False)
    _write_prompt_copy(storage.prompts_dir, "custom default runtime")

    await rpc_result(state, "agent.update", id="coder", custom_system_prompt_enabled=True)

    assert storage.read_agent_prompt_fragment("coder", "runtime.md") == "agent custom"
    assert storage.read_block_layout("coder") == customized
