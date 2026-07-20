"""Shared fixtures, data builders, and dependencies for discovery tests."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import core.models.discovery as discovery_module
from core.models.discovery import (
    ModelDiscoveryError,
    PassthroughModelFilter,
    PassthroughRawFilter,
    refresh_models,
)
from core.models.models import (
    Capabilities,
    Model,
    ModelRegistry,
    ReasoningCapabilities,
    is_provider_file,
)
from core.models.models_dev import ModelsDevCatalog, refresh_canonical_layer
from core.providers.errors import CatalogEntrySkipped
from core.providers.openai import CODEX_PACKAGE_METADATA_URL
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
GITHUB_COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
OPENAI_SUBSCRIPTION_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
OPENCODE_GO_MODELS_URL = "https://opencode-go.example/v1/models"
STUB_DISCOVERY_MODELS_URL = "https://stub-provider.example/v1/models"
_SIMPLE_MODELS_URL = "https://simple.example/v1/models"
OPENROUTER_IMAGE_MODELS_URL = "https://openrouter.ai/api/v1/images/models"
API_KEY = "test-openrouter-key"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _simple_compatible_config() -> ProviderConfig:
    """Minimal OpenAI-compatible provider: one fetch per refresh, no supplementary calls."""
    return ProviderConfig(
        id="simple",
        name="Simple",
        adapter="openai_compatible",
        base_url="https://simple.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="SIMPLE_KEY",
                ),
            )
        ],
        defaults={},
        models_endpoint="/models",
    )


def mock_openrouter_image_catalog(entries: list[dict[str, Any]] | None = None) -> None:
    """Mock the image-API catalog fetch the OpenRouter adapter performs.

    Registered inside each test's ``respx.mock`` context. The default empty
    catalog lets a refresh proceed without task options; entries with ids
    also get their per-model endpoint-detail route mocked as empty.
    """

    entries = entries or []
    respx.get(OPENROUTER_IMAGE_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": entries})
    )


def mock_openai_codex_package(version: str = "0.144.6") -> None:
    """Mock the official stable Codex package metadata used by discovery."""

    respx.get(CODEX_PACKAGE_METADATA_URL).mock(
        return_value=httpx.Response(
            200,
            json={"name": "@openai/codex", "version": version},
        )
    )


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    ModelRegistry._cache.clear()
    yield
    ModelRegistry._cache.clear()


@pytest.fixture()
def openrouter_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENROUTER_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={"X-Title": "vBot"},
        models_endpoint="/models",
    )


@pytest.fixture()
def github_copilot_config() -> ProviderConfig:
    return ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
        base_url="https://api.githubcopilot.com",
        connections=[
            ConnectionConfig(
                id="oauth",
                type="oauth",
                label="GitHub OAuth",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="GITHUB_COPILOT_TOKEN",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={"Copilot-Integration-Id": "vbot"},
        models_endpoint="/models",
    )


@pytest.fixture()
def opencode_go_config() -> ProviderConfig:
    return ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://opencode-go.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_GO_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        models_endpoint="/models",
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
) -> dict:
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


@pytest.fixture()
def openai_subscription_connection_config() -> ProviderConfig:
    """Provider with one OAuth connection (subscription) for Codex discovery.

    After the openai-provider merge there is a single ``openai`` provider
    with two connections; for unit-testing the connection-aware discovery
    pipeline we model that state with a connection-level
    ``base_url``/``models_endpoint`` and the Codex adapter.
    """

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
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                ),
                mode="codex_responses",
                models_endpoint="/codex/models",
            )
        ],
        defaults={"max_tokens": 8192},
    )


def model_data(name: str = "Model Name") -> dict:
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
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


__all__ = [
    "base64",
    "json",
    "logging",
    "Path",
    "Any",
    "httpx",
    "pytest",
    "respx",
    "discovery_module",
    "ModelDiscoveryError",
    "PassthroughModelFilter",
    "PassthroughRawFilter",
    "refresh_models",
    "Capabilities",
    "Model",
    "ModelRegistry",
    "ReasoningCapabilities",
    "is_provider_file",
    "ModelsDevCatalog",
    "refresh_canonical_layer",
    "CatalogEntrySkipped",
    "AuthConfig",
    "ConnectionConfig",
    "ProviderConfig",
    "OPENROUTER_MODELS_URL",
    "GITHUB_COPILOT_MODELS_URL",
    "OPENAI_SUBSCRIPTION_MODELS_URL",
    "OPENCODE_GO_MODELS_URL",
    "STUB_DISCOVERY_MODELS_URL",
    "_SIMPLE_MODELS_URL",
    "OPENROUTER_IMAGE_MODELS_URL",
    "API_KEY",
    "FIXTURES_DIR",
    "_simple_compatible_config",
    "mock_openrouter_image_catalog",
    "mock_openai_codex_package",
    "_clear_registry_cache",
    "openrouter_config",
    "github_copilot_config",
    "opencode_go_config",
    "raw_openrouter_model",
    "openai_subscription_connection_config",
    "model_data",
    "jwt_with_openai_account",
]
