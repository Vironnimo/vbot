"""Usage types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    ConnectionRef,
    compose_connection_id,
)
from core.providers.token_getter import (
    TokenGetter,
)

OPENAI_USAGE_CONNECTION = "openai:subscription"

COPILOT_USAGE_CONNECTION = "github-copilot:oauth"

OLLAMA_USAGE_CONNECTION = "ollama-cloud:api-key"

MINIMAX_USAGE_CONNECTION = "minimax:api-key"

OPENROUTER_USAGE_CONNECTION = "openrouter:api-key"

OPENAI_USAGE_PATH = "/wham/usage"

COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"

OLLAMA_USAGE_PATH = "/api/usage"

MINIMAX_USAGE_PATH = "/token_plan/remains"

OPENROUTER_CREDITS_PATH = "/credits"

OPENROUTER_KEY_PATH = "/key"

_MINIMAX_TOTAL_KEYS = ("current_interval_total_count", "total_count", "total")

_MINIMAX_REMAINING_KEYS = (
    "current_interval_remain_count",
    "current_interval_usage",
    "remain_count",
    "remaining",
)

_MINIMAX_RESET_KEYS = ("current_interval_end", "next_reset_time", "reset_at", "reset_time")

_MINIMAX_PLAN_KEYS = ("plan", "plan_name", "subscription_type")

_MINIMAX_CHAT_MODEL_PREFIX = "minimax-m"

_WEEK_SECONDS = 7 * 24 * 3600

_OLLAMA_SESSION_SECONDS = 5 * 3600

_DAY_SECONDS = 24 * 3600

_EPOCH_MILLISECONDS_THRESHOLD = 1_000_000_000_000

_PRIMARY_FALLBACK_LABEL = "Limit"

_SECONDARY_FALLBACK_LABEL = "Weekly"

_RATIO_PERCENT_DECIMAL_PLACES = 10


@dataclass(frozen=True)
class UsageWindow:
    """One provider usage window (e.g. the rolling 5h or weekly limit)."""

    label: str
    used_percent: float
    reset_at: str | None = None
    window_seconds: int | None = None
    used_units: float | None = None
    remaining_units: float | None = None
    total_units: float | None = None
    unit: str | None = None
    unlimited: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable form of this window."""

        return {
            "label": self.label,
            "used_percent": self.used_percent,
            "reset_at": self.reset_at,
            "window_seconds": self.window_seconds,
            "used_units": self.used_units,
            "remaining_units": self.remaining_units,
            "total_units": self.total_units,
            "unit": self.unit,
            "unlimited": self.unlimited,
        }


@dataclass(frozen=True)
class UsageCredits:
    """Structured subscription-credit state when a Provider exposes it."""

    enabled: bool
    balance: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable credit projection."""

        return {"enabled": self.enabled, "balance": self.balance}


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    """Per-connection usage state, or a clean error/unavailable marker."""

    connection: str
    account: str
    display_name: str
    plan: str | None = None
    windows: list[UsageWindow] = field(default_factory=list)
    credits: UsageCredits | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable form of this snapshot."""

        return {
            "connection": self.connection,
            "account": self.account,
            "display_name": self.display_name,
            "plan": self.plan,
            "windows": [window.to_dict() for window in self.windows],
            "credits": self.credits.to_dict() if self.credits is not None else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class UsageReport:
    """All per-connection usage snapshots for one on-demand fetch."""

    generated_at: str
    providers: list[ProviderUsageSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable form of this report."""

        return {
            "generated_at": self.generated_at,
            "providers": [snapshot.to_dict() for snapshot in self.providers],
        }


class UsageFetchError(Exception):
    """A fetcher failure carrying a short, user-safe message (no token data)."""


class _ProviderLookupProtocol(Protocol):
    def get(self, provider_id: str) -> Any: ...


class _ProviderCredentialsProtocol(Protocol):
    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool: ...

    def resolve_account_id(
        self,
        provider_id: str,
        local_connection_id: str,
        account_id: str | None = None,
    ) -> str: ...


class UsageProbeRuntime(Protocol):
    """The narrow runtime surface the usage probe needs.

    Defined locally (not imported from ``core.runtime.interfaces``) so this
    module never imports ``core.runtime`` — a runtime import would pull in the
    full ``Runtime`` bootstrap and create an import cycle.
    """

    @property
    def providers(self) -> _ProviderLookupProtocol: ...

    @property
    def provider_credentials(self) -> _ProviderCredentialsProtocol: ...

    def get_connection_token_getter(self, connection: ConnectionRef) -> TokenGetter: ...

    def get_connection_token_extra(self, connection: ConnectionRef) -> Mapping[str, str]: ...


class UsageResponse(Protocol):
    """Minimal HTTP response surface used by the fetchers (``httpx.Response``)."""

    @property
    def status_code(self) -> int: ...

    def json(self) -> Any: ...

    @property
    def text(self) -> str: ...


class UsageTransport(Protocol):
    """Async HTTP GET surface, injected so tests never touch the network."""

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        params: Mapping[str, str] | None = None,
    ) -> UsageResponse: ...


@dataclass(frozen=True)
class _SupportedConnection:
    """A connection the probe knows how to query."""

    provider_id: str
    local_connection_id: str
    account_id: str = DEFAULT_ACCOUNT_ID

    @property
    def connection_id(self) -> str:
        return f"{self.provider_id}:{self.local_connection_id}"

    @property
    def target_id(self) -> str:
        """Return the exact Connection+Account target used for credentials/cache."""

        return compose_connection_id(
            self.provider_id,
            self.local_connection_id,
            self.account_id,
        )

    @property
    def ref(self) -> ConnectionRef:
        """Return the exact target as a Runtime-seam connection reference."""

        return ConnectionRef(self.provider_id, self.target_id)
