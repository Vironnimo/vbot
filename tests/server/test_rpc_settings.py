"""Server RPC settings handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    EmptyStubModels,
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
            "custom_endpoints": {"supported": True, "items": []},
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
        "speech": {
            "transcription_audio": {
                "profile": "compatibility",
                "format": "wav",
                "sample_rate_hz": 16_000,
            }
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
async def test_settings_catalog_exposes_public_paths_and_lifecycle(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "settings.catalog", "params": {"prefix": "web_search"}},
    )

    assert response["ok"] is True, response
    entries = {entry["path"]: entry for entry in response["result"]["settings"]}
    provider = entries["web_search.provider"]
    assert provider["value"] == "brave"
    assert provider["source"] == "default"
    assert provider["allowed_values"] == ["brave", "searxng"]
    assert provider["application"] == "live"


@pytest.mark.asyncio
async def test_settings_catalog_filters_to_concrete_dynamic_path(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    path = 'local_models.context_windows["ollama/qwen2.5:7b"]'
    state.runtime.storage.save_settings(
        {"local_models": {"context_windows": {"ollama/qwen2.5:7b": 32768}}}
    )

    response = await dispatch_rpc(
        state,
        {"method": "settings.catalog", "params": {"prefix": path}},
    )

    assert response["ok"] is True, response
    assert response["result"]["settings"] == [
        {
            "path": path,
            "template": 'local_models.context_windows["<model>"]',
            "type": "integer",
            "description": "Effective context window override for one local Model.",
            "application": "live",
            "nullable": False,
            "unsettable": True,
            "has_default": False,
            "minimum": 1,
            "exclusive_minimum": False,
            "configured": True,
            "source": "configured",
            "restart_required": False,
            "value": 32768,
            "configured_value": 32768,
        }
    ]


@pytest.mark.asyncio
async def test_settings_get_path_returns_effective_value_and_details(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "settings.get_path", "params": {"path": "web_search.provider"}},
    )

    assert response["ok"] is True, response
    setting = response["result"]["setting"]
    assert setting["value"] == "brave"
    assert setting["default"] == "brave"
    assert setting["configured"] is False
    assert setting["source"] == "default"
    assert setting["restart_required"] is False


@pytest.mark.asyncio
async def test_settings_patch_applies_searxng_configuration_atomically(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [
                    {"op": "set", "path": "web_search.provider", "value": "searxng"},
                    {
                        "op": "set",
                        "path": "web_search.searxng.base_url",
                        "value": "https://search.example/",
                    },
                ]
            },
        },
    )

    assert response["ok"] is True, response
    assert response["result"]["changed"] == [
        "web_search.provider",
        "web_search.searxng.base_url",
    ]
    assert response["result"]["restart_required"] is False
    assert state.runtime.storage.load_web_search_settings() == {
        "provider": "searxng",
        "default_count": 12,
        "searxng": {"base_url": "https://search.example/"},
    }


@pytest.mark.asyncio
async def test_settings_patch_is_all_or_nothing_on_invalid_value(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"web_search": {"provider": "brave"}})

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [
                    {"op": "set", "path": "web_search.provider", "value": "searxng"},
                    {"op": "set", "path": "debug.trace_limit", "value": 0},
                ]
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert state.runtime.storage.load_settings() == {"web_search": {"provider": "brave"}}


@pytest.mark.asyncio
async def test_settings_patch_reports_restart_pending_against_active_port(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.server_bind = {
        "listen_host": "127.0.0.1",
        "listen_port": 8420,
        "port_source": "default",
    }

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {"operations": [{"op": "set", "path": "server.port", "value": 9000}]},
        },
    )

    assert response["ok"] is True, response
    change = response["result"]["changes"][0]
    assert change["value"] == 8420
    assert change["configured_value"] == 9000
    assert change["application"] == "restart"
    assert change["restart_required"] is True
    assert state.runtime.storage.load_settings()["server_port"] == 9000


@pytest.mark.asyncio
async def test_settings_patch_unset_restart_setting_reports_default_pending(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"server_port": 9000})
    state.server_bind = {
        "listen_host": "127.0.0.1",
        "listen_port": 9000,
        "port_source": "settings.server_port",
    }

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {"operations": [{"op": "unset", "path": "server.port"}]},
        },
    )

    assert response["ok"] is True, response
    change = response["result"]["changes"][0]
    assert change["value"] == 9000
    assert change["pending_value"] == 8420
    assert change["configured"] is False
    assert change["restart_required"] is True
    assert response["result"]["restart_required"] is True


@pytest.mark.asyncio
async def test_settings_patch_unset_restores_default(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"web_search": {"provider": "searxng"}})

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {"operations": [{"op": "unset", "path": "web_search.provider"}]},
        },
    )

    assert response["ok"] is True, response
    change = response["result"]["changes"][0]
    assert change["value"] == "brave"
    assert change["configured"] is False
    assert change["source"] == "default"


@pytest.mark.asyncio
async def test_settings_patch_supports_quoted_dynamic_model_key(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    path = 'local_models.context_windows["ollama/qwen2.5:7b"]'

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {"operations": [{"op": "set", "path": path, "value": 32768}]},
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_local_models_settings() == {
        "context_windows": {"ollama/qwen2.5:7b": 32768}
    }


@pytest.mark.asyncio
async def test_settings_patch_ignores_unrelated_runtime_semantic_setting(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"recall": {"backend": "extension_backend"}})

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [{"op": "set", "path": "web_search.provider", "value": "searxng"}]
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_settings()["recall"] == {"backend": "extension_backend"}
    assert state.runtime.recall_reload_count == 0


@pytest.mark.asyncio
async def test_settings_patch_reloads_extension_directories_live(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    extension_dir = tmp_path / "extra-extensions"

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [
                    {
                        "op": "set",
                        "path": "extensions.directories",
                        "value": [str(extension_dir)],
                    }
                ]
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.extension_reload_count == 1


@pytest.mark.asyncio
async def test_settings_patch_reloads_skill_directories_live(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    previous_skills = state.runtime.skills
    skill_dir = tmp_path / "extra-skills"

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [
                    {
                        "op": "set",
                        "path": "skills.directories",
                        "value": [str(skill_dir)],
                    }
                ]
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.skills is not previous_skills


@pytest.mark.asyncio
async def test_settings_patch_reloads_recall_backend_live(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.patch",
            "params": {
                "operations": [{"op": "set", "path": "recall.backend", "value": "sqlite_fts"}]
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.recall_reload_count == 1
