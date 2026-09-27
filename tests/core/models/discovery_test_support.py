"""Shared Provider configs, raw payload builders, and HTTP mocks for discovery tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import respx

from core.models.models_dev import ModelsDevCatalog
from core.providers.openai import CODEX_PACKAGE_METADATA_URL
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_IMAGE_MODELS_URL = "https://openrouter.ai/api/v1/images/models"
OPENROUTER_VIDEO_MODELS_URL = "https://openrouter.ai/api/v1/videos/models"
GITHUB_COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
OPENAI_SUBSCRIPTION_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
OPENCODE_GO_MODELS_URL = "https://opencode-go.example/v1/models"
SIMPLE_MODELS_URL = "https://simple.example/v1/models"
API_KEY = "test-openrouter-key"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def api_key_connection(credential_key: str, **changes: Any) -> ConnectionConfig:
    fields: dict[str, Any] = {
        "id": "api-key",
        "type": "api_key",
        "label": "API Key",
        "auth": AuthConfig(header="Authorization", prefix="Bearer ", credential_key=credential_key),
    }
    return ConnectionConfig(**(fields | changes))


def keyless_connection() -> ConnectionConfig:
    return ConnectionConfig(
        id="local",
        type="none",
        label="Local",
        auth=AuthConfig(header="", prefix="", credential_key=""),
    )


def simple_compatible_config(**changes: Any) -> ProviderConfig:
    """Minimal OpenAI-compatible Provider: one fetch per refresh, no supplementary calls."""

    return ProviderConfig(
        **{
            "id": "simple",
            "name": "Simple",
            "adapter": "openai_compatible",
            "base_url": "https://simple.example/v1",
            "connections": [api_key_connection("SIMPLE_KEY")],
            "defaults": {},
            "models_endpoint": "/models",
        }
        | changes
    )


def openrouter_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[api_key_connection("OPENROUTER_API_KEY")],
        defaults={"max_tokens": 8192},
        extra_headers={"X-Title": "vBot"},
        models_endpoint="/models",
    )


def github_copilot_config() -> ProviderConfig:
    return ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
        base_url="https://api.githubcopilot.com",
        connections=[
            api_key_connection(
                "GITHUB_COPILOT_TOKEN", id="oauth", type="oauth", label="GitHub OAuth"
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={"Copilot-Integration-Id": "vbot"},
        models_endpoint="/models",
    )


def opencode_go_config(**changes: Any) -> ProviderConfig:
    return ProviderConfig(
        **{
            "id": "opencode-go",
            "name": "OpenCode Go",
            "adapter": "opencode_go",
            "base_url": "https://opencode-go.example/v1",
            "connections": [api_key_connection("OPENCODE_API_KEY")],
            "defaults": {"max_tokens": 8192},
            "models_endpoint": "/models",
        }
        | changes
    )


def openai_subscription_config() -> ProviderConfig:
    """The ``openai`` Provider reduced to its Codex Subscription Connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                base_url="https://chatgpt.com/backend-api",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                mode="codex_responses",
                models_endpoint="/codex/models",
            )
        ],
        defaults={"max_tokens": 8192},
    )


def raw_openrouter_model(
    *,
    model_id: str = "anthropic/claude-sonnet-4",
    name: str = "Anthropic: Claude Sonnet 4",
    input_modalities: list[str] | None = None,
    output_modalities: list[str] | None = None,
    supported_parameters: list[str] | None = None,
    context_length: int = 128000,
    max_completion_tokens: int | None = 64000,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": name,
        "architecture": {
            "input_modalities": input_modalities or ["text", "image"],
            "output_modalities": output_modalities or ["text"],
            "modality": "text+image->text",
        },
        "supported_parameters": (
            supported_parameters
            if supported_parameters is not None
            else ["tools", "response_format", "reasoning"]
        ),
        "context_length": context_length,
        "top_provider": {"max_completion_tokens": max_completion_tokens},
    }


def model_data(name: str = "Model Name") -> dict[str, Any]:
    return {
        "name": name,
        "capabilities": {
            "vision": False,
            "tools": True,
            "json_mode": True,
            "reasoning": {"supported": False},
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_parameters": ["response_format", "tools"],
            "task_types": ["chat", "text_output"],
        },
        "context_window": 32000,
        "max_output_tokens": 4096,
    }


def jwt_with_openai_account(account_id: str = "acct_vbot") -> str:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


def fixture_models_dev_catalog() -> ModelsDevCatalog:
    """The trimmed models.dev capture under ``fixtures/``."""

    path = FIXTURES_DIR / "models_dev_catalog.json"
    return ModelsDevCatalog(json.loads(path.read_text(encoding="utf-8")))


def read_models_file(resources_dir: Path, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((resources_dir / "models" / name).read_text(encoding="utf-8"))
    return data


def mock_openrouter_image_catalog(entries: list[dict[str, Any]] | None = None) -> None:
    """Mock the image-API catalog the OpenRouter Adapter fetches (inside ``respx.mock``)."""

    respx.get(OPENROUTER_IMAGE_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": entries or []})
    )


def mock_openai_codex_package(version: str = "0.144.6") -> None:
    """Mock the official stable Codex package metadata used by discovery."""

    respx.get(CODEX_PACKAGE_METADATA_URL).mock(
        return_value=httpx.Response(200, json={"name": "@openai/codex", "version": version})
    )
