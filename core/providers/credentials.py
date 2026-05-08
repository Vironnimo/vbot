"""Provider credential resolution helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping

from core.providers.providers import ProviderRegistry
from core.utils.errors import ConfigError


class ProviderCredentialResolver:
    """Resolve provider credentials from process env with data-dir fallback."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        *,
        fallback_credentials: Mapping[str, str] | None = None,
        process_env: Mapping[str, str] | None = None,
    ) -> None:
        self._provider_registry = provider_registry
        self._fallback_credentials = dict(fallback_credentials or {})
        self._process_env = os.environ if process_env is None else process_env

    def has_credentials(self, provider_id: str) -> bool:
        """Return whether the provider has a non-empty configured credential."""

        provider_config = self._provider_registry.get(provider_id)
        credential_value = self._resolve_credential_value(provider_config.auth.credential_key)
        return bool(credential_value)

    def get_credentials(self, provider_id: str) -> str:
        """Return the configured credential value for *provider_id*."""

        provider_config = self._provider_registry.get(provider_id)
        credential_key = provider_config.auth.credential_key
        credential_value = self._resolve_credential_value(credential_key)
        if credential_value:
            return credential_value

        raise ConfigError(
            f"Provider credentials not found for provider '{provider_id}': "
            f"credential '{credential_key}' is not set"
        )

    def _resolve_credential_value(self, credential_key: str) -> str:
        if credential_key in self._process_env:
            return self._process_env[credential_key]
        return self._fallback_credentials.get(credential_key, "")
