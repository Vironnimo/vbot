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

# Last-resort context-window floor, used when neither the model nor the
# provider config supplies a window (e.g. custom models and thin providers
# whose endpoint reports no window). Deliberately small and conservative:
# better to under-promise the budget — compaction triggers a little early,
# the token badge reads a little low — than to over-promise and let a real
# request blow past the model's true window. 8192 is a safe floor every
# modern chat model clears. This is a read-side FLOOR, never written into the
# catalog as a discovered fact (see ``resolve_context_window``).
GLOBAL_CONTEXT_WINDOW_FLOOR = 8192

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
STANDARD_DEVICE_FLOW = "oauth2"
OPENAI_CODEX_DEVICE_FLOW = "openai_codex"
VALID_DEVICE_FLOWS = frozenset({STANDARD_DEVICE_FLOW, OPENAI_CODEX_DEVICE_FLOW})


@dataclass(frozen=True)
class OAuthConfig:
    """OAuth flow metadata for a provider connection."""

    flow: str
    client_id: str
    device_auth_url: str
    token_url: str
    scopes: list[str]
    token_exchange_url: str | None = None
    device_flow: str = STANDARD_DEVICE_FLOW
    verification_uri: str | None = None
    redirect_uri: str | None = None
    expires_in: int | None = None


