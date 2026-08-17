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
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_POLICY,
    ReasoningReplayPolicy,
)
from core.utils.errors import ConfigError, ProviderError

_LOGGER = logging.getLogger("vbot.providers")
_PROVIDER_CONFIG_ERRORS = (
    ConfigError,
    KeyError,
    OSError,
    TypeError,
    UnicodeError,
    ValueError,
)

# Last-resort context-window floor, used when neither the model nor the
# provider config supplies a window (e.g. custom models and thin providers
# whose endpoint reports no window). Deliberately small and conservative:
# better to under-promise the budget — compaction triggers a little early,
# the token badge reads a little low — than to over-promise and let a real
# request blow past the model's true window. 8192 is a safe floor every
# modern chat model clears. This is a read-side FLOOR, never written into the
# catalog as a discovered fact (see ``resolve_context_window``).
GLOBAL_CONTEXT_WINDOW_FLOOR = 8192

# Default cap for the EFFECTIVE context window of flagged-local models (e.g.
# Ollama). A local endpoint reports the model's *theoretical* max (262k for an
# 8B model), which says nothing about the user's hardware — and Ollama silently
# truncates the prompt when the real window is smaller. Capping the effective
# window keeps assumption == reality: vBot budgets against this window AND
# requests exactly it from the local server (``options.num_ctx``). Per-model
# user overrides live in settings ``local_models.context_windows`` (see
# ``resolve_effective_context_window``).
LOCAL_CONTEXT_DEFAULT_CAP = 32768

# Tokenizers and Provider-side Tool framing differ slightly from vBot's shared
# request estimator. Keep both a request-relative and window-relative reserve,
# plus a small absolute floor, then use whichever is largest.
REQUEST_INPUT_ESTIMATE_RESERVE_RATIO = 0.25
REQUEST_CONTEXT_WINDOW_RESERVE_RATIO = 0.01
REQUEST_MIN_RESERVE_TOKENS = 256


def custom_provider_credential_key(provider_id: str) -> str:
    """Return the deterministic environment key for a normalized Custom Provider id."""

    return f"VBOT_CUSTOM_{provider_id.replace('-', '_').upper()}_API_KEY"


def resolve_request_output_limit(
    *,
    explicit_limit: Any,
    model_output_limit: Any,
    provider_default: Any,
    effective_context_window: int,
    estimated_input_tokens: int,
) -> int | None:
    """Resolve and context-clamp one request's output-token allowance.

    Precedence remains explicit positive caller limit, positive Model ceiling,
    then positive Provider default. The selected allowance is capped so the
    estimated messages, Tool definitions, uncertainty reserve, and output all
    fit inside the effective context window. A request whose input already
    leaves no positive output capacity fails locally instead of sending a known
    invalid Provider request.
    """

    requested = next(
        (
            value
            for value in (explicit_limit, model_output_limit, provider_default)
            if _positive_int(value) is not None
        ),
        None,
    )
    if requested is None:
        return None
    requested = int(requested)

    input_tokens = max(0, int(estimated_input_tokens))
    reserve = max(
        math.ceil(input_tokens * REQUEST_INPUT_ESTIMATE_RESERVE_RATIO),
        math.ceil(effective_context_window * REQUEST_CONTEXT_WINDOW_RESERVE_RATIO),
        REQUEST_MIN_RESERVE_TOKENS,
    )
    available_output = effective_context_window - input_tokens - reserve
    if available_output <= 0:
        raise ProviderError(
            "Request input leaves no output capacity in the Model context window "
            f"(estimated_input_tokens={input_tokens}, reserve_tokens={reserve}, "
            f"context_window={effective_context_window})",
            retryable=False,
        )
    return min(requested, available_output)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


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


VALID_CONNECTION_TYPES = frozenset({"api_key", "oauth", "none"})
VALID_OAUTH_FLOWS = frozenset({"device"})
STANDARD_DEVICE_FLOW = "oauth2"
OPENAI_CODEX_DEVICE_FLOW = "openai_codex"
MINIMAX_OAUTH_DEVICE_FLOW = "minimax_oauth"
NOUS_OAUTH_DEVICE_FLOW = "nous_oauth"
OPENCODE_OAUTH_DEVICE_FLOW = "opencode_oauth"
XAI_OAUTH_DEVICE_FLOW = "xai_oauth"
VALID_DEVICE_FLOWS = frozenset(
    {
        STANDARD_DEVICE_FLOW,
        OPENAI_CODEX_DEVICE_FLOW,
        MINIMAX_OAUTH_DEVICE_FLOW,
        NOUS_OAUTH_DEVICE_FLOW,
        OPENCODE_OAUTH_DEVICE_FLOW,
        XAI_OAUTH_DEVICE_FLOW,
    }
)
UNIX_MILLISECONDS_PER_SECOND = 1000
UNIX_MILLISECONDS_DETECTION_DIVISOR = 2


