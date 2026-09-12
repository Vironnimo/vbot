"""Connection selection and authenticated Adapter construction for the exact probe."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models.models import ModelRegistry
from core.providers.ollama import OllamaCloudAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.providers import ConnectionConfig, ProviderConfig, ProviderRegistry
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter, TokenGetter
from core.providers.token_store import TokenStore
from core.providers.xai import XAIAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RESOURCES_DIR = PROJECT_ROOT / "resources"


PROBE_ADAPTERS = {
    "ollama-cloud": OllamaCloudAdapter,
    "openai": OpenAIAdapter,
    "xai": XAIAdapter,
}


def _load_api_key(env_name: str, data_dir: Path) -> str:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        raise SystemExit(f"no .env found at {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"\s*{re.escape(env_name)}\s*=\s*(.+?)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"no {env_name} entry in {env_path}")


@dataclass(frozen=True)
class ProbeAdapter:
    adapter: Any
    connection_id: str
    credential_source: str
    token_getter: TokenGetter


def _select_probe_connection(
    provider_config: ProviderConfig,
    *,
    data_dir: Path,
    connection_id: str | None,
    api_key_env: str | None,
) -> ConnectionConfig:
    """Select one exact Connection without silently changing wire families."""

    if connection_id:
        try:
            return provider_config.get_connection(connection_id)
        except KeyError as error:
            raise SystemExit(
                f"unknown Connection {connection_id!r} for Provider {provider_config.id!r}"
            ) from error

    if api_key_env:
        matching = [
            connection
            for connection in provider_config.connections
            if connection.type == "api_key" and connection.auth.credential_key == api_key_env
        ]
        if len(matching) == 1:
            return matching[0]

    token_store = TokenStore(data_dir)
    usable_oauth = [
        connection
        for connection in provider_config.connections
        if connection.type == "oauth"
        and connection.oauth is not None
        and token_store.has_valid_token(provider_config.id, connection.id)
    ]
    if len(usable_oauth) == 1:
        return usable_oauth[0]

    if len(provider_config.connections) == 1:
        return provider_config.connections[0]

    choices = ", ".join(connection.id for connection in provider_config.connections)
    raise SystemExit(
        f"cannot select one Connection for Provider {provider_config.id!r}; "
        f"pass --connection from: {choices}"
    )


def _probe_token_getter(
    provider_config: ProviderConfig,
    connection: ConnectionConfig,
    *,
    data_dir: Path,
    api_key_env: str | None,
) -> tuple[TokenGetter, str]:
    if connection.type == "api_key":
        env_name = api_key_env or connection.auth.credential_key
        if not env_name:
            raise SystemExit(
                f"Connection {connection.id!r} has no credential key; pass --api-key-env"
            )
        return StaticTokenGetter(_load_api_key(env_name, data_dir)), f"api_key:{env_name}"
    if connection.type == "oauth" and connection.oauth is not None:
        token_store = TokenStore(data_dir)
        if not token_store.has_valid_token(provider_config.id, connection.id):
            raise SystemExit(
                f"no usable OAuth token for {provider_config.id}:{connection.id} under {data_dir}"
            )
        return (
            OAuthTokenGetter(
                token_store,
                provider_config.id,
                connection.id,
                connection.oauth,
            ),
            "oauth",
        )
    raise SystemExit(
        f"Connection {provider_config.id}:{connection.id} has unsupported type {connection.type!r}"
    )


def _build_adapter(
    provider_id: str,
    model_id: str,
    *,
    data_dir: Path,
    connection_id: str | None = None,
    api_key_env: str | None = None,
) -> ProbeAdapter:
    provider_registry = ProviderRegistry.load(RESOURCES_DIR)
    config = provider_registry.get(provider_id)
    connection = _select_probe_connection(
        config,
        data_dir=data_dir,
        connection_id=connection_id,
        api_key_env=api_key_env,
    )
    token_getter, credential_source = _probe_token_getter(
        config,
        connection,
        data_dir=data_dir,
        api_key_env=api_key_env,
    )
    model_registry = ModelRegistry.load(RESOURCES_DIR)
    if provider_id == "opencode-go":
        from core.providers.opencode_go import OpenCodeGoAdapter

        adapter_class: Any = OpenCodeGoAdapter
    else:
        adapter_class = PROBE_ADAPTERS.get(provider_id)
    if adapter_class is None:
        raise SystemExit(f"provider {provider_id} not supported by the exact probe yet")
    adapter = adapter_class(
        config,
        token_getter,
        connection.base_url,
        connection.auth,
        model_lookup=lambda mid: model_registry.get(provider_id, mid.split("::", 1)[0]),
        connection_mode=connection.mode,
    )
    return ProbeAdapter(
        adapter=adapter,
        connection_id=connection.id,
        credential_source=credential_source,
        token_getter=token_getter,
    )
