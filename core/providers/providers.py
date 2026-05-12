"""Provider configuration dataclass and registry.

A ``ProviderConfig`` holds the static settings that distinguish one provider
from another: base URL, auth connections, default parameters, and optional
extra headers.  Configs are frozen (immutable) and loaded from JSON files
under ``resources/providers/``.

``ProviderRegistry`` reads every ``.json`` file in that directory, parses
each into a ``ProviderConfig``, and indexes them by provider ID.  Loading
is cached — the second call with the same *resources_dir* returns the same
registry instance without re-reading disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.utils.errors import ConfigError

# ---------------------------------------------------------------------------
# Nested dataclass for auth configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthConfig:
    """Authentication configuration for a provider.

    Attributes:
        header: HTTP header name for the API key (e.g. ``"Authorization"``).
        prefix: Value prefix prepended to the key (e.g. ``"Bearer "``).
        credential_key: Credential identifier used to look up the API key.
    """

    header: str
    prefix: str
    credential_key: str = ""


VALID_CONNECTION_TYPES = frozenset({"api_key", "oauth"})
VALID_OAUTH_FLOWS = frozenset({"device"})
VALID_MODEL_DISCOVERY_STRATEGIES = frozenset({"openai_compatible", "openrouter"})
DEFAULT_MODEL_DISCOVERY_BY_ADAPTER = {
    "openai_compatible": "openai_compatible",
}


@dataclass(frozen=True)
class OAuthConfig:
    """OAuth flow metadata for a provider connection."""

    flow: str
    client_id: str
    device_auth_url: str
    token_url: str
    scopes: list[str]
    token_exchange_url: str | None = None


@dataclass(frozen=True)
class ConnectionConfig:
    """Authentication connection configuration for a provider.

    Attributes:
        id: Local connection identifier within the provider config.
        type: Connection kind. Supported values are ``"api_key"`` and ``"oauth"``.
        label: Human-readable display label.
        auth: Authentication configuration for this connection.
        base_url: Optional provider base URL override for this connection.
    """

    id: str
    type: str
    label: str
    auth: AuthConfig
    base_url: str | None = None
    oauth: OAuthConfig | None = None


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """Immutable configuration for a single provider.

    Attributes:
        id: Unique provider identifier (matches the JSON ``id`` field and
            is used as the registry key).
        name: Human-readable provider name.
        adapter: Adapter class selector (e.g. ``"openai_compatible"``,
            ``"anthropic"``).
        base_url: Base URL for the provider API.
        connections: Authentication connection configurations.
        defaults: Optional default request parameters (e.g. ``max_tokens``,
            ``temperature``).
        extra_headers: Optional provider-specific HTTP headers.
        models_endpoint: Optional path to the models listing endpoint
            (e.g. ``"/models"``).  Reserved for future dynamic model refresh.
        model_discovery: Explicit model discovery strategy selector.
    """

    id: str
    name: str
    adapter: str
    base_url: str
    connections: list[ConnectionConfig] = field(default_factory=list)
    defaults: dict[str, Any] | None = None
    extra_headers: dict[str, str] | None = None
    models_endpoint: str | None = None
    model_discovery: str = ""

    def __post_init__(self) -> None:
        if self.model_discovery:
            return
        object.__setattr__(
            self,
            "model_discovery",
            DEFAULT_MODEL_DISCOVERY_BY_ADAPTER.get(self.adapter, ""),
        )

    def get_connection(self, local_id: str) -> ConnectionConfig:
        """Return a connection by its local provider-scoped ID."""

        for connection in self.connections:
            if connection.id == local_id:
                return connection
        raise KeyError(
            f"No connection config found for id '{local_id}' on provider '{self.id}'. "
            f"Available: {', '.join(connection.id for connection in self.connections)}"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Module-level cache keyed by the resolved resources directory path.
_registry_cache: dict[Path, ProviderRegistry] = {}


class ProviderRegistry:
    """Loads, caches, and serves ``ProviderConfig`` instances by provider ID.

    Usage::

        registry = ProviderRegistry.load(Path("resources"))
        config = registry.get("openai")
        all_ids = registry.list_ids()
    """

    def __init__(self, configs: dict[str, ProviderConfig]) -> None:
        self._configs: dict[str, ProviderConfig] = configs

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, resources_dir: Path) -> ProviderRegistry:
        """Read all provider JSON files and return a cached registry.

        The first call for a given *resources_dir* reads and parses every
        ``resources/providers/*.json`` file.  Subsequent calls with the same
        directory return the cached instance without touching disk again.

        Args:
            resources_dir: Path to the ``resources/`` root directory.

        Returns:
            A ``ProviderRegistry`` indexed by provider ID.

        Raises:
            KeyError: If two provider configs share the same ``id``.
        """
        cache_key = resources_dir.resolve()
        if cache_key in _registry_cache:
            return _registry_cache[cache_key]

        providers_dir = resources_dir / "providers"
        configs: dict[str, ProviderConfig] = {}

        if providers_dir.is_dir():
            for json_file in sorted(providers_dir.glob("*.json")):
                data = json.loads(json_file.read_text(encoding="utf-8"))
                config = cls._parse_config(data)
                if config.id in configs:
                    raise KeyError(
                        f"Duplicate provider id '{config.id}' (from {json_file} and another file)"
                    )
                configs[config.id] = config

        registry = cls(configs)
        _registry_cache[cache_key] = registry
        return registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, provider_id: str) -> ProviderConfig:
        """Return the ``ProviderConfig`` for *provider_id*.

        Args:
            provider_id: The unique provider identifier.

        Returns:
            The matching ``ProviderConfig``.

        Raises:
            KeyError: If no provider with *provider_id* exists.
        """
        try:
            return self._configs[provider_id]
        except KeyError:
            raise KeyError(
                f"No provider config found for id '{provider_id}'. "
                f"Available: {', '.join(sorted(self._configs))}"
            ) from None

    def list_ids(self) -> list[str]:
        """Return a sorted list of all registered provider IDs."""
        return sorted(self._configs.keys())

    # ------------------------------------------------------------------
    # Parsing helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_config(data: dict[str, Any]) -> ProviderConfig:
        """Parse a raw JSON dict into a ``ProviderConfig``.

        Args:
            data: Parsed JSON object for one provider.

        Returns:
            A fully-constructed ``ProviderConfig``.
        """
        connections = ProviderRegistry._parse_connections(data)
        return ProviderConfig(
            id=data["id"],
            name=data["name"],
            adapter=data["adapter"],
            base_url=data["base_url"],
            connections=connections,
            defaults=data.get("defaults"),
            extra_headers=data.get("extra_headers"),
            models_endpoint=data.get("models_endpoint"),
            model_discovery=ProviderRegistry._parse_model_discovery(data),
        )

    @staticmethod
    def _parse_model_discovery(data: dict[str, Any]) -> str:
        model_discovery = data.get("model_discovery")
        if model_discovery is None:
            adapter = data["adapter"]
            if not isinstance(adapter, str) or not adapter:
                raise ConfigError(
                    f"Provider '{data['id']}' field 'adapter' must be a non-empty string"
                )
            return DEFAULT_MODEL_DISCOVERY_BY_ADAPTER.get(adapter, "")
        if not isinstance(model_discovery, str) or not model_discovery:
            raise ConfigError(
                f"Provider '{data['id']}' field 'model_discovery' must be a non-empty string"
            )
        if model_discovery not in VALID_MODEL_DISCOVERY_STRATEGIES:
            raise ConfigError(
                f"Unknown model discovery strategy '{model_discovery}' for provider '{data['id']}'"
            )
        return model_discovery

    @staticmethod
    def _parse_connections(data: dict[str, Any]) -> list[ConnectionConfig]:
        provider_id = data["id"]
        connections: list[ConnectionConfig] = []
        seen_ids: set[str] = set()

        if "connections" not in data:
            raise ConfigError(f"Provider '{provider_id}' is missing required field 'connections'")

        for connection_data in data["connections"]:
            local_id = connection_data["id"]
            if local_id in seen_ids:
                raise KeyError(f"Duplicate connection id '{local_id}' for provider '{provider_id}'")
            seen_ids.add(local_id)

            connection_type = connection_data["type"]
            if connection_type not in VALID_CONNECTION_TYPES:
                raise ConfigError(
                    f"Unknown connection type '{connection_type}' for provider "
                    f"'{provider_id}' connection '{local_id}'"
                )

            auth_data = connection_data["auth"]
            credential_key = auth_data.get("credential_key", "")
            if connection_type == "api_key" and not credential_key:
                raise ConfigError(
                    f"Provider '{provider_id}' connection '{local_id}' api_key auth "
                    "requires 'credential_key'"
                )
            auth = AuthConfig(
                header=auth_data["header"],
                prefix=auth_data["prefix"],
                credential_key=credential_key,
            )
            oauth = ProviderRegistry._parse_oauth_config(provider_id, local_id, connection_data)
            connections.append(
                ConnectionConfig(
                    id=local_id,
                    type=connection_type,
                    label=connection_data["label"],
                    auth=auth,
                    base_url=connection_data.get("base_url"),
                    oauth=oauth,
                )
            )

        return connections

    @staticmethod
    def _parse_oauth_config(
        provider_id: str,
        local_id: str,
        connection_data: dict[str, Any],
    ) -> OAuthConfig | None:
        oauth_data = connection_data.get("oauth")
        if oauth_data is None:
            return None

        flow = oauth_data["flow"]
        if flow not in VALID_OAUTH_FLOWS:
            raise ConfigError(
                f"Unknown OAuth flow '{flow}' for provider '{provider_id}' connection '{local_id}'"
            )

        return OAuthConfig(
            flow=flow,
            client_id=oauth_data["client_id"],
            device_auth_url=oauth_data["device_auth_url"],
            token_url=oauth_data["token_url"],
            scopes=list(oauth_data.get("scopes", [])),
            token_exchange_url=oauth_data.get("token_exchange_url"),
        )