@dataclass(frozen=True)
class ConnectionConfig:
    """Authentication connection configuration for a provider.

    Attributes:
        id: Local connection identifier within the provider config.
        type: Connection kind. Supported values are ``"api_key"`` and ``"oauth"``.
        label: Human-readable display label.
        auth: Authentication configuration for this connection.
        base_url: Optional provider base URL override for this connection.
        mode: Optional wire-variant selector freely interpreted by the
            provider adapter (e.g. ``"codex_responses"``). Per-connection;
            provider-level has no equivalent.
        models_endpoint: Optional per-connection discovery endpoint path
            (e.g. ``"/codex/models"``). Overrides the provider-level
            ``models_endpoint`` when set.
    """

    id: str
    type: str
    label: str
    auth: AuthConfig
    base_url: str | None = None
    oauth: OAuthConfig | None = None
    mode: str | None = None
    models_endpoint: str | None = None


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
        models_dev_id: Optional models.dev provider key for this vBot provider
            (e.g. vBot ``"opencode-go"`` may map to models.dev ``"opencode"``).
            When absent, the vBot provider id is the models.dev id (the common
            case). Used by the refresh-time lift mechanism to find a provider's
            section inside the models.dev catalog; the at-load canonical join
            does *not* depend on it. Read via :meth:`effective_models_dev_id`.
        context_window: Optional per-provider read-side default context window,
            applied when a model on this provider has no window of its own
            (``Model.context_window is None``). This is a READ-SIDE FACT
            default, not a request-shaping default — a provider whose endpoint
            reliably reports no window (e.g. a thin gateway) can name one sane
            window here so its models resolve a usable budget instead of falling
            all the way to the global floor. Distinct from ``defaults`` (which
            holds request-shaping params like ``max_tokens``). Consumed by
            :func:`resolve_context_window`.
    """

    id: str
    name: str
    adapter: str
    base_url: str
    connections: list[ConnectionConfig] = field(default_factory=list)
    defaults: dict[str, Any] | None = None
    extra_headers: dict[str, str] | None = None
    models_endpoint: str | None = None
    models_dev_id: str | None = None
    context_window: int | None = None

    def effective_models_dev_id(self) -> str:
        """Return the models.dev provider key for this provider.

        Falls back to ``self.id`` when ``models_dev_id`` is not set — the
        common case where the vBot provider id already matches models.dev.
        """

        return self.models_dev_id or self.id

    def get_connection(self, local_id: str) -> ConnectionConfig:
        """Return a connection by its local provider-scoped ID."""

        for connection in self.connections:
            if connection.id == local_id:
                return connection
        raise KeyError(
            f"No connection config found for id '{local_id}' on provider '{self.id}'. "
            f"Available: {', '.join(connection.id for connection in self.connections)}"
        )


def resolve_context_window(
    model_context_window: int | None,
    provider_config: ProviderConfig | None,
) -> int:
    """Resolve a usable context window through the read-side default chain.

    The single source of truth for "what window do we actually use" so no
    read-side caller (compaction, token budget, ``/status``, the agent payload)
    re-implements the chain. A missing fact stays missing in the data
    (``Model.context_window is None``); this fills the gap at use time only:

    1. The model's own window when it is a positive int (the discovered fact).
    2. Else the provider config's ``context_window`` default when positive
       (a per-provider read-side default for thin/window-less endpoints).
    3. Else :data:`GLOBAL_CONTEXT_WINDOW_FLOOR` — the conservative last resort
       that keeps custom and window-less models alive.

    Non-positive values at any layer (a stray ``0`` from an old catalog, a
    misconfigured provider default) are treated as "unknown" and skipped, so a
    fake ``0`` can never reach a caller as a real budget. The return is always a
    positive int, so callers never divide by zero or render a NaN.

    Args:
        model_context_window: The model's ``context_window`` (``None`` / a
            non-positive stray means "unknown").
        provider_config: The provider config of the model's provider, or
            ``None`` when it cannot be resolved (custom/unknown provider).

    Returns:
        A positive context window to use downstream.
    """

    if model_context_window is not None and model_context_window > 0:
        return model_context_window
    if provider_config is not None:
        provider_default = provider_config.context_window
        if provider_default is not None and provider_default > 0:
            return provider_default
    return GLOBAL_CONTEXT_WINDOW_FLOOR


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
        provider_id = data["id"]
        if ":" in provider_id:
            raise ConfigError(f"Provider id '{provider_id}' must not contain ':'")
        connections = ProviderRegistry._parse_connections(data)
        models_dev_id = data.get("models_dev_id")
        if models_dev_id is not None and not isinstance(models_dev_id, str):
            raise ConfigError(
                f"Provider '{provider_id}' models_dev_id must be a string when set, "
                f"got {type(models_dev_id).__name__}"
            )
        context_window = data.get("context_window")
        if context_window is not None and (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window <= 0
        ):
            raise ConfigError(
                f"Provider '{provider_id}' context_window must be a positive integer when set"
            )
        return ProviderConfig(
            id=provider_id,
            name=data["name"],
            adapter=data["adapter"],
            base_url=data["base_url"],
            connections=connections,
            defaults=data.get("defaults"),
            extra_headers=data.get("extra_headers"),
            models_endpoint=data.get("models_endpoint"),
            models_dev_id=models_dev_id,
            context_window=context_window,
        )

    @staticmethod
    def _parse_connections(data: dict[str, Any]) -> list[ConnectionConfig]:
        provider_id = data["id"]
        connections: list[ConnectionConfig] = []
        seen_ids: set[str] = set()

        if "connections" not in data:
            raise ConfigError(f"Provider '{provider_id}' is missing required field 'connections'")

        for connection_data in data["connections"]:
            local_id = connection_data["id"]
            if "--" in local_id or ":" in local_id:
                raise ConfigError(
                    f"Provider '{provider_id}' connection id '{local_id}' must not "
                    "contain '--' or ':'"
                )
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

            mode = connection_data.get("mode")
            if mode is not None and not isinstance(mode, str):
                raise ConfigError(
                    f"Provider '{provider_id}' connection '{local_id}' mode "
                    f"must be a string when set, got {type(mode).__name__}"
                )

            models_endpoint = connection_data.get("models_endpoint")
            if models_endpoint is not None and not isinstance(models_endpoint, str):
                raise ConfigError(
                    f"Provider '{provider_id}' connection '{local_id}' models_endpoint "
                    f"must be a string when set, got {type(models_endpoint).__name__}"
                )

            connections.append(
                ConnectionConfig(
                    id=local_id,
                    type=connection_type,
                    label=connection_data["label"],
                    auth=auth,
                    base_url=connection_data.get("base_url"),
                    oauth=oauth,
                    mode=mode,
                    models_endpoint=models_endpoint,
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

        device_flow = oauth_data.get("device_flow", STANDARD_DEVICE_FLOW)
        if not isinstance(device_flow, str) or device_flow not in VALID_DEVICE_FLOWS:
            raise ConfigError(
                f"Unknown OAuth device_flow '{device_flow}' for provider "
                f"'{provider_id}' connection '{local_id}'"
            )

        expires_in = oauth_data.get("expires_in")
        if expires_in is not None and (
            isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0
        ):
            raise ConfigError(
                f"Provider '{provider_id}' connection '{local_id}' OAuth expires_in "
                "must be a positive integer"
            )

        return OAuthConfig(
            flow=flow,
            client_id=oauth_data["client_id"],
            device_auth_url=oauth_data["device_auth_url"],
            token_url=oauth_data["token_url"],
            scopes=list(oauth_data.get("scopes", [])),
            token_exchange_url=oauth_data.get("token_exchange_url"),
            device_flow=device_flow,
            verification_uri=oauth_data.get("verification_uri"),
            redirect_uri=oauth_data.get("redirect_uri"),
            expires_in=expires_in,
        )
