"""The offline Provider Runtime the snapshot renders through.

Builds a temporary data directory with Settings (one Custom Provider, one
local context-window override), the bundled Provider and Model registries,
fake credentials for every Connection (API keys through the credential
resolver's environment, OAuth logins through the token store) and the
production :class:`ProviderRuntime`. Every Connection is enabled, keyless ones
included. The local Ollama catalog is runtime-discovered, so a few synthetic
discovered Models are added through the Ollama Adapter's own catalog
normalization.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.models.models import Model, ModelRegistry
from core.providers.credentials import ProviderCredentialResolver
from core.providers.ollama import OllamaAdapter
from core.providers.openai_subscription_auth import openai_subscription_token_extra
from core.providers.providers import ProviderConfig, ProviderRegistry
from core.providers.runtime import ProviderRuntime
from core.providers.token_getter import (
    COPILOT_API_ENDPOINT_EXTRA_KEY,
    GITHUB_OAUTH_TOKEN_EXTRA_KEY,
)
from core.providers.token_store import OAuthToken, TokenStore
from core.storage.storage import StorageManager
from scripts._wire_snapshot.transport import COPILOT_API_ENDPOINT, COPILOT_API_TOKEN

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESOURCES_DIR = PROJECT_ROOT / "resources"

CUSTOM_PROVIDER_ID = "snapshot-custom"
OLLAMA_PROVIDER_ID = "ollama"
CODEX_ACCOUNT_ID = "snapshot-chatgpt-account"

CUSTOM_PROVIDER_SETTINGS: dict[str, Any] = {
    "name": "Snapshot Custom",
    "adapter": "openai_compatible",
    "base_url": "https://custom.snapshot.invalid/v1",
    "auth": "api_key",
    "defaults": {"max_tokens": 4096},
    "models": {
        "custom-chat": {
            "name": "Custom Chat",
            "context_window": 65536,
            "max_output_tokens": 8192,
            "capabilities": {"tools": True, "reasoning": True},
        },
        "custom-vision": {
            "name": "Custom Vision",
            "context_window": 32768,
            "capabilities": {"vision": True, "tools": True, "input_modalities": ["text", "image"]},
        },
    },
}

# ``/api/tags`` entries and their ``/api/show`` answers for the synthetic local catalog.
_OLLAMA_TAGS: tuple[dict[str, Any], ...] = (
    {"model": "qwen3:8b", "details": {"family": "qwen3"}},
    {"model": "gpt-oss:20b", "details": {"family": "gptoss"}},
    {"model": "gemma3:4b", "details": {"family": "gemma3"}},
    {"model": "glm-4.6:cloud", "details": {"family": "glm4"}, "remote_host": "https://ollama.com"},
    {"model": "nomic-embed-text:latest", "details": {"family": "nomic-bert"}},
)
_OLLAMA_SHOW: dict[str, dict[str, Any]] = {
    "qwen3:8b": {
        "capabilities": ["completion", "tools", "thinking"],
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
    },
    "gpt-oss:20b": {
        "capabilities": ["completion", "tools", "thinking"],
        "model_info": {"general.architecture": "gptoss", "gptoss.context_length": 131072},
    },
    "gemma3:4b": {
        "capabilities": ["completion", "vision"],
        "model_info": {"general.architecture": "gemma3", "gemma3.context_length": 131072},
    },
    "glm-4.6:cloud": {
        "capabilities": ["completion", "tools", "thinking"],
        "model_info": {"general.architecture": "glm4", "glm4.context_length": 202752},
    },
    "nomic-embed-text:latest": {
        "capabilities": ["embedding"],
        "model_info": {"general.architecture": "nomic-bert", "nomic-bert.context_length": 2048},
    },
}

SETTINGS: dict[str, Any] = {
    "providers": {"custom": {CUSTOM_PROVIDER_ID: CUSTOM_PROVIDER_SETTINGS}},
    "local_models": {"context_windows": {f"{OLLAMA_PROVIDER_ID}/qwen3:8b": 16384}},
}


@dataclass(frozen=True)
class SnapshotEnvironment:
    """The registries, Runtime and fake secrets of one capture."""

    data_dir: Path
    providers: ProviderRegistry
    models: ModelRegistry
    runtime: ProviderRuntime
    secrets: Mapping[str, str]


async def build_environment(data_dir: Path) -> SnapshotEnvironment:
    """Build the offline Provider Runtime in the empty directory *data_dir*."""

    data_dir.mkdir(parents=True, exist_ok=True)
    storage = StorageManager(data_dir=data_dir, resources_dir=RESOURCES_DIR)
    storage.save_settings(SETTINGS)
    custom_providers = storage.load_custom_providers_settings()
    providers = ProviderRegistry.load(RESOURCES_DIR, custom_providers=custom_providers)
    bundled_models = ModelRegistry.load(RESOURCES_DIR, custom_providers=custom_providers)
    models = await _with_discovered_ollama_models(bundled_models, providers)

    token_store = TokenStore(data_dir)
    secrets: dict[str, str] = {}
    process_env: dict[str, str] = {}
    enabled: dict[str, bool] = {}
    for provider_id in providers.list_ids():
        config = providers.get(provider_id)
        for connection in config.connections:
            enabled[f"{provider_id}:{connection.id}"] = True
            if connection.type == "oauth" and connection.oauth is not None:
                token, token_secrets = _fake_oauth_token(config, connection.id)
                token_store.save(provider_id, connection.id, token)
                secrets.update(token_secrets)
            elif connection.type in {"api_key", "oauth"} and connection.auth.credential_key:
                key = connection.auth.credential_key
                process_env[key] = f"snapshot-key-{key.lower().replace('_', '-')}"
                secrets[process_env[key]] = key

    credentials = ProviderCredentialResolver(
        providers,
        fallback_credentials={},
        process_env=process_env,
        token_store=token_store,
        enabled_overrides_loader=lambda: enabled,
    )
    runtime = ProviderRuntime(
        providers=providers,
        models=models,
        credentials=credentials,
        token_store=token_store,
        storage=storage,
        resources_path=RESOURCES_DIR,
        logger=logging.getLogger("vbot.wire_snapshot"),
    )
    return SnapshotEnvironment(
        data_dir=data_dir,
        providers=providers,
        models=models,
        runtime=runtime,
        secrets=secrets,
    )


def _fake_oauth_token(
    config: ProviderConfig, connection_id: str
) -> tuple[OAuthToken, dict[str, str]]:
    """Return a non-expiring fake login, or Copilot's expired one that forces its exchange."""

    label = f"{config.id}:{connection_id}"
    if config.id == "openai":
        access_token = _fake_codex_jwt()
        return (
            OAuthToken(
                access_token=access_token, extra=openai_subscription_token_extra(access_token)
            ),
            {access_token: f"{label}:access-token"},
        )
    if config.id == "github-copilot":
        github_token = "snapshot-github-oauth-token"
        expired_token = "snapshot-copilot-expired-token"
        return (
            OAuthToken(
                access_token=expired_token,
                expires_at=datetime(2001, 1, 1, tzinfo=UTC),
                extra={
                    GITHUB_OAUTH_TOKEN_EXTRA_KEY: github_token,
                    COPILOT_API_ENDPOINT_EXTRA_KEY: COPILOT_API_ENDPOINT,
                },
            ),
            {
                github_token: f"{label}:github-oauth-token",
                expired_token: f"{label}:expired-api-token",
                COPILOT_API_TOKEN: f"{label}:api-token",
            },
        )
    access_token = f"snapshot-oauth-{config.id}-{connection_id}"
    return OAuthToken(access_token=access_token), {access_token: f"{label}:access-token"}


