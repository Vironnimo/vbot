"""A GitHub Copilot-style OAuth Device Flow Provider on a minimal RPC state.

Shared by the ``provider.connect``/``disconnect``/``connection_status`` tests and
the ``model.refresh_db`` OAuth credential tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.providers.providers import AuthConfig, ConnectionConfig, OAuthConfig, ProviderConfig
from core.providers.token_store import TokenStore
from core.storage.layout import DataDirectoryLayout
from server.events import ServerEventBus

PROVIDER_ID = "github-copilot"


def oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="client-id",
        device_auth_url="https://github.com/login/device/code",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["copilot"],
        token_exchange_url="https://api.github.com/copilot_internal/v2/token",
    )


def oauth_connection() -> ConnectionConfig:
    return ConnectionConfig(
        id="oauth",
        type="oauth",
        label="Sign in with GitHub",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
        oauth=oauth_config(),
        base_url=None,
    )


def api_key_connection() -> ConnectionConfig:
    return ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="GITHUB_COPILOT_API_KEY",
        ),
        base_url=None,
    )


def copilot_provider(
    *connections: ConnectionConfig, models_endpoint: str | None = None
) -> ProviderConfig:
    return ProviderConfig(
        id=PROVIDER_ID,
        name="GitHub Copilot",
        adapter="openai_compatible",
        base_url="https://api.githubcopilot.com",
        connections=list(connections),
        models_endpoint=models_endpoint,
    )


class _ProviderRegistry:
    def __init__(self, provider: ProviderConfig) -> None:
        self._provider = provider

    def get(self, provider_id: str) -> ProviderConfig:
        if provider_id != self._provider.id:
            raise KeyError(provider_id)
        return self._provider

    def list_ids(self) -> list[str]:
        return [self._provider.id]


class _ProviderCredentials:
    """Every Connection of the Provider is usable; API keys resolve to one secret."""

    def __init__(self, usable_connection_ids: set[str]) -> None:
        self._usable_connection_ids = usable_connection_ids

    def has_credentials(self, provider_id: str, connection_id: str) -> bool:
        return provider_id == PROVIDER_ID and connection_id in self._usable_connection_ids

    def is_connection_enabled(self, provider_id: str, connection_id: str | None = None) -> bool:
        return True

    def is_usable(self, provider_id: str, connection_id: str) -> bool:
        return self.has_credentials(provider_id, connection_id)

    def get_credentials(self, provider_id: str, connection_id: str) -> str:
        if self.has_credentials(provider_id, connection_id):
            return "api-key-secret"
        raise KeyError(connection_id)

    def resolve_account_id(
        self, provider_id: str, connection_id: str, account_id: str | None = None
    ) -> str:
        assert f"{provider_id}:{connection_id}" in self._usable_connection_ids
        assert account_id is None
        return "default"


class _ModelRegistry:
    def list_for_provider(self, _provider_id: str) -> list[Any]:
        return []

    async def reload_async(self, _resources_dir: Any, **_kwargs: Any) -> None:
        # ``model.refresh_db`` reloads the registry after writing layer files;
        # nothing here reads the reloaded catalog.
        pass


def oauth_provider_state(tmp_path: Path, provider: ProviderConfig) -> SimpleNamespace:
    """Return an RPC state serving only *provider*, with a real Token Store."""

    refresh_lock = asyncio.Lock()
    return SimpleNamespace(
        runtime=SimpleNamespace(
            model_database_refresh=lambda: refresh_lock,
            providers=_ProviderRegistry(provider),
            token_store=TokenStore(tmp_path),
            provider_credentials=_ProviderCredentials(
                {f"{provider.id}:{connection.id}" for connection in provider.connections}
            ),
            storage=SimpleNamespace(
                data_dir=tmp_path,
                resources_dir=tmp_path / "resources",
                layout=DataDirectoryLayout(tmp_path),
                load_custom_providers_settings=dict,
            ),
            models=_ModelRegistry(),
        ),
        event_bus=ServerEventBus(),
    )
