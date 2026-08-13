"""Server RPC agent and Session handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.channels import ChannelConfigError
from core.models import Capabilities, Model, ReasoningCapabilities
from core.prompts import LayoutEntry, load_bundled_default_layout
from core.runs import Run
from server.rpc.methods import dispatch_rpc
from server.rpc.payloads import _model_response
from tests.server.rpc_test_support import (
    InstrumentedAgentDeleteLock,
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_session_create_creates_explicit_session(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "session.create", "params": {"agent_id": "coder", "session_id": "session-one"}},
    )

    assert response == {
        "ok": True,
        "result": {"agent_id": "coder", "session_id": "session-one"},
    }
    assert state.runtime.chat_sessions.get("coder", "session-one").id == "session-one"


@pytest.mark.asyncio
async def test_agent_crud_delegates_expose_current_session_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="current-one")
    state.runtime.agents.update("coder", current_session_id="current-one")

    list_response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})
    create_response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {"id": "writer"},
        },
    )
    update_response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "writer", "name": "Updated Writer"}},
    )
    delete_response = await dispatch_rpc(
        state, {"method": "agent.delete", "params": {"id": "writer"}}
    )

    assert list_response["result"]["agents"][0]["current_session_id"] == "current-one"
    assert create_response["result"]["id"] == "writer"
    assert create_response["result"]["name"] == "writer"
    assert create_response["result"]["custom_system_prompt_enabled"] is False
    assert create_response["result"]["memory_prompt_mode"] == "agent_user"
    assert create_response["result"]["tools"] == {}
    assert update_response["result"]["name"] == "Updated Writer"
    assert delete_response["result"]["agent_id"] == "writer"


@pytest.mark.asyncio
async def test_agent_update_empty_name_restores_id_default(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "name": None}},
    )

    assert response["ok"] is True
    assert response["result"]["name"] == "coder"


@pytest.mark.asyncio
async def test_agent_update_accepts_and_normalizes_bash_env_grants(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {
                "id": "coder",
                "tools": {"bash": {"allowed_env": ["OPENAI_API_KEY", "OPENAI_API_KEY"]}},
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["tools"] == {"bash": {"allowed_env": ["OPENAI_API_KEY"]}}


@pytest.mark.asyncio
async def test_agent_update_enabling_custom_prompt_seeds_agent_fragments(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.write_prompt_fragment("runtime.md", "custom default runtime")

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "custom_system_prompt_enabled": True},
        },
    )

    assert response["ok"] is True
    assert response["result"]["custom_system_prompt_enabled"] is True
    assert (
        state.runtime.storage.read_agent_prompt_fragment("coder", "runtime.md")
        == "custom default runtime"
    )


@pytest.mark.asyncio
async def test_agent_update_reenabling_custom_prompt_preserves_agent_fragments(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)
    state.runtime.storage.write_agent_prompt_fragment("coder", "runtime.md", "agent custom")
    state.runtime.agents.update("coder", custom_system_prompt_enabled=False)

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "custom_system_prompt_enabled": True},
        },
    )

    assert response["ok"] is True
    assert state.runtime.storage.read_agent_prompt_fragment("coder", "runtime.md") == "agent custom"


@pytest.mark.asyncio
async def test_agent_update_enabling_custom_prompt_seeds_agent_block_layout(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    effective_default = [
        LayoutEntry(id="core:intro", enabled=True, source="core"),
        LayoutEntry(id="tool:bash", enabled=False, source="tool"),
    ]
    state.runtime.storage.write_block_layout(None, effective_default)

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "custom_system_prompt_enabled": True},
        },
    )

    assert response["ok"] is True
    layout_path = state.runtime.storage.agent_prompts_dir("coder") / "layout.json"
    assert layout_path.exists()
    assert state.runtime.storage.read_block_layout("coder") == effective_default


@pytest.mark.asyncio
async def test_agent_update_enabling_custom_prompt_seeds_bundled_layout_without_default(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "custom_system_prompt_enabled": True},
        },
    )

    assert response["ok"] is True
    # No default-scope layout is saved, so the seed falls back to the bundled default.
    assert state.runtime.storage.read_block_layout("coder") == load_bundled_default_layout()


@pytest.mark.asyncio
async def test_agent_update_reenabling_custom_prompt_preserves_agent_block_layout(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    customized = [LayoutEntry(id="user:house-rules", enabled=True, source="user")]
    state.runtime.storage.write_block_layout("coder", customized)

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "custom_system_prompt_enabled": True},
        },
    )

    assert response["ok"] is True
    # Re-activation must not clobber an already-customized agent layout.
    assert state.runtime.storage.read_block_layout("coder") == customized


@pytest.mark.asyncio
async def test_agent_crud_rejects_connection_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    create_response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {
                "id": "writer",
                "name": "Writer",
                "model": "openai/gpt-5.2",
                "connection": "openai:api-key",
                "fallback_model": "anthropic/claude-sonnet-4-20250219",
                "fallback_connection": "anthropic:api-key",
            },
        },
    )
    update_response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {
                "id": "writer",
                "connection": "openai:oauth",
                "fallback_connection": "",
            },
        },
    )

    assert create_response["ok"] is False
    assert create_response["error"]["code"] == "invalid_request"
    assert "connection" in create_response["error"]["message"]
    assert update_response["ok"] is False
    assert update_response["error"]["code"] == "invalid_request"
    assert "fallback_connection" in update_response["error"]["message"]


def _append_subscription_only_model(state: Any) -> None:
    """Add an openai model restricted to the subscription connection."""
    state.runtime.models._models["openai"].append(
        Model(
            model_id="gpt-5.4",
            name="GPT-5.4",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("subscription",),
        )
    )


@pytest.mark.asyncio
async def test_agent_create_rejects_model_on_forbidden_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    _append_subscription_only_model(state)

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {
                "id": "writer",
                "name": "Writer",
                "model": "openai/gpt-5.4::api-key",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "api-key" in response["error"]["message"]


@pytest.mark.asyncio
async def test_agent_update_rejects_fallback_model_on_forbidden_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    _append_subscription_only_model(state)

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "fallback_model": "openai/gpt-5.4::api-key"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "openai/gpt-5.4" in response["error"]["message"]


@pytest.mark.asyncio
async def test_agent_create_allows_model_on_permitted_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    _append_subscription_only_model(state)

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {
                "id": "writer",
                "name": "Writer",
                "model": "openai/gpt-5.4::subscription",
            },
        },
    )

    assert response["ok"] is True, response
    assert response["result"]["model"] == "openai/gpt-5.4::subscription"


@pytest.mark.asyncio
async def test_settings_update_rejects_default_model_on_forbidden_connection(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    _append_subscription_only_model(state)

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"defaults": {"agent": {"model": "openai/gpt-5.4::api-key"}}},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "defaults.agent.model" in response["error"]["message"]
    assert state.runtime.storage.load_defaults() == {}


@pytest.mark.asyncio
async def test_settings_update_rejects_summary_model_on_forbidden_connection(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    _append_subscription_only_model(state)

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
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
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "compaction.summary_model" in response["error"]["message"]


@pytest.mark.asyncio
async def test_agent_list_response_omits_connection_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert "connection" not in agent
    assert "fallback_connection" not in agent


@pytest.mark.asyncio
async def test_agent_reorder_persists_canonical_list_order(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    await dispatch_rpc(
        state,
        {"method": "agent.create", "params": {"id": "writer", "name": "Writer"}},
    )
    listed = await dispatch_rpc(state, {"method": "agent.list", "params": {}})
    revision = listed["result"]["order_revision"]

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.reorder",
            "params": {
                "agent_ids": ["writer", "coder"],
                "expected_revision": revision,
            },
        },
    )

    assert response["ok"] is True
    assert [agent["id"] for agent in response["result"]["agents"]] == [
        "writer",
        "coder",
    ]
    assert response["result"]["order_revision"] == revision + 1
    assert [agent.id for agent in state.runtime.agents.list()] == ["writer", "coder"]


@pytest.mark.asyncio
async def test_agent_reorder_rejects_stale_revision(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    await dispatch_rpc(state, {"method": "agent.create", "params": {"id": "writer"}})
    listed = await dispatch_rpc(state, {"method": "agent.list", "params": {}})
    stale_revision = listed["result"]["order_revision"]
    await dispatch_rpc(
        state,
        {
            "method": "agent.reorder",
            "params": {
                "agent_ids": ["writer", "coder"],
                "expected_revision": stale_revision,
            },
        },
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.reorder",
            "params": {
                "agent_ids": ["coder", "writer"],
                "expected_revision": stale_revision,
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_order_conflict"
    assert [agent.id for agent in state.runtime.agents.list()] == ["writer", "coder"]


@pytest.mark.asyncio
async def test_agent_reorder_rejects_invalid_payload(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.reorder",
            "params": {"agent_ids": ["coder", "coder"], "expected_revision": 1},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_agent_list_includes_context_window_for_known_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "openai/gpt-5.2"
    assert agent["context_window"] == 256000


@pytest.mark.asyncio
async def test_agent_list_includes_context_window_for_suffixed_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", model="openai/gpt-5.2::api-key")

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "openai/gpt-5.2::api-key"
    assert agent["context_window"] == 256000


@pytest.mark.asyncio
async def test_agent_list_includes_null_context_window_for_unknown_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", model="unknown/missing-model")

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "unknown/missing-model"
    assert agent["context_window"] is None


@pytest.mark.asyncio
async def test_agent_list_includes_null_context_window_for_model_without_provider_prefix(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", model="bare-model-id")

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "bare-model-id"
    assert agent["context_window"] is None


@pytest.mark.asyncio
async def test_agent_list_resolves_window_for_null_window_model(tmp_path: Path) -> None:
    # The agent payload drives the WebUI token badge, so a null-window model
    # resolves through the default chain to a usable window (here the global
    # floor — the openai provider stub carries no context_window default).
    from core.providers.providers import GLOBAL_CONTEXT_WINDOW_FLOOR

    state = make_state(tmp_path, StubAdapter())
    # Register a window-less model on this test's state only (avoid mutating the
    # shared catalog stub that other model.list tests assert against).
    state.runtime.models._models["openai"].append(
        Model(
            model_id="gpt-5.2-thin",
            name="GPT-5.2 Thin",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=None,
            max_output_tokens=None,
        )
    )
    state.runtime.agents.update("coder", model="openai/gpt-5.2-thin")

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["context_window"] == GLOBAL_CONTEXT_WINDOW_FLOOR


@pytest.mark.asyncio
async def test_model_list_carries_null_context_window_verbatim(tmp_path: Path) -> None:
    # model.list is the honest catalog: a null window stays null (the WebUI
    # tolerates it), it is NOT resolved through the default chain.

    model = Model(
        model_id="gpt-5.2-thin",
        name="GPT-5.2 Thin",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=None,
        max_output_tokens=None,
    )

    payload = _model_response("openai", model)

    assert payload["context_window"] is None


@pytest.mark.asyncio
async def test_agent_update_accepts_null_temperature_to_clear_override(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", temperature=0.9)

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "temperature": None}},
    )

    assert response["ok"] is True
    assert response["result"]["temperature"] is None
    assert state.runtime.agents.get("coder").temperature is None


@pytest.mark.asyncio
async def test_agent_update_accepts_memory_prompt_mode(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "memory_prompt_mode": "off"}},
    )

    assert response["ok"] is True
    assert response["result"]["memory_prompt_mode"] == "off"
    assert state.runtime.agents.get("coder").memory_prompt_mode == "off"


@pytest.mark.asyncio
async def test_agent_update_accepts_explicit_tool_access_policy(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {
                "id": "coder",
                "tool_access": {
                    "mode": "selected",
                    "allowed": ["read"],
                    "denied": ["memory"],
                },
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["tool_access"] == {
        "mode": "selected",
        "allowed": ["read"],
        "denied": ["memory"],
    }
    assert (
        state.runtime.agents.get("coder").tool_access.to_dict() == response["result"]["tool_access"]
    )


@pytest.mark.asyncio
async def test_agent_get_reflects_configured_default_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_settings_sections(
        {"defaults": {"agent": {"model": "openai/gpt-4.1-mini"}}}
    )
    state.runtime.agents.update("coder", model="")

    response = await dispatch_rpc(
        state,
        {"method": "agent.get", "params": {"id": "coder"}},
    )

    assert response["ok"] is True
    assert response["result"]["id"] == "coder"
    assert response["result"]["model"] == "openai/gpt-4.1-mini"


@pytest.mark.asyncio
async def test_agent_get_exposes_raw_config_and_effective_provenance(tmp_path: Path) -> None:
    # The agent payload distinguishes an explicit own value from an inherited global
    # default: `config` carries the raw own value ("" model) while `effective` names
    # the winning tier (global_default for the fallen-through model).
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_settings_sections(
        {"defaults": {"agent": {"model": "openai/gpt-4.1-mini"}}}
    )
    state.runtime.agents.update("coder", model="")

    response = await dispatch_rpc(state, {"method": "agent.get", "params": {"id": "coder"}})

    result = response["result"]
    # Raw own value is preserved (empty), the baked top-level value is resolved.
    assert result["config"]["model"] == ""
    assert result["model"] == "openai/gpt-4.1-mini"
    # Effective names the tier that supplied the model.
    assert result["effective"]["model"] == {
        "value": "openai/gpt-4.1-mini",
        "source": "global_default",
    }
    # A field with an explicit own value reports source "agent".
    assert result["effective"]["temperature"]["source"] == "agent"


@pytest.mark.asyncio
async def test_agent_create_returns_resolved_defaults_and_signals_agents_reload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_settings_sections(
        {
            "defaults": {
                "agent": {
                    "model": "openai/gpt-5.2",
                    "temperature": 0.6,
                    "thinking_effort": "high",
                }
            }
        }
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {
                "id": "writer",
                "name": "Writer",
                "model": "",
                "temperature": None,
                "thinking_effort": None,
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["model"] == "openai/gpt-5.2"
    assert response["result"]["temperature"] == 0.6
    assert response["result"]["thinking_effort"] == "high"
    assert response["result"]["context_window"] == 256000

    raw_agent = state.runtime.agents._get_raw("writer")
    assert raw_agent.model == ""
    assert raw_agent.temperature is None
    assert raw_agent.thinking_effort is None

    # Agent create now signals a reload over the generic channel (the payload
    # carries no agent data); the resolved defaults are verified on the response.
    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "resource_changed"
    assert event["payload"] == {"kind": "agents"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("agent.create", {"id": "writer", "name": "Writer", "allowed_tools": "read_file"}),
        (
            "agent.create",
            {"id": "writer", "name": "Writer", "allowed_tools": ["read_file", 1]},
        ),
        ("agent.create", {"id": "writer", "name": "Writer", "allowed_skills": "debugging"}),
        (
            "agent.create",
            {"id": "writer", "name": "Writer", "allowed_skills": ["debugging", None]},
        ),
        ("agent.create", {"id": "writer", "name": "Writer", "tools": "worker"}),
        (
            "agent.create",
            {
                "id": "writer",
                "name": "Writer",
                "tools": {"subagent": {"allowed_agents": ["worker", None]}},
            },
        ),
        (
            "agent.create",
            {
                "id": "writer",
                "name": "Writer",
                "tools": {"bash": {"allowed_env": ["bad-key"]}},
            },
        ),
        ("agent.create", {"id": "writer", "name": "Writer", "temperature": "0.7"}),
        ("agent.create", {"id": "writer", "name": "Writer", "temperature": -0.1}),
        ("agent.create", {"id": "writer", "name": "Writer", "temperature": 2.1}),
        ("agent.create", {"id": "writer", "name": "Writer", "thinking_effort": "extreme"}),
        ("agent.update", {"id": "coder", "allowed_tools": "read_file"}),
        ("agent.update", {"id": "coder", "allowed_tools": ["read_file", 1]}),
        ("agent.update", {"id": "coder", "tool_access": {"mode": "selected"}}),
        (
            "agent.update",
            {
                "id": "coder",
                "tool_access": {
                    "mode": "selected",
                    "allowed": ["read"],
                    "denied": ["read"],
                },
            },
        ),
        ("agent.update", {"id": "coder", "allowed_skills": "debugging"}),
        ("agent.update", {"id": "coder", "allowed_skills": ["debugging", None]}),
        ("agent.update", {"id": "coder", "tools": "worker"}),
        (
            "agent.update",
            {
                "id": "coder",
                "tools": {"subagent": {"allowed_agents": ["worker", None]}},
            },
        ),
        (
            "agent.update",
            {"id": "coder", "tools": {"bash": {"allowed_env": "OPENAI_API_KEY"}}},
        ),
        ("agent.update", {"id": "coder", "temperature": "0.7"}),
        ("agent.update", {"id": "coder", "temperature": -0.1}),
        ("agent.update", {"id": "coder", "temperature": 2.1}),
        ("agent.update", {"id": "coder", "thinking_effort": "extreme"}),
        ("agent.update", {"id": "coder", "memory_prompt_mode": "sometimes"}),
        ("agent.create", {"id": "writer", "name": 5}),
        ("agent.update", {"id": "coder", "name": 5}),
        ("agent.update", {"id": "coder", "model": 5}),
        ("agent.update", {"id": "coder", "custom_system_prompt_enabled": "yes"}),
    ],
)
async def test_agent_rpc_rejects_malformed_mutable_payloads(
    tmp_path: Path,
    method: str,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_agent_create_rpc_rejects_workspace_field(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {"id": "writer", "name": "Writer", "workspace": "C:/escape"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_agent_update_rpc_accepts_workspace_mutation(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    workspace = tmp_path / "updated-workspace"

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "workspace": str(workspace)}},
    )

    assert response["ok"] is True
    assert response["result"]["workspace"] == str(workspace.resolve())
    assert state.runtime.agents.get("coder").workspace == str(workspace.resolve())


@pytest.mark.asyncio
async def test_agent_rename_retargets_live_references_and_publishes_mapping(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update(
        "coder",
        tools={"subagent": {"allowed_agents": ["coder", "coder@vbot"]}},
    )
    state.runtime.agents.create(
        "manager",
        "Manager",
        tools={"subagent": {"allowed_agents": ["coder"]}},
    )
    state.runtime.chat_sessions.create("child", session_id="child-session")
    state.runtime.chat_sessions.set_metadata(
        "child",
        "child-session",
        {
            "subagent_parent": {
                "agent_id": "coder",
                "session_id": "parent-session",
                "run_id": "parent-run",
                "project_id": None,
            },
            "fork_source": {"agent_id": "coder", "session_id": "historical"},
        },
    )
    channels = [SimpleNamespace(id="telegram", agent_id="coder")]
    jobs = [
        SimpleNamespace(id="active", agent_id="coder", project_id=None, status="active"),
        SimpleNamespace(id="history", agent_id="coder", project_id=None, status="completed"),
        SimpleNamespace(id="project", agent_id="coder", project_id="vbot", status="active"),
    ]
    bootstrap_jobs = [
        SimpleNamespace(id="boot-active", agent_id="coder", project_id=None, status="active"),
        SimpleNamespace(id="boot-history", agent_id="coder", project_id=None, status="completed"),
    ]

    def update_channel(channel_id: str, **fields: Any) -> None:
        channel = next(item for item in channels if item.id == channel_id)
        channel.agent_id = fields["agent_id"]

    def update_job(job_id: str, **fields: Any) -> Any:
        job = next(item for item in jobs if item.id == job_id)
        job.agent_id = fields["agent_id"]
        return job

    def update_bootstrap_job(job_id: str, **fields: Any) -> Any:
        job = next(item for item in bootstrap_jobs if item.id == job_id)
        job.agent_id = fields["agent_id"]
        return job

    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: channels,
        update_channel=update_channel,
    )
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: jobs,
        update_job=update_job,
    )
    state.runtime.bootstrap_service = SimpleNamespace(
        list_jobs=lambda: bootstrap_jobs,
        update_job=update_bootstrap_job,
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is True
    assert response["result"]["id"] == "researcher"
    assert response["result"]["rename"] == {
        "old_id": "coder",
        "new_id": "researcher",
        "channels_updated": ["telegram"],
        "cron_jobs_updated": ["active"],
        "bootstrap_jobs_updated": ["boot-active"],
        "agent_policies_updated": ["manager", "researcher"],
        "session_links_updated": 1,
    }
    assert channels[0].agent_id == "researcher"
    assert jobs[0].agent_id == "researcher"
    assert jobs[1].agent_id == "coder"
    assert jobs[2].agent_id == "coder"
    assert bootstrap_jobs[0].agent_id == "researcher"
    assert bootstrap_jobs[1].agent_id == "coder"
    assert state.runtime.agents.get("researcher").tools["subagent"]["allowed_agents"] == [
        "researcher",
        "coder@vbot",
    ]
    assert state.runtime.agents.get("manager").tools["subagent"]["allowed_agents"] == ["researcher"]
    child_metadata = state.runtime.chat_sessions.get_metadata("child", "child-session")
    assert child_metadata["subagent_parent"]["agent_id"] == "researcher"
    assert child_metadata["fork_source"]["agent_id"] == "coder"
    events = [event["payload"] for event in state.event_bus.events]
    assert events == [
        {
            "kind": "agents",
            "scope": {"old_agent_id": "coder", "new_agent_id": "researcher"},
        },
        {
            "kind": "sessions",
            "scope": {"old_agent_id": "coder", "new_agent_id": "researcher"},
        },
        {"kind": "channels"},
        {"kind": "cron"},
    ]


@pytest.mark.asyncio
async def test_agent_rename_rejects_active_run(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    release = asyncio.Event()
    coder = state.runtime.agents.get("coder")

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        agent_id="coder",
        session_id=coder.current_session_id,
        executor=hold_run,
        project_id=None,
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_busy"
    assert state.runtime.agents.get("coder").id == "coder"
    release.set()
    assert await run.wait() == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_agent_id", ["coder", "researcher"])
async def test_agent_rename_rejects_open_subagent_relation(
    tmp_path: Path,
    busy_agent_id: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.subagents = SimpleNamespace(
        batch_tracker=SimpleNamespace(
            references_identity_agent=lambda agent_id: agent_id == busy_agent_id
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_busy"
    assert state.runtime.agents.get("coder").id == "coder"


@pytest.mark.asyncio
async def test_agent_rename_rolls_back_all_changes_when_reference_update_fails(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create(
        "manager",
        "Manager",
        tools={"subagent": {"allowed_agents": ["coder"]}},
    )
    state.runtime.chat_sessions.create("child", session_id="child-session")
    original_metadata = {
        "subagent_parent": {
            "agent_id": "coder",
            "session_id": "parent-session",
            "run_id": "parent-run",
            "project_id": None,
        }
    }
    state.runtime.chat_sessions.set_metadata("child", "child-session", original_metadata)
    channels = [
        SimpleNamespace(id="first", agent_id="coder"),
        SimpleNamespace(id="second", agent_id="coder"),
    ]

    def update_channel(channel_id: str, **fields: Any) -> None:
        if channel_id == "second" and fields["agent_id"] == "researcher":
            raise ChannelConfigError("adapter preflight failed")
        channel = next(item for item in channels if item.id == channel_id)
        channel.agent_id = fields["agent_id"]

    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: channels,
        update_channel=update_channel,
    )

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert state.runtime.agents.get("coder").id == "coder"
    assert all(channel.agent_id == "coder" for channel in channels)
    assert state.runtime.agents.get("manager").tools["subagent"]["allowed_agents"] == ["coder"]
    assert state.runtime.chat_sessions.get_metadata("child", "child-session") == original_metadata
    assert state.event_bus.events == []


@pytest.mark.asyncio
async def test_agent_rename_rejects_existing_destination(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("researcher", "Researcher")

    response = await dispatch_rpc(
        state,
        {"method": "agent.rename", "params": {"id": "coder", "new_id": "researcher"}},
    )

    assert response["ok"] is False
    assert state.runtime.agents.get("coder").id == "coder"
    assert state.runtime.agents.get("researcher").name == "Researcher"


@pytest.mark.asyncio
async def test_agent_delete_rejects_last_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "last_agent"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_active_run(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    release = asyncio.Event()
    coder = state.runtime.agents.get("coder")

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        agent_id="coder", session_id=coder.current_session_id, executor=hold_run, project_id=None
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_busy"
    assert state.runtime.agents.get("coder").id == "coder"

    release.set()
    assert await run.wait() == "done"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_channel_reference(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: [SimpleNamespace(id="tg-coder", agent_id="coder")]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "channel:tg-coder" in response["error"]["message"]
    assert state.runtime.agents.get("coder").id == "coder"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_cron_reference(tmp_path: Path) -> None:
    # A bare cron job (project_id=None) targets the identity agent, so it blocks
    # the identity-agent delete.
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: [SimpleNamespace(id="job-coder", agent_id="coder", project_id=None)]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "cron:job-coder" in response["error"]["message"]
    assert state.runtime.agents.get("coder").id == "coder"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_bootstrap_reference(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.bootstrap_service = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="boot-coder",
                agent_id="coder",
                project_id=None,
                status="active",
            )
        ]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "bootstrap:boot-coder" in response["error"]["message"]


@pytest.mark.asyncio
async def test_agent_delete_ignores_project_qualified_cron_reference(tmp_path: Path) -> None:
    # A project-qualified cron job (project_id set) targets that project's Team
    # agent, not the same-named identity agent, so it must not block the identity
    # delete.
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: [SimpleNamespace(id="job-coder", agent_id="coder", project_id="vbot")]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is True
    assert response["result"]["agent_id"] == "coder"


@pytest.mark.asyncio
async def test_agent_delete_ignores_terminal_cron_history(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="job-coder",
                agent_id="coder",
                project_id=None,
                status="completed",
            )
        ]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is True


@pytest.mark.asyncio
async def test_agent_delete_serializes_minimum_one_check_and_delete(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    agent_delete_lock = InstrumentedAgentDeleteLock()
    state.agent_delete_lock = agent_delete_lock

    coder_delete, writer_delete = await asyncio.gather(
        dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}}),
        dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "writer"}}),
    )

    responses = [coder_delete, writer_delete]
    successes = [response for response in responses if response["ok"]]
    failures = [response for response in responses if not response["ok"]]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error"]["code"] == "last_agent"
    assert len(state.runtime.agents.list()) == 1
    assert len(successes[0]["result"]["remaining_agents"]) == 1
    assert agent_delete_lock.max_active == 1


@pytest.mark.asyncio
async def test_session_create_make_current_updates_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "session.create",
            "params": {"agent_id": "coder", "session_id": "current-two", "make_current": True},
        },
    )

    assert response["ok"] is True
    assert state.runtime.agents.get("coder").current_session_id == "current-two"
