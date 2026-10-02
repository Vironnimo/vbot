"""Settings read RPCs: ``settings.get``, ``get_raw``, ``catalog`` and ``get_path``.

Writes (``settings.update`` and ``settings.patch``) live in
``test_settings_methods_update.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc import settings_methods
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
    openrouter_provider,
    rpc_error,
    rpc_result,
)

__all__ = ["_no_models_dev_fetch"]

_SETTINGS_LOGGER = "vbot.server.rpc.settings"


def _copilot_device_flow_provider() -> SimpleNamespace:
    return SimpleNamespace(
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


@pytest.mark.asyncio
async def test_settings_get_returns_normalized_settings_payload_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-live-secret")
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_OAUTH_TOKEN",
        "OLLAMA_API_KEY",
        "OPENROUTER_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "SERPER_API_KEY",
        "FIRECRAWL_API_KEY",
        "PERPLEXITY_API_KEY",
        "PARALLEL_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    state = make_state(tmp_path, StubAdapter())
    # Blank entries, like the seeded .env's placeholders, are present but not set.
    (tmp_path / ".env").write_text('TAVILY_API_KEY=\nEXA_API_KEY="  "\n', encoding="utf-8")
    state.runtime.providers.add(_copilot_device_flow_provider())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.models._models["github-copilot"] = []
    state.runtime.models._models["openrouter"] = []
    state.server_bind = {
        "listen_host": "0.0.0.0",
        "listen_port": 9001,
        "port_source": "settings.server_port",
    }

    result = await rpc_result(state, "settings.get")

    timezone = result["general"].pop("timezone")
    available_timezones = result["general"].pop("available_timezones")
    assert timezone in available_timezones
    assert "Europe/Berlin" in available_timezones
    assert result == {
        "general": {
            "server": {
                "listen_host": "0.0.0.0",
                "listen_port": 9001,
                "port_source": "settings.server_port",
            },
            "data_directory": str(tmp_path),
            "build": {
                "version": "0.4.4",
                "revision": "a" * 40,
                "branch": "main",
                "release": False,
            },
            "keep_awake": False,
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
                    "id": "github-copilot",
                    "name": "GitHub Copilot",
                    "base_url": "https://api.githubcopilot.com",
                    "models_endpoint": None,
                    "connections": [
                        {
                            "id": "github-copilot:oauth",
                            "type": "oauth",
                            "label": "Sign in with GitHub",
                            "configured": False,
                            "enabled": True,
                            "usable": False,
                            "accounts": [],
                            # A device-flow OAuth Connection can be signed in from Settings.
                            "connectable": True,
                        }
                    ],
                    "credentials_configured": False,
                    "status": "missing_credentials",
                    "model_count": 0,
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
                {
                    "id": "openrouter",
                    "name": "OpenRouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    # The endpoint enables the Settings refresh button.
                    "models_endpoint": "/models",
                    "connections": [
                        {
                            "id": "openrouter:api-key",
                            "type": "api_key",
                            "label": "API Key",
                            "configured": False,
                            "enabled": True,
                            "usable": False,
                            "accounts": [],
                            "credential_key": "OPENROUTER_API_KEY",
                        }
                    ],
                    "credentials_configured": False,
                    "status": "missing_credentials",
                    "model_count": 0,
                    "kind": "remote",
                    "editable": False,
                    "routing": {
                        "default": {
                            "mode": "automatic",
                            "providers": [],
                            "blocked": [],
                            "allow_fallbacks": True,
                        },
                        "models": {},
                    },
                },
            ],
            "custom_endpoints": {"supported": True, "items": []},
        },
        "appearance": {
            "language": "en",
            "available_languages": ["en"],
            "chat_width": "comfortable",
            "chat_working_mode": "normal",
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
            "backend": "sqlite_fts",
            "available_backends": ["hybrid", "sqlite_fts", "vector"],
        },
        "web_fetch": {
            "provider": "direct",
            "mode": "fallback",
            "available_providers": ["direct", "firecrawl", "tavily", "exa", "parallel"],
            "services": [
                {
                    "id": "firecrawl",
                    "api_key_env": "FIRECRAWL_API_KEY",
                    "configured": False,
                    "source": None,
                    "pricing_url": "https://www.firecrawl.dev/pricing",
                },
                {
                    "id": "tavily",
                    "api_key_env": "TAVILY_API_KEY",
                    "configured": False,
                    "source": "data_dir",
                    "pricing_url": "https://docs.tavily.com/documentation/api-credits",
                },
                {
                    "id": "exa",
                    "api_key_env": "EXA_API_KEY",
                    "configured": False,
                    "source": "data_dir",
                    "pricing_url": "https://exa.ai/pricing",
                },
                {
                    "id": "parallel",
                    "api_key_env": "PARALLEL_API_KEY",
                    "configured": False,
                    "source": None,
                    "pricing_url": "https://docs.parallel.ai/getting-started/pricing",
                },
            ],
        },
        "web_search": {
            "provider": "brave",
            "available_providers": [
                "brave",
                "duckduckgo",
                "exa",
                "firecrawl",
                "perplexity",
                "searxng",
                "serper",
                "tavily",
            ],
            "default_count": 12,
            "searxng": {"base_url": "http://localhost:8888"},
            # Keyed providers only: SearXNG and DuckDuckGo need no key.
            "services": [
                {
                    "id": "brave",
                    "api_key_env": "BRAVE_API_KEY",
                    "configured": True,
                    "source": "process_environment",
                },
                {
                    "id": "tavily",
                    "api_key_env": "TAVILY_API_KEY",
                    "configured": False,
                    "source": "data_dir",
                },
                {
                    "id": "exa",
                    "api_key_env": "EXA_API_KEY",
                    "configured": False,
                    "source": "data_dir",
                },
                {
                    "id": "serper",
                    "api_key_env": "SERPER_API_KEY",
                    "configured": False,
                    "source": None,
                },
                {
                    "id": "firecrawl",
                    "api_key_env": "FIRECRAWL_API_KEY",
                    "configured": False,
                    "source": None,
                },
                {
                    "id": "perplexity",
                    "api_key_env": "PERPLEXITY_API_KEY",
                    "configured": False,
                    "source": None,
                },
            ],
        },
        "debug": {
            "enabled": False,
            "trace_limit": 50,
            "trace_count": 0,
        },
        "archive": {"retention_days": 30},
        "reflection": {
            "enabled": True,
            "memory_turn_interval": 10,
            "skill_model_step_interval": 10,
        },
        "librarian": {
            "enabled": True,
            "interval_days": 7,
            "archive_after_days": 90,
            "consolidate": True,
            "model": "",
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
        "notifications": {
            "run_completed": True,
            "run_failed": True,
            "automation_failed": True,
            "update_result": True,
            "server_stopped": True,
        },
        "local_models": {"context_windows": {}},
        "skills": {
            "default_directory": str(tmp_path / "skills"),
            "directories": [],
        },
    }
    assert "sk-live-secret" not in str(result)
    assert "brave-live-secret" not in str(result)
    # Retired settings are not returned: token counts and the live voice opt-in.
    assert "show_token_counts" not in str(result)
    assert "live_voice" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "warns"),
    [(FileNotFoundError("no traces yet"), False), (RuntimeError("store corrupt"), True)],
    ids=["missing-store", "unexpected-failure"],
)
async def test_settings_get_reports_zero_traces_when_the_trace_store_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    warns: bool,
) -> None:
    """A missing store is expected and silent; anything else warns with a traceback."""

    def failing_store(**_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(settings_methods, "DebugTraceStore", failing_store)
    state = make_state(tmp_path, StubAdapter())

    with caplog.at_level(logging.WARNING, logger=_SETTINGS_LOGGER):
        result = await rpc_result(state, "settings.get")

    assert result["debug"]["trace_count"] == 0
    warnings = [record for record in caplog.records if record.name == _SETTINGS_LOGGER]
    assert len(warnings) == (1 if warns else 0)
    assert all(record.exc_info is not None for record in warnings)


@pytest.mark.asyncio
async def test_settings_get_raw_returns_raw_settings_payload(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"server_port": 9001, "debug": {"trace_limit": 20}})

    result = await rpc_result(state, "settings.get_raw")

    assert result == {"settings": {"server_port": 9001, "debug": {"trace_limit": 20}}}


@pytest.mark.asyncio
async def test_settings_catalog_exposes_public_paths_and_lifecycle(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await rpc_result(state, "settings.catalog", prefix="web_search")

    entries = {entry["path"]: entry for entry in result["settings"]}
    provider = entries["web_search.provider"]
    assert provider["value"] == "brave"
    assert provider["source"] == "default"
    assert provider["allowed_values"] == [
        "brave",
        "duckduckgo",
        "exa",
        "firecrawl",
        "perplexity",
        "searxng",
        "serper",
        "tavily",
    ]
    assert provider["application"] == "live"


@pytest.mark.asyncio
async def test_settings_catalog_lists_a_patched_quoted_dynamic_path(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    path = 'local_models.context_windows["ollama/qwen2.5:7b"]'

    await rpc_result(
        state, "settings.patch", operations=[{"op": "set", "path": path, "value": 32768}]
    )
    result = await rpc_result(state, "settings.catalog", prefix=path)

    assert state.runtime.storage.load_local_models_settings() == {
        "context_windows": {"ollama/qwen2.5:7b": 32768}
    }
    assert result["settings"] == [
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

    result = await rpc_result(state, "settings.get_path", path="web_search.provider")

    setting = result["setting"]
    assert setting["value"] == "brave"
    assert setting["default"] == "brave"
    assert setting["configured"] is False
    assert setting["source"] == "default"
    assert setting["restart_required"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        ("settings.get", {"extra": True}, "settings.get does not accept params"),
        ("settings.catalog", {"prefix": 5}, "params.prefix must be a string"),
        (
            "settings.get_path",
            {"path": "web_search.provider", "allow_missing": "yes"},
            "params.allow_missing must be a boolean",
        ),
    ],
)
async def test_malformed_settings_reads_are_rejected(
    tmp_path: Path, method: str, params: JsonObject, named: str
) -> None:
    state = make_state(tmp_path, StubAdapter())

    error = await rpc_error(state, method, **params)

    assert error == {"code": "invalid_request", "message": named}
