"""Tests for rpc integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from server.app import create_app
from tests.server.rpc_integration_test_support import (
    IntegrationRuntime,
    SequencedAdapter,
)


def test_model_list_and_settings_get_follow_credential_contract(tmp_path: Path) -> None:
    runtime = IntegrationRuntime(tmp_path, SequencedAdapter(), configured_provider_ids={"openai"})
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        model_response = client.post("/api/rpc", json={"method": "model.list", "params": {}})
        settings_response = client.post("/api/rpc", json={"method": "settings.get", "params": {}})

    assert model_response.json() == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "openai/gpt-5.2",
                    "provider_id": "openai",
                    "model_id": "gpt-5.2",
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True, "control": None, "levels": []},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 256000,
                    "effective_context_window": 256000,
                    "local": False,
                    "max_output_tokens": 32000,
                    "connections": ["api-key"],
                }
            ]
        },
    }
    settings_payload = settings_response.json()
    timezone = settings_payload["result"]["general"].pop("timezone")
    available_timezones = settings_payload["result"]["general"].pop("available_timezones")
    assert timezone in available_timezones
    assert "Europe/Berlin" in available_timezones
    assert settings_payload == {
        "ok": True,
        "result": {
            "general": {
                "server": {
                    "listen_host": "127.0.0.1",
                    "listen_port": 8420,
                    "port_source": "default",
                },
                "data_directory": str(tmp_path),
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
                        "id": "openai",
                        "name": "OpenAI",
                        "base_url": "https://api.openai.com/v1",
                        "models_endpoint": None,
                        "connections": [
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
                            {
                                "id": "openai:subscription",
                                "type": "oauth",
                                "label": "ChatGPT Plus/Pro",
                                "configured": True,
                                "enabled": True,
                                "usable": True,
                                "accounts": [
                                    {
                                        "id": "default",
                                        "usable": True,
                                        "source": "oauth",
                                        "credential_key": "",
                                    }
                                ],
                                "connectable": False,
                            },
                        ],
                        "credentials_configured": True,
                        "status": "configured",
                        "model_count": 1,
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
                "chat_working_mode": "normal",
            },
            "subagents": {
                "max_subagent_depth": 4,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
            },
            "compaction": {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15000,
                "summary_model": None,
            },
            "recall": {
                "backend": "canonical_scan",
                "available_backends": ["canonical_scan", "hybrid", "sqlite_fts", "vector"],
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
            },
            "defaults": {},
            "debug": {
                "enabled": False,
                "trace_limit": 50,
                "trace_count": 0,
            },
            "reflection": {
                "enabled": True,
                "memory_turn_interval": 10,
                "skill_model_step_interval": 10,
            },
            "speech": {
                "transcription_audio": {
                    "profile": "compatibility",
                    "format": "wav",
                    "sample_rate_hz": 16_000,
                }
            },
            "live_voice": {"enabled": False},
            "model_tasks": {},
            "session_titles": {"enabled": False, "model": ""},
            "local_models": {"context_windows": {}},
        },
    }
    assert "env_key" not in json.dumps(settings_response.json())
    assert "missing_api_key" not in json.dumps(settings_response.json())