def _fake_codex_jwt() -> str:
    """Return a JWT-shaped token carrying the ChatGPT account claim Codex requires."""

    def encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = encode({"alg": "none", "typ": "JWT"})
    payload = encode({"https://api.openai.com/auth": {"chatgpt_account_id": CODEX_ACCOUNT_ID}})
    return f"{header}.{payload}.snapshot-signature"


async def _with_discovered_ollama_models(
    bundled: ModelRegistry, providers: ProviderRegistry
) -> ModelRegistry:
    """Return *bundled* plus synthetic local Ollama Models, normalized like discovery does."""

    connection = providers.get(OLLAMA_PROVIDER_ID).get_connection("local")
    normalized = {
        model.model_id: model
        for model in (OllamaAdapter.normalize_catalog_entry(raw) for raw in _OLLAMA_TAGS)
    }

    async def post_json(endpoint: str, body: dict[str, Any]) -> Any:
        del endpoint
        return _OLLAMA_SHOW[body["model"]]

    enriched = await OllamaAdapter.enrich_discovered_models(normalized, post_json)
    discovered: dict[str, Model] = {
        model_id: OllamaAdapter.finalize_discovered_model(model, connection)
        for model_id, model in enriched.items()
    }

    models: dict[tuple[str, str], Model] = {}
    replay: dict[str, str] = {}
    for provider_id in providers.list_ids():
        for model in bundled.list_for_provider(provider_id):
            models[(provider_id, model.model_id)] = model
        policy = bundled.provider_reasoning_replay(provider_id)
        if policy is not None:
            replay[provider_id] = policy
    for model_id, model in discovered.items():
        models.setdefault((OLLAMA_PROVIDER_ID, model_id), model)
    return ModelRegistry(models, replay)
