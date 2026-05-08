"""Provider configuration dataclass and registry.

A ``ProviderConfig`` holds the static settings that distinguish one provider
from another: base URL, auth mechanism, default parameters, and optional
extra headers.  Configs are frozen (immutable) and loaded from JSON files
under ``resources/providers/``.

``ProviderRegistry`` reads every ``.json`` file in that directory, parses
each into a ``ProviderConfig``, and indexes them by provider ID.  Loading
is cached — the second call with the same *resources_dir* returns the same
registry instance without re-reading disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    credential_key: str

    @property
    def env_key(self) -> str:
        """Return the configured credential identifier.

        Runtime and server credential-source migration lands in later phases.
        Until then, existing callers still reading ``auth.env_key`` resolve the
        same identifier value through this compatibility property.
        """

        return self.credential_key


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
        auth: Authentication configuration.
        defaults: Optional default request parameters (e.g. ``max_tokens``,
            ``temperature``).
        extra_headers: Optional provider-specific HTTP headers.
        models_endpoint: Optional path to the models listing endpoint
            (e.g. ``"/models"``).  Reserved for future dynamic model refresh.
    """

    id: str
    name: str
    adapter: str
    base_url: str
    auth: AuthConfig
    defaults: dict[str, Any] | None = None
    extra_headers: dict[str, str] | None = None
    models_endpoint: str | None = None


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
        auth_data = data["auth"]
        auth = AuthConfig(
            header=auth_data["header"],
            prefix=auth_data["prefix"],
            credential_key=auth_data["credential_key"],
        )
        return ProviderConfig(
            id=data["id"],
            name=data["name"],
            adapter=data["adapter"],
            base_url=data["base_url"],
            auth=auth,
            defaults=data.get("defaults"),
            extra_headers=data.get("extra_headers"),
            models_endpoint=data.get("models_endpoint"),
        )