def resolve_minimax_oauth_expiry(value: Any, *, now: datetime) -> datetime:
    """Resolve MiniMax's ambiguous ``expired_in`` field to a UTC instant.

    MiniMax has returned this field both as a TTL in seconds and as an absolute
    Unix timestamp in milliseconds. A genuine TTL is many orders of magnitude
    below half the current Unix-millisecond timestamp, so the two shapes can be
    distinguished without guessing a fixed calendar cutoff.
    """

    if isinstance(value, bool):
        raise ValueError("MiniMax OAuth expired_in must be an integer")
    try:
        raw_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("MiniMax OAuth expired_in must be an integer") from exc
    if raw_value <= 0:
        raise ValueError("MiniMax OAuth expired_in must be positive")

    normalized_now = now.astimezone(UTC)
    now_milliseconds = int(normalized_now.timestamp() * UNIX_MILLISECONDS_PER_SECOND)
    if raw_value > now_milliseconds // UNIX_MILLISECONDS_DETECTION_DIVISOR:
        return datetime.fromtimestamp(raw_value / UNIX_MILLISECONDS_PER_SECOND, tz=UTC)
    return normalized_now + timedelta(seconds=raw_value)


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
        type: Connection kind. Supported values are ``"api_key"``, ``"oauth"``,
            and ``"none"`` (a keyless endpoint, e.g. a local server).
        label: Human-readable display label.
        auth: Authentication configuration for this connection. Empty for
            ``"none"`` connections, which need no credential.
        base_url: Optional provider base URL override for this connection.
        mode: Optional wire-variant selector freely interpreted by the
            provider adapter (e.g. ``"codex_responses"``). Per-connection;
            provider-level has no equivalent.
        models_endpoint: Optional per-connection discovery endpoint path
            (e.g. ``"/codex/models"``). Overrides the provider-level
            ``models_endpoint`` when set.
        auto_refresh: Whether the runtime refreshes this connection's model
            catalog automatically (at startup and on picker open, throttled).
            Meant for local endpoints (e.g. Ollama) whose installed-model set
            changes outside vBot; remote catalogs stay explicit-refresh only.
        catalog_requires_credentials: Whether catalog discovery requires the
            Connection credential. Defaults to ``True``; set ``False`` only
            when the Provider documents its model-list and detail endpoints as
            public while inference on the same Connection remains authenticated.
    """

    id: str
    type: str
    label: str
    auth: AuthConfig
    base_url: str | None = None
    oauth: OAuthConfig | None = None
    mode: str | None = None
    models_endpoint: str | None = None
    auto_refresh: bool = False
    catalog_requires_credentials: bool = True


def connection_default_enabled(connection: ConnectionConfig) -> bool:
    """Return whether *connection* is enabled when the user has not decided.

    Keyed connections (``api_key``/``oauth``) default to enabled — their
    credential requirement already gates them, so presence of a key/login is
    the opt-in. Keyless ``none`` connections (local endpoints like Ollama)
    default to disabled: they pass every credential gate, so without an
    explicit user opt-in vBot would probe and list a service the user may
    never run. Per-connection user overrides live in settings
    ``providers.connections`` and win over this default.
    """

    return connection.type != "none"


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
        catalog_exclusions: Exact wire model ids a provider's listing endpoint
            advertises but cannot currently serve. Discovery retains them in
            the raw inspection dump and omits them from the usable projection.
        context_window: Optional per-provider read-side default context window,
            applied when a model on this provider has no window of its own
            (``Model.context_window is None``). This is a READ-SIDE FACT
            default, not a request-shaping default — a provider whose endpoint
            reliably reports no window (e.g. a thin gateway) can name one sane
            window here so its models resolve a usable budget instead of falling
            all the way to the global floor. Distinct from ``defaults`` (which
            holds request-shaping params like ``max_tokens``). Consumed by
            :func:`resolve_context_window`.
        reasoning_replay: Effective Provider-level native Reasoning replay
            policy. Runtime overlays the optional value from the Provider's
            Model-DB Override file; direct construction uses the system
            ``full_history`` default.
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
    catalog_exclusions: frozenset[str] = frozenset()
    custom: bool = False
    reasoning_replay: ReasoningReplayPolicy = DEFAULT_REASONING_REPLAY_POLICY

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


def model_is_local(model_metadata: Mapping[str, Any] | None) -> bool:
    """Return whether a model is flagged as running on local hardware.

    Locality is a model fact stamped at refresh into the provider-scoped
    metadata blob (``metadata.<provider>.local: true`` — Ollama today, any
    future local provider the same way). The check is provider-agnostic: any
    provider blob carrying ``local: true`` flags the model.
    """

    if not model_metadata:
        return False
    for provider_blob in model_metadata.values():
        if isinstance(provider_blob, Mapping) and provider_blob.get("local") is True:
            return True
    return False


def resolve_effective_context_window(
    model_context_window: int | None,
    provider_config: ProviderConfig | None,
    *,
    model_metadata: Mapping[str, Any] | None = None,
    model_key: str = "",
    local_context_windows: Mapping[str, Any] | None = None,
) -> int:
    """Resolve the context window vBot actually budgets against.

    For models NOT flagged local this is exactly :func:`resolve_context_window`
    (trust the reported window — applies to remote providers, proxied ``:cloud``
    models, and the direct cloud connection alike).

    For flagged-local models (see :func:`model_is_local`) the *theoretical* max
    the endpoint reports is not trusted as a budget. The effective window is:

    1. The user-configured value from settings ``local_models.context_windows``
       (keyed ``"<provider>/<model_id>"``), when positive.
    2. Else ``min(LOCAL_CONTEXT_DEFAULT_CAP, resolved chain value)`` — the
       conservative default that fits commodity hardware.

    The same value is enforced on the wire (Ollama ``options.num_ctx``), used
    for compaction budgeting, ``/status``, and the picker suitability filter,
    so assumption and reality never drift apart.

    Args:
        model_context_window: The model's raw ``context_window`` fact.
        provider_config: The model's provider config (for the default chain).
        model_metadata: The model's ``metadata`` blob (locality flag source).
        model_key: The settings key ``"<provider>/<model_id>"``.
        local_context_windows: The live ``local_models.context_windows`` map.
    """

    if not model_is_local(model_metadata):
        return resolve_context_window(model_context_window, provider_config)

    configured = (local_context_windows or {}).get(model_key)
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        return configured
    return min(
        LOCAL_CONTEXT_DEFAULT_CAP,
        resolve_context_window(model_context_window, provider_config),
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
    def load(
        cls,
        resources_dir: Path,
        *,
        custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
        tolerate_invalid: bool = False,
    ) -> ProviderRegistry:
        """Read bundled Provider JSON and overlay optional Custom Providers.

        The first call for a given *resources_dir* reads and parses every
        ``resources/providers/*.json`` file.  Subsequent calls with the same
        directory return the cached bundled-only instance without touching disk
        again. A supplied Custom Provider mapping always gets an isolated
        registry because it belongs to one data directory and must never mutate
        another Runtime that uses the same bundled resources.

        Args:
            resources_dir: Path to the ``resources/`` root directory.

        Returns:
            A ``ProviderRegistry`` indexed by provider ID.

        Raises:
            KeyError: If two provider configs share the same ``id``.
        """
        if custom_providers is not None or tolerate_invalid:
            return cls(
                cls._assemble_configs(
                    resources_dir,
                    custom_providers or {},
                    tolerate_invalid=tolerate_invalid,
                )
            )

        cache_key = resources_dir.resolve()
        if cache_key in _registry_cache:
            return _registry_cache[cache_key]

        configs = cls._assemble_configs(
            resources_dir,
            {},
            tolerate_invalid=tolerate_invalid,
        )
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

    def reload(
        self,
        resources_dir: Path,
        *,
        custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
        tolerate_invalid: bool = False,
    ) -> None:
        """Reload bundled and Custom Provider configs in place."""

        resolved = resources_dir.resolve()
        self._configs = self._assemble_configs(
            resources_dir,
            custom_providers or {},
            tolerate_invalid=tolerate_invalid,
        )
        if custom_providers is None and not tolerate_invalid:
            _registry_cache[resolved] = self

    @classmethod
    def _assemble_configs(
        cls,
        resources_dir: Path,
        custom_providers: Mapping[str, Mapping[str, Any]],
        *,
        tolerate_invalid: bool = False,
    ) -> dict[str, ProviderConfig]:
        providers_dir = resources_dir / "providers"
        configs: dict[str, ProviderConfig] = {}

        if providers_dir.is_dir():
            try:
                provider_files = sorted(providers_dir.glob("*.json"))
            except OSError as exc:
                if not tolerate_invalid:
                    raise
                _LOGGER.warning("Could not scan Provider configs in '%s': %s", providers_dir, exc)
                provider_files = []

            for json_file in provider_files:
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ConfigError("Provider config root must be an object")
                    config = cls._parse_config(data)
                    if config.id in configs:
                        raise KeyError(
                            f"Duplicate provider id '{config.id}' "
                            f"(from {json_file} and another file)"
                        )
                except _PROVIDER_CONFIG_ERRORS as exc:
                    if not tolerate_invalid:
                        raise
                    _LOGGER.warning("Ignoring invalid Provider config '%s': %s", json_file, exc)
                    continue
                configs[config.id] = config

        for provider_id, custom_provider in sorted(custom_providers.items()):
            try:
                if provider_id in configs:
                    raise ConfigError(
                        f"Custom Provider id '{provider_id}' conflicts with a bundled Provider"
                    )
                config = cls._parse_config(
                    cls._custom_config_data(provider_id, custom_provider),
                    custom=True,
                )
            except _PROVIDER_CONFIG_ERRORS as exc:
                if not tolerate_invalid:
                    raise
                _LOGGER.warning("Ignoring invalid Custom Provider '%s': %s", provider_id, exc)
                continue
            configs[config.id] = config
        return configs

    @staticmethod
    def _custom_config_data(
        provider_id: str,
        provider: Mapping[str, Any],
    ) -> dict[str, Any]:
        auth_type = provider["auth"]
        connection: dict[str, Any] = {
            "id": "default",
            "type": auth_type,
            "label": "Default",
        }
        if auth_type == "api_key":
            credential_key = custom_provider_credential_key(provider_id)
            connection["auth"] = {
                "header": "Authorization",
                "prefix": "Bearer ",
                "credential_key": credential_key,
            }
        return {
            "id": provider_id,
            "name": provider["name"],
            "adapter": provider["adapter"],
            "base_url": provider["base_url"],
            "connections": [connection],
            "defaults": dict(provider.get("defaults", {})),
            "models_endpoint": provider.get("models_endpoint"),
        }

    # ------------------------------------------------------------------
    # Parsing helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_config(data: dict[str, Any], *, custom: bool = False) -> ProviderConfig:
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
        raw_catalog_exclusions = data.get("catalog_exclusions", [])
        if not isinstance(raw_catalog_exclusions, list) or not all(
            isinstance(model_id, str) and model_id.strip() for model_id in raw_catalog_exclusions
        ):
            raise ConfigError(
                f"Provider '{provider_id}' catalog_exclusions must be a list "
                "of non-empty model-id strings"
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
            catalog_exclusions=frozenset(model_id.strip() for model_id in raw_catalog_exclusions),
            custom=custom,
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

            auth = ProviderRegistry._parse_auth(
                provider_id, local_id, connection_type, connection_data
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

            auto_refresh = connection_data.get("auto_refresh", False)
            if not isinstance(auto_refresh, bool):
                raise ConfigError(
                    f"Provider '{provider_id}' connection '{local_id}' auto_refresh "
                    f"must be a boolean when set, got {type(auto_refresh).__name__}"
                )

            catalog_requires_credentials = connection_data.get("catalog_requires_credentials", True)
            if not isinstance(catalog_requires_credentials, bool):
                raise ConfigError(
                    f"Provider '{provider_id}' connection '{local_id}' "
                    "catalog_requires_credentials must be a boolean when set, "
                    f"got {type(catalog_requires_credentials).__name__}"
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
                    auto_refresh=auto_refresh,
                    catalog_requires_credentials=catalog_requires_credentials,
                )
            )

        return connections

    @staticmethod
    def _parse_auth(
        provider_id: str,
        local_id: str,
        connection_type: str,
        connection_data: dict[str, Any],
    ) -> AuthConfig:
        auth_data = connection_data.get("auth")
        if auth_data is None:
            # Keyless connections need no auth block; every other type does.
            if connection_type == "none":
                return AuthConfig(header="", prefix="", credential_key="")
            raise ConfigError(
                f"Provider '{provider_id}' connection '{local_id}' is missing required field 'auth'"
            )

        credential_key = auth_data.get("credential_key", "")
        if connection_type == "api_key" and not credential_key:
            raise ConfigError(
                f"Provider '{provider_id}' connection '{local_id}' api_key auth "
                "requires 'credential_key'"
            )
        if connection_type == "none":
            return AuthConfig(
                header=auth_data.get("header", ""),
                prefix=auth_data.get("prefix", ""),
                credential_key=credential_key,
            )
        return AuthConfig(
            header=auth_data["header"],
            prefix=auth_data["prefix"],
            credential_key=credential_key,
        )

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
