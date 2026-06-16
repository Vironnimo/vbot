"""Live provider subscription-usage probe.

This module fetches each logged-in provider's *own* current usage windows
(e.g. a 5h / weekly percentage used, with a reset time and plan) from the
provider's dedicated usage endpoint — no chat request needed. It is **live
provider state**, deliberately separate from ``core.statistics`` (which is a
read-only aggregation over persisted Sessions and never touches the network).

The probe lives inside the providers domain because it owns provider-domain
knowledge: endpoints, auth, and wire shapes. It follows the
``core.providers.task_client`` precedent — a non-chat provider HTTP client that
takes a narrow, locally-defined runtime protocol so it never imports
``core.runtime`` (import-cycle risk) and never caches raw OAuth access tokens.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from core.providers.openai import CODEX_EXTRA_HEADERS
from core.providers.openai_subscription_auth import (
    CHATGPT_ACCOUNT_ID_EXTRA_KEY,
    extract_chatgpt_account_id,
)
from core.providers.token_getter import (
    COPILOT_EDITOR_VERSION,
    COPILOT_INTEGRATION_ID,
    GITHUB_OAUTH_TOKEN_EXTRA_KEY,
    TokenGetter,
)
from core.utils.errors import ConfigError
from core.utils.logging import get_logger

_LOGGER = get_logger("providers.usage")

DEFAULT_USAGE_TIMEOUT_SECONDS = 8.0
DEFAULT_USAGE_CACHE_TTL_SECONDS = 60.0

OPENAI_USAGE_CONNECTION = "openai:subscription"
COPILOT_USAGE_CONNECTION = "github-copilot:oauth"
MINIMAX_USAGE_CONNECTION = "minimax:api-key"

OPENAI_USAGE_PATH = "/wham/usage"
# GitHub's own host, not the Copilot API host: the usage endpoint authenticates
# with the GitHub OAuth token (token-store ``extra``), not the Copilot bearer.
COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"
MINIMAX_USAGE_PATH = "/token_plan/remains"

# Candidate field names for the MiniMax remaining/total counts. The shape is
# implemented blind from openclaw's verified field names (no live credentials);
# the candidate lists keep parsing tolerant and the snapshot degrades to an
# "unsupported" error rather than crashing on a mismatch.
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
_DAY_SECONDS = 24 * 3600
# Epoch values above this are milliseconds, not seconds (year ~33658 in seconds).
_EPOCH_MILLISECONDS_THRESHOLD = 1_000_000_000_000
_PRIMARY_FALLBACK_LABEL = "Limit"
_SECONDARY_FALLBACK_LABEL = "Weekly"


# ---------------------------------------------------------------------------
# Common normalized shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageWindow:
    """One provider usage window (e.g. the rolling 5h or weekly limit)."""

    label: str
    used_percent: float
    reset_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable form of this window."""

        return {
            "label": self.label,
            "used_percent": self.used_percent,
            "reset_at": self.reset_at,
        }


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    """Per-connection usage state, or a clean error/unavailable marker."""

    connection: str
    display_name: str
    plan: str | None = None
    windows: list[UsageWindow] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable form of this snapshot."""

        return {
            "connection": self.connection,
            "display_name": self.display_name,
            "plan": self.plan,
            "windows": [window.to_dict() for window in self.windows],
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


# ---------------------------------------------------------------------------
# Runtime / transport injection surfaces
# ---------------------------------------------------------------------------


class _ProviderLookupProtocol(Protocol):
    def get(self, provider_id: str) -> Any: ...


class _ProviderCredentialsProtocol(Protocol):
    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool: ...


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

    def get_connection_token_getter(self, provider_id: str, connection_id: str) -> TokenGetter: ...

    def get_connection_token_extra(
        self, provider_id: str, connection_id: str
    ) -> Mapping[str, str]: ...


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


class HttpxUsageTransport:
    """Default :class:`UsageTransport` backed by a per-request ``httpx`` client."""

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        params: Mapping[str, str] | None = None,
    ) -> UsageResponse:
        """Issue one GET, mapping transport failures to a clean fetch error."""

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                return await client.get(
                    url,
                    headers=dict(headers),
                    params=dict(params) if params else None,
                )
            except httpx.TransportError as exc:
                raise UsageFetchError("Network error") from exc


@dataclass(frozen=True)
class _SupportedConnection:
    """A connection the probe knows how to query."""

    provider_id: str
    local_connection_id: str

    @property
    def connection_id(self) -> str:
        return f"{self.provider_id}:{self.local_connection_id}"


_SUPPORTED_CONNECTIONS: tuple[_SupportedConnection, ...] = (
    _SupportedConnection("openai", "subscription"),
    _SupportedConnection("github-copilot", "oauth"),
    _SupportedConnection("minimax", "api-key"),
)

_Fetcher = Callable[[_SupportedConnection], Awaitable[ProviderUsageSnapshot]]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProviderUsageService:
    """Fetch normalized live usage windows for logged-in provider connections."""

    def __init__(
        self,
        runtime: UsageProbeRuntime,
        *,
        transport: UsageTransport | None = None,
        timeout: float = DEFAULT_USAGE_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_USAGE_CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._transport = transport or HttpxUsageTransport()
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._monotonic = monotonic
        self._cache: dict[str, tuple[float, ProviderUsageSnapshot]] = {}
        # Only connections with a registered fetcher are queried.
        self._fetchers: dict[str, _Fetcher] = {
            OPENAI_USAGE_CONNECTION: self._fetch_openai,
            COPILOT_USAGE_CONNECTION: self._fetch_copilot,
            MINIMAX_USAGE_CONNECTION: self._fetch_minimax,
        }

    async def report(self, connections: list[str] | None = None) -> UsageReport:
        """Return usage snapshots for every supported, logged-in connection.

        Fetchers run concurrently; a single fetcher failure (timeout, HTTP
        error, or shape mismatch) becomes that snapshot's ``error`` and never
        breaks its siblings (fail-open). Connections with neither a window nor a
        meaningful error are dropped from the report.
        """

        requested = set(connections) if connections is not None else None
        targets = self._select_targets(requested)
        snapshots = await asyncio.gather(*(self._snapshot_for(target) for target in targets))
        meaningful = [snapshot for snapshot in snapshots if _is_meaningful(snapshot)]
        return UsageReport(generated_at=_now_iso(), providers=meaningful)

    def _select_targets(self, requested: set[str] | None) -> list[_SupportedConnection]:
        targets: list[_SupportedConnection] = []
        for connection in _SUPPORTED_CONNECTIONS:
            if connection.connection_id not in self._fetchers:
                continue
            if requested is not None and connection.connection_id not in requested:
                continue
            if not self._connection_usable(connection):
                continue
            targets.append(connection)
        return targets

    def _connection_usable(self, connection: _SupportedConnection) -> bool:
        try:
            return self._runtime.provider_credentials.has_credentials(
                connection.provider_id, connection.connection_id
            )
        except (KeyError, ConfigError):
            return False

    async def _snapshot_for(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        cached = self._cached_snapshot(connection.connection_id)
        if cached is not None:
            return cached
        snapshot = await self._run_fetcher(connection)
        self._store_cache(connection.connection_id, snapshot)
        return snapshot

    async def _run_fetcher(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        fetcher = self._fetchers[connection.connection_id]
        try:
            return await asyncio.wait_for(fetcher(connection), timeout=self._timeout)
        except TimeoutError:
            return self._error_snapshot(connection, "Timeout")
        except UsageFetchError as exc:
            return self._error_snapshot(connection, str(exc))
        except Exception as exc:  # noqa: BLE001 — fail-open: one fetcher must not break siblings
            # Blind Copilot/MiniMax parsing may raise unexpected shapes; convert
            # any non-fetch error into a clean "unavailable" snapshot and log the
            # real cause for debugging (no token data is included in the message).
            _LOGGER.warning(
                "Usage fetch failed for connection %s: %s", connection.connection_id, exc
            )
            return self._error_snapshot(connection, "Unavailable")

    def _error_snapshot(
        self, connection: _SupportedConnection, message: str
    ) -> ProviderUsageSnapshot:
        return ProviderUsageSnapshot(
            connection=connection.connection_id,
            display_name=self._display_name(connection),
            error=message,
        )

    def _display_name(self, connection: _SupportedConnection) -> str:
        try:
            name = self._runtime.providers.get(connection.provider_id).name
        except (KeyError, AttributeError):
            return connection.provider_id
        return name if isinstance(name, str) and name else connection.provider_id

    def _cached_snapshot(self, connection_id: str) -> ProviderUsageSnapshot | None:
        entry = self._cache.get(connection_id)
        if entry is None:
            return None
        stored_at, snapshot = entry
        if self._monotonic() - stored_at > self._cache_ttl:
            return None
        return snapshot

    def _store_cache(self, connection_id: str, snapshot: ProviderUsageSnapshot) -> None:
        self._cache[connection_id] = (self._monotonic(), snapshot)

    # ------------------------------------------------------------------
    # Per-provider fetchers
    # ------------------------------------------------------------------

    async def _fetch_openai(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        provider = self._runtime.providers.get(connection.provider_id)
        connection_config = provider.get_connection(connection.local_connection_id)
        base_url = connection_config.base_url or provider.base_url

        token_getter = self._runtime.get_connection_token_getter(
            connection.provider_id, connection.connection_id
        )
        token = await token_getter()
        account_id = extract_chatgpt_account_id(token)
        if not account_id:
            extra = self._runtime.get_connection_token_extra(
                connection.provider_id, connection.connection_id
            )
            account_id = extra.get(CHATGPT_ACCOUNT_ID_EXTRA_KEY) or None
        if not account_id:
            raise UsageFetchError("Reconnect required")

        headers = {
            connection_config.auth.header: f"{connection_config.auth.prefix}{token}",
            "chatgpt-account-id": account_id,
            **CODEX_EXTRA_HEADERS,
        }
        body = await self._get_json(_join_url(base_url, OPENAI_USAGE_PATH), headers)
        return _parse_openai_usage(connection.connection_id, self._display_name(connection), body)

    async def _fetch_copilot(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        # The Copilot usage endpoint authenticates with the GitHub OAuth token
        # (token-store ``extra``) under GitHub's ``token`` scheme — NOT the
        # exchanged Copilot bearer.
        extra = self._runtime.get_connection_token_extra(
            connection.provider_id, connection.connection_id
        )
        github_oauth_token = extra.get(GITHUB_OAUTH_TOKEN_EXTRA_KEY)
        if not github_oauth_token:
            raise UsageFetchError("Reconnect required")

        headers = {
            "Authorization": f"token {github_oauth_token}",
            "Accept": "application/json",
            "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
            "Editor-Version": COPILOT_EDITOR_VERSION,
        }
        body = await self._get_json(COPILOT_USAGE_URL, headers)
        return _parse_copilot_usage(connection.connection_id, self._display_name(connection), body)

    async def _fetch_minimax(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        provider = self._runtime.providers.get(connection.provider_id)
        connection_config = provider.get_connection(connection.local_connection_id)
        base_url = connection_config.base_url or provider.base_url

        token_getter = self._runtime.get_connection_token_getter(
            connection.provider_id, connection.connection_id
        )
        token = await token_getter()
        headers = {connection_config.auth.header: f"{connection_config.auth.prefix}{token}"}
        body = await self._get_json(_join_url(base_url, MINIMAX_USAGE_PATH), headers)
        return _parse_minimax_usage(connection.connection_id, self._display_name(connection), body)

    async def _get_json(self, url: str, headers: Mapping[str, str]) -> Any:
        response = await self._transport.get(url, headers=headers, timeout=self._timeout)
        if response.status_code >= 400:
            raise UsageFetchError(f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise UsageFetchError("Invalid response") from exc


# ---------------------------------------------------------------------------
# OpenAI parsing
# ---------------------------------------------------------------------------


def _parse_openai_usage(connection_id: str, display_name: str, body: Any) -> ProviderUsageSnapshot:
    rate_limit = body.get("rate_limit") if isinstance(body, Mapping) else None
    windows: list[UsageWindow] = []
    if isinstance(rate_limit, Mapping):
        primary = _openai_window(rate_limit.get("primary_window"), _primary_window_label)
        if primary is not None:
            windows.append(primary)
        secondary = _openai_window(rate_limit.get("secondary_window"), _secondary_window_label)
        if secondary is not None:
            windows.append(secondary)
    return ProviderUsageSnapshot(
        connection=connection_id,
        display_name=display_name,
        plan=_openai_plan(body),
        windows=windows,
    )


def _openai_window(raw: Any, label_for: Callable[[Any], str]) -> UsageWindow | None:
    if not isinstance(raw, Mapping):
        return None
    used_percent = _as_number(raw.get("used_percent"))
    if used_percent is None:
        return None
    return UsageWindow(
        label=label_for(raw.get("limit_window_seconds")),
        used_percent=clamp_percent(used_percent),
        reset_at=_epoch_to_iso(raw.get("reset_at")),
    )


def _openai_plan(body: Any) -> str | None:
    if not isinstance(body, Mapping):
        return None
    plan_type = body.get("plan_type")
    plan = plan_type.strip() if isinstance(plan_type, str) and plan_type.strip() else None
    balance = _credits_balance(body.get("credits"))
    if plan is None:
        return f"{balance} credits" if balance is not None else None
    if balance is not None:
        return f"{plan} · {balance} credits"
    return plan


def _credits_balance(credits: Any) -> int | None:
    # The live `/wham/usage` body reports `balance` as a STRING ("0") gated by a
    # `has_credits` flag, so only surface a positive, credit-enabled balance.
    if not isinstance(credits, Mapping) or credits.get("has_credits") is not True:
        return None
    balance = _coerce_number(credits.get("balance"))
    if balance is not None and balance > 0:
        return int(balance)
    return None


def _primary_window_label(seconds: Any) -> str:
    if not _is_positive_number(seconds):
        return _PRIMARY_FALLBACK_LABEL
    return f"{seconds / 3600:g}h"


def _secondary_window_label(seconds: Any) -> str:
    if not _is_positive_number(seconds):
        return _SECONDARY_FALLBACK_LABEL
    if seconds >= _WEEK_SECONDS:
        return "Week"
    if seconds >= _DAY_SECONDS:
        return "Day"
    return f"{round(seconds / 3600)}h"


# ---------------------------------------------------------------------------
# GitHub Copilot parsing (blind, best-effort)
# ---------------------------------------------------------------------------


def _parse_copilot_usage(connection_id: str, display_name: str, body: Any) -> ProviderUsageSnapshot:
    quota_snapshots = body.get("quota_snapshots") if isinstance(body, Mapping) else None
    reset_at = _date_to_iso(body.get("quota_reset_date")) if isinstance(body, Mapping) else None
    windows: list[UsageWindow] = []
    if isinstance(quota_snapshots, Mapping):
        for snapshot_key, label in (("premium_interactions", "Premium"), ("chat", "Chat")):
            window = _copilot_window(quota_snapshots.get(snapshot_key), label, reset_at)
            if window is not None:
                windows.append(window)
    return ProviderUsageSnapshot(
        connection=connection_id,
        display_name=display_name,
        plan=_copilot_plan(body),
        windows=windows,
    )


def _copilot_window(raw: Any, label: str, reset_at: str | None) -> UsageWindow | None:
    if not isinstance(raw, Mapping):
        return None
    percent_remaining = _as_number(raw.get("percent_remaining"))
    if percent_remaining is None:
        return None
    return UsageWindow(
        label=label,
        used_percent=clamp_percent(100.0 - percent_remaining),
        reset_at=reset_at,
    )


def _copilot_plan(body: Any) -> str | None:
    if not isinstance(body, Mapping):
        return None
    plan = body.get("copilot_plan")
    return plan.strip() if isinstance(plan, str) and plan.strip() else None


# ---------------------------------------------------------------------------
# MiniMax parsing (blind, best-effort)
# ---------------------------------------------------------------------------


def _parse_minimax_usage(connection_id: str, display_name: str, body: Any) -> ProviderUsageSnapshot:
    model_remains = body.get("model_remains") if isinstance(body, Mapping) else None
    if not isinstance(model_remains, list):
        raise UsageFetchError("Unsupported response shape")
    entry = _pick_minimax_model(model_remains)
    if entry is None:
        raise UsageFetchError("Unsupported response shape")

    total = _first_number(entry, _MINIMAX_TOTAL_KEYS)
    remaining = _first_number(entry, _MINIMAX_REMAINING_KEYS)
    if total is None or total <= 0 or remaining is None:
        raise UsageFetchError("Unsupported response shape")

    window = UsageWindow(
        label=_minimax_window_label(entry),
        used_percent=clamp_percent((total - remaining) / total * 100.0),
        reset_at=_minimax_reset_at(entry),
    )
    return ProviderUsageSnapshot(
        connection=connection_id,
        display_name=display_name,
        plan=_first_string(body, _MINIMAX_PLAN_KEYS) if isinstance(body, Mapping) else None,
        windows=[window],
    )


def _pick_minimax_model(model_remains: list[Any]) -> Mapping[str, Any] | None:
    """Return the chat-model entry (``MiniMax-M*``) with a non-zero total."""

    for entry in model_remains:
        if not isinstance(entry, Mapping):
            continue
        model_name = entry.get("model_name")
        if not isinstance(model_name, str):
            continue
        if not model_name.lower().startswith(_MINIMAX_CHAT_MODEL_PREFIX):
            continue
        total = _first_number(entry, _MINIMAX_TOTAL_KEYS)
        if total is not None and total > 0:
            return entry
    return None


def _minimax_window_label(entry: Mapping[str, Any]) -> str:
    minutes = _as_number(entry.get("current_interval_minutes"))
    if minutes is not None and minutes > 0:
        if minutes >= 60:
            return f"{minutes / 60:g}h"
        return f"{round(minutes)}m"
    model_name = entry.get("model_name")
    return model_name if isinstance(model_name, str) and model_name else "Plan"


def _minimax_reset_at(entry: Mapping[str, Any]) -> str | None:
    for key in _MINIMAX_RESET_KEYS:
        reset_at = _date_to_iso(entry.get(key))
        if reset_at is not None:
            return reset_at
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def clamp_percent(value: Any) -> float:
    """Clamp a raw percentage to the inclusive 0–100 range as a float."""

    number = _as_number(value)
    if number is None:
        return 0.0
    return max(0.0, min(100.0, number))


def _epoch_to_iso(value: Any) -> str | None:
    seconds = _as_number(value)
    if seconds is None:
        return None
    if seconds > _EPOCH_MILLISECONDS_THRESHOLD:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _date_to_iso(value: Any) -> str | None:
    """Normalize an epoch number or an ISO date/datetime string to ISO-8601 UTC."""

    if _as_number(value) is not None:
        return _epoch_to_iso(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _first_number(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        number = _as_number(mapping.get(key))
        if number is not None:
            return number
    return None


def _first_string(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_number(value: Any) -> float | None:
    """Return *value* as a float, or ``None`` when it is not a real number."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _coerce_number(value: Any) -> float | None:
    """Like :func:`_as_number` but also parse a numeric string (e.g. ``"1234"``)."""

    number = _as_number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _is_positive_number(value: Any) -> bool:
    number = _as_number(value)
    return number is not None and number > 0


def _is_meaningful(snapshot: ProviderUsageSnapshot) -> bool:
    return bool(snapshot.windows) or bool(snapshot.error)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
