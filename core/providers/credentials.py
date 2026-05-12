"""Provider credential resolution helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping

from core.providers.providers import ConnectionConfig, ProviderRegistry
from core.providers.token_store import TokenStore
from core.utils.errors import ConfigError


class ProviderCredentialResolver:
    """Resolve provider credentials from process env with data-dir fallback."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        *,
        fallback_credentials: Mapping[str, str] | None = None,
        process_env: Mapping[str, str] | None = None,
        token_store: TokenStore | None = None,
    ) -> None:
        self._provider_registry = provider_registry
        self._fallback_credentials = dict(fallback_credentials or {})
        self._process_env = os.environ if process_env is None else process_env
        self._token_store = token_store

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        """Return whether a provider or connection has configured credentials."""

        provider_config = self._provider_registry.get(provider_id)
        if connection_id is not None:
            connection = self._get_connection(provider_id, connection_id)
            return self._has_connection_credentials(provider_id, connection)

        for connection in provider_config.connections:
            if self._has_connection_credentials(provider_id, connection):
                return True
        return False

    def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str:
        """Return the configured credential value for a provider or connection."""

        provider_config = self._provider_registry.get(provider_id)
        if connection_id is not None:
            connection = self._get_connection(provider_id, connection_id)
            return self._get_connection_credentials(provider_id, connection.id)

        for connection in provider_config.connections:
            if self._has_connection_credentials(provider_id, connection):
                return self._get_connection_credentials(provider_id, connection.id)

        credential_names = ", ".join(
            connection.auth.credential_key for connection in provider_config.connections
        )
        raise ConfigError(
            f"Provider credentials not found for provider '{provider_id}': "
            f"credentials '{credential_names}' are not set"
        )

    def _get_connection_credentials(self, provider_id: str, local_id: str) -> str:
        provider_config = self._provider_registry.get(provider_id)
        connection = provider_config.get_connection(local_id)
        if self._uses_token_store(connection):
            token = None
            if self._token_store is not None:
                token = self._token_store.load(provider_id, local_id)
            if token is not None:
                return token.access_token
            raise ConfigError(
                f"Provider credentials not found for provider '{provider_id}': "
                f"OAuth token for connection '{local_id}' is not set"
            )

        credential_key = connection.auth.credential_key
        credential_value = self._resolve_credential_value(credential_key)
        if credential_value:
            return credential_value

        raise ConfigError(
            f"Provider credentials not found for provider '{provider_id}': "
            f"credential '{credential_key}' is not set"
        )

    def _get_connection(self, provider_id: str, connection_id: str) -> ConnectionConfig:
        provider_config = self._provider_registry.get(provider_id)
        expected_prefix = f"{provider_id}:"
        if not connection_id.startswith(expected_prefix):
            raise ConfigError(
                f"Unknown connection id '{connection_id}' for provider '{provider_id}'"
            )

        local_id = connection_id.removeprefix(expected_prefix)
        try:
            return provider_config.get_connection(local_id)
        except KeyError as error:
            raise ConfigError(
                f"Unknown connection id '{connection_id}' for provider '{provider_id}'"
            ) from error

    def _has_connection_credentials(self, provider_id: str, connection: ConnectionConfig) -> bool:
        if self._uses_token_store(connection):
            if self._token_store is None:
                return False
            return self._token_store.has_valid_token(provider_id, connection.id)

        credential_value = self._resolve_credential_value(connection.auth.credential_key)
        return bool(credential_value)

    def _uses_token_store(self, connection: ConnectionConfig) -> bool:
        return connection.type == "oauth" and (
            connection.oauth is not None or not connection.auth.credential_key
        )

    def _resolve_credential_value(self, credential_key: str) -> str:
        if credential_key in self._process_env:
            return self._process_env[credential_key]
        return self._fallback_credentials.get(credential_key, "")
