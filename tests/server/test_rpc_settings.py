"""Server RPC settings handlers."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    EmptyStubModels,
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
    openrouter_provider,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_settings_get_returns_normalized_settings_payload_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.server_bind = {
        "listen_host": "0.0.0.0",
        "listen_port": 9001,
        "port_source": "settings.server_port",
    }

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True, response
    assert response["result"] == {
        "general": {
            "server": {
                "listen_host": "0.0.0.0",
                "listen_port": 9001,
                "port_source": "settings.server_port",
            },
            "data_directory": str(tmp_path),
        },
        "providers": {
            "items": [
                {
                    "id": "anthropic",
                    "name": "Anthropic",
                    "base_url": "https://api.anthropic.com/v1",
                    "models_endpoint": None,
                    "connections": [
                        {
                            "id": "anthropic:api-key",
                            "type": "api_key",
                            "label": "API Key",
                            "configured": False,
                            "enabled": True,
                            "usable": False,
                            "accounts": [],
                            "credential_key": "ANTHROPIC_API_KEY",
                        }
                    ],
                    "credentials_configured": False,
                    "status": "missing_credentials",
                    "model_count": 1,
                    "kind": "remote",
                    "editable": False,
                },
                {
                    "id": "ollama",
                    "name": "Ollama",
                    "base_url": "",
                    "models_endpoint": None,
                    "connections": [
                        {
                            "id": "ollama:api-key",
                            "type": "api_key",
                            "label": "API Key",
                            "configured": False,
                            "enabled": True,
                            "usable": False,
                            "accounts": [],
                            "credential_key": "OLLAMA_API_KEY",
                        }
                    ],
                    "credentials_configured": False,
                    "status": "missing_credentials",
                    "model_count": 1,
                    "kind": "local",
                    "editable": False,
                },
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "models_endpoint": None,
                    "connections": [
                        {
                            "id": "openai:oauth",
                            "type": "oauth",
                            "label": "OAuth",
                            "configured": False,
                            "enabled": True,
                            "usable": False,
                            "accounts": [],
                            "connectable": False,
                        },
                        {
                            "id": "openai:api-key",
                            "type": "api_key",
                            "label": "API Key",
                            "configured": True,
                            "enabled": True,
                            "usable": True,
                            "accounts": [
                                {
                                    "id": "default",
                                    "usable": True,
                                    "source": "process_env",
                                    "credential_key": "OPENAI_API_KEY",
                                }
                            ],
                            "credential_key": "OPENAI_API_KEY",
                        },
                    ],
                    "credentials_configured": True,
                    "status": "configured",
                    "model_count": 2,
                    "kind": "remote",
                    "editable": False,
                },
            ],
            "custom_endpoints": {"supported": False, "items": []},
        },
        "appearance": {
            "language": "en",
            "available_languages": ["en"],
            "chat_width": "comfortable",
        },
        "defaults": {},
        "subagents": {
            "max_subagent_depth": 4,
            "max_subagents_per_turn": 8,
            "subagent_timeout_minutes": 60,
        },
        "compaction": {
            "enabled": True,
            "trigger": {"type": "context_ratio", "threshold": 0.8},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 15000,
                "summary_model": None,
            },
        },
        "recall": {
            "backend": "jsonl_scan",
            "available_backends": ["hybrid", "jsonl_scan", "sqlite_fts", "vector"],
        },
        "web_search": {
            "provider": "brave",
            "available_providers": ["brave", "searxng"],
            "default_count": 12,
            "searxng": {"base_url": "http://localhost:8888"},
        },
        "debug": {
            "enabled": False,
            "trace_limit": 50,
            "trace_count": 0,
        },
        "reflection": {
            "enabled": False,
            "memory_turn_interval": 10,
            "skill_tool_call_interval": 25,
        },
        "model_tasks": {},
        "session_titles": {"enabled": False, "model": ""},
        "local_models": {"context_windows": {}},
        "skills": {
            "default_directory": str(tmp_path / "skills"),
            "directories": [],
        },
    }
    assert "sk-live-secret" not in str(response)
    assert "show_token_counts" not in str(response)
    assert "origin" not in response["result"]["general"]["server"]


@pytest.mark.asyncio
async def test_settings_get_marks_device_flow_oauth_connections_connectable(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="github-copilot",
            name="GitHub Copilot",
            base_url="https://api.githubcopilot.com",
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="oauth",
                    type="oauth",
                    label="Sign in with GitHub",
                    auth=SimpleNamespace(credential_key=""),
                    oauth=SimpleNamespace(flow="device"),
                )
            ],
        )
    )
    state.runtime.models._models["github-copilot"] = []

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True
    provider = next(
        item for item in response["result"]["providers"]["items"] if item["id"] == "github-copilot"
    )
    assert provider["connections"] == [
        {
            "id": "github-copilot:oauth",
            "type": "oauth",
            "label": "Sign in with GitHub",
            "configured": False,
            "enabled": True,
            "usable": False,
            "accounts": [],
            "connectable": True,
        }
    ]


@pytest.mark.asyncio
async def test_settings_get_exposes_provider_models_endpoint_for_refresh_button(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime._models = EmptyStubModels()
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True
    providers = response["result"]["providers"]["items"]
    openrouter = next(provider for provider in providers if provider["id"] == "openrouter")
    openai = next(provider for provider in providers if provider["id"] == "openai")
    assert openrouter["models_endpoint"] == "/models"
    assert openrouter["routing"] == {
        "default": {
            "mode": "automatic",
            "providers": [],
            "blocked": [],
            "allow_fallbacks": True,
        },
        "models": {},
    }
    assert openai["models_endpoint"] is None


@pytest.mark.asyncio
async def test_settings_update_persists_openrouter_routing(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime._models = EmptyStubModels()
    state.runtime.providers.add(openrouter_provider())
    routing = {
        "default": {
            "mode": "allowed",
            "providers": ["anthropic", "amazon-bedrock"],
            "blocked": ["deepinfra"],
            "allow_fallbacks": False,
        },
        "models": {
            "anthropic/claude-sonnet-4": {
                "mode": "ordered",
                "providers": ["anthropic"],
                "blocked": ["google-vertex"],
                "allow_fallbacks": True,
            }
        },
    }

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"providers": {"openrouter": {"routing": routing}}},
        },
    )

    assert response["ok"] is True, response
    openrouter = next(
        provider
        for provider in response["result"]["providers"]["items"]
        if provider["id"] == "openrouter"
    )
    assert openrouter["routing"] == routing
    assert state.runtime.storage.load_openrouter_routing_settings() == routing


@pytest.mark.asyncio
async def test_settings_get_includes_defaults_key_when_unconfigured(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True
    assert "defaults" in response["result"]
    assert response["result"]["defaults"] == {}


@pytest.mark.asyncio
async def test_settings_get_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {"extra": True}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_get_raw_returns_raw_settings_payload(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings(
        {
            "server_port": 9001,
            "feature_flags": {"logs": True},
        }
    )

    response = await dispatch_rpc(state, {"method": "settings.get_raw", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "settings": {
                "server_port": 9001,
                "feature_flags": {"logs": True},
            }
        },
    }


@pytest.mark.asyncio
async def test_settings_set_key_updates_settings_and_returns_raw_payload(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"server_host": "127.0.0.1"})

    response = await dispatch_rpc(
        state,
        {"method": "settings.set_key", "params": {"key": "server_port", "value": 9000}},
    )

    assert response == {
        "ok": True,
        "result": {
            "settings": {
                "server_host": "127.0.0.1",
                "server_port": 9000,
            }
        },
    }
    assert state.runtime.storage.load_settings() == {
        "server_host": "127.0.0.1",
        "server_port": 9000,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"key": "compaction", "value": "hello"},
            r"\$\.compaction: must be an object",
        ),
        (
            {"key": "attachment_max_size_bytes", "value": -1},
            r"\$\.attachment_max_size_bytes: must be a positive integer",
        ),
        (
            {"key": "server_port", "value": 0},
            r"\$\.server_port: must be between 1 and 65535",
        ),
    ],
)
async def test_settings_set_key_rejects_invalid_raw_settings_without_partial_write(
    tmp_path: Path,
    params: JsonObject,
    message: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_settings = {"server_port": 9000, "feature_flags": {"logs": True}}
    state.runtime.storage.save_settings(original_settings)

    response = await dispatch_rpc(state, {"method": "settings.set_key", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert re.search(message, response["error"]["message"])
    assert state.runtime.storage.load_settings() == original_settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"key": "server_port"},
        {"value": 9000},
    ],
)
async def test_settings_set_key_rejects_missing_key_or_value(
    tmp_path: Path,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.set_key", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert response["error"]["message"] == "settings.set_key requires 'key' and 'value'"
