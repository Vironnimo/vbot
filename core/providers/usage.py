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
``core.runtime`` (import-cycle risk) and never caches raw OAuth access tokens."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from core.providers._http_shared import classify_http_status
from core.providers._usage_parsers import (
    _parse_copilot_usage,
    _parse_minimax_usage,
    _parse_ollama_usage,
    _parse_openai_usage,
    _parse_openrouter_usage,
    clamp_percent,
)
from core.providers._usage_types import (
    COPILOT_USAGE_CONNECTION,
    COPILOT_USAGE_URL,
    MINIMAX_USAGE_CONNECTION,
    MINIMAX_USAGE_PATH,
    OLLAMA_USAGE_CONNECTION,
    OLLAMA_USAGE_PATH,
    OPENAI_USAGE_CONNECTION,
    OPENAI_USAGE_PATH,
    OPENROUTER_CREDITS_PATH,
    OPENROUTER_KEY_PATH,
    OPENROUTER_USAGE_CONNECTION,
    ProviderUsageSnapshot,
    UsageCredits,
    UsageFetchError,
    UsageProbeRuntime,
    UsageReport,
    UsageResponse,
    UsageTransport,
    UsageWindow,
    _SupportedConnection,
)
from core.providers.errors import ProviderAuthError
from core.providers.openai import CODEX_EXTRA_HEADERS
from core.providers.openai_subscription_auth import (
    CHATGPT_ACCOUNT_ID_EXTRA_KEY,
    extract_chatgpt_account_id,
)
from core.providers.token_getter import (
    COPILOT_EDITOR_VERSION,
    COPILOT_INTEGRATION_ID,
    GITHUB_OAUTH_TOKEN_EXTRA_KEY,
    OAuthRequestRecovery,
)
from core.providers.usage_history import (
    ProviderUsageHistoryStore,
    UsageHistoryClearResult,
    UsageHistoryError,
    UsageHistoryReport,
)
from core.utils.errors import ConfigError
from core.utils.logging import get_logger

__all__ = [
    "COPILOT_USAGE_CONNECTION",
    "COPILOT_USAGE_URL",
    "DEFAULT_USAGE_CACHE_TTL_SECONDS",
    "DEFAULT_USAGE_ERROR_CACHE_TTL_SECONDS",
    "DEFAULT_USAGE_HISTORY_INTERVAL_SECONDS",
    "DEFAULT_USAGE_TIMEOUT_SECONDS",
    "HttpxUsageTransport",
    "MINIMAX_USAGE_CONNECTION",
    "MINIMAX_USAGE_PATH",
    "OLLAMA_USAGE_CONNECTION",
    "OLLAMA_USAGE_PATH",
    "OPENAI_USAGE_CONNECTION",
    "OPENAI_USAGE_PATH",
    "OPENROUTER_CREDITS_PATH",
    "OPENROUTER_KEY_PATH",
    "OPENROUTER_USAGE_CONNECTION",
    "ProviderUsageService",
    "ProviderUsageSnapshot",
    "UsageCredits",
    "UsageFetchError",
    "UsageProbeRuntime",
    "UsageReport",
    "UsageResponse",
    "UsageTransport",
    "UsageWindow",
    "clamp_percent",
]

_LOGGER = get_logger("providers.usage")

DEFAULT_USAGE_TIMEOUT_SECONDS = 8.0

DEFAULT_USAGE_CACHE_TTL_SECONDS = 10.0

DEFAULT_USAGE_ERROR_CACHE_TTL_SECONDS = 60.0

DEFAULT_USAGE_HISTORY_INTERVAL_SECONDS = 60 * 60


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


_SUPPORTED_CONNECTIONS: tuple[_SupportedConnection, ...] = (
    _SupportedConnection("openai", "subscription"),
    _SupportedConnection("github-copilot", "oauth"),
    _SupportedConnection("ollama-cloud", "api-key"),
    _SupportedConnection("minimax", "api-key"),
    _SupportedConnection("openrouter", "api-key"),
)

_Fetcher = Callable[[_SupportedConnection], Awaitable[ProviderUsageSnapshot]]


class ProviderUsageService:
    """Fetch normalized live usage windows for logged-in provider connections."""

    def __init__(
        self,
        runtime: UsageProbeRuntime,
        *,
        transport: UsageTransport | None = None,
        history_store: ProviderUsageHistoryStore | None = None,
        data_root: str | Path | None = None,
        timeout: float = DEFAULT_USAGE_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_USAGE_CACHE_TTL_SECONDS,
        error_cache_ttl: float = DEFAULT_USAGE_ERROR_CACHE_TTL_SECONDS,
        history_interval: float = DEFAULT_USAGE_HISTORY_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runtime = runtime
        self._transport = transport or HttpxUsageTransport()
        self._history = history_store or (
            ProviderUsageHistoryStore(data_root) if data_root is not None else None
        )
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._error_cache_ttl = error_cache_ttl
        self._history_interval = history_interval
        self._monotonic = monotonic
        self._clock = clock
        self._cache: dict[str, tuple[float, float, ProviderUsageSnapshot]] = {}
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        self._history_task: asyncio.Task[None] | None = None
        self._history_started = False
        # Only connections with a registered fetcher are queried.
        self._fetchers: dict[str, _Fetcher] = {
            OPENAI_USAGE_CONNECTION: self._fetch_openai,
            COPILOT_USAGE_CONNECTION: self._fetch_copilot,
            OLLAMA_USAGE_CONNECTION: self._fetch_ollama,
            MINIMAX_USAGE_CONNECTION: self._fetch_minimax,
            OPENROUTER_USAGE_CONNECTION: self._fetch_openrouter,
        }

    def start(self) -> None:
        """Start the automatic history sampler on the current event loop."""

        if self._history_started or self._history is None:
            return
        self._history_started = True
        self._history_task = asyncio.create_task(
            self._history_loop(),
            name="provider-usage-history",
        )

    def stop(self) -> None:
        """Cancel automatic sampling without awaiting task completion."""

        self._history_started = False
        task = self._history_task
        self._history_task = None
        if task is not None:
            task.cancel()

    async def aclose(self) -> None:
        """Cancel and await the automatic sampler."""

        task = self._history_task
        self.stop()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

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
        return UsageReport(generated_at=self._now_iso(), providers=meaningful)

    def history_report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageHistoryReport:
        """Return durable automatic samples, never the 10-second live polls."""

        if self._history is None:
            return UsageHistoryReport(generated_at=self._now_iso())
        return self._history.report(since=since, until=until)

    def clear_history(self) -> UsageHistoryClearResult:
        """Explicitly delete all durable automatic usage samples."""

        if self._history is None:
            return UsageHistoryClearResult(deleted_samples=0, deleted_files=0)
        return self._history.clear()

    async def collect_history_sample(self) -> bool:
        """Fetch/coalesce live state and persist at most one automatic sample."""

        if self._history is None:
            return False
        try:
            report = await self.report()
            return self._history.append(
                report.generated_at,
                [snapshot.to_dict() for snapshot in report.providers],
            )
        except UsageHistoryError as exc:
            _LOGGER.warning("Provider usage history sample could not be stored: %s", exc)
            return False

    async def _history_loop(self) -> None:
        try:
            delay = self._initial_history_delay()
            if delay > 0:
                await asyncio.sleep(delay)
            while self._history_started:
                try:
                    await self.collect_history_sample()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — each background sample fails soft
                    _LOGGER.error(
                        "Provider usage history sample failed unexpectedly; "
                        "retrying at the next interval: %s",
                        exc,
                        exc_info=True,
                    )
                if self._history_started:
                    await asyncio.sleep(self._history_interval)
        finally:
            self._history_started = False
            self._history_task = None

    def _initial_history_delay(self) -> float:
        if self._history is None:
            return self._history_interval
        try:
            latest = self._history.latest_sampled_at()
        except UsageHistoryError as exc:
            _LOGGER.warning("Provider usage history freshness could not be read: %s", exc)
            return 0.0
        except Exception as exc:  # noqa: BLE001 — background sampling must fail soft
            _LOGGER.error(
                "Provider usage history freshness check failed unexpectedly; "
                "sampling immediately: %s",
                exc,
                exc_info=True,
            )
            return 0.0
        if latest is None:
            return 0.0
        elapsed = (self._now() - latest).total_seconds()
        return max(0.0, self._history_interval - max(0.0, elapsed))

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _select_targets(self, requested: set[str] | None) -> list[_SupportedConnection]:
        targets: list[_SupportedConnection] = []
        for supported in _SUPPORTED_CONNECTIONS:
            if supported.connection_id not in self._fetchers:
                continue
            if requested is not None and supported.connection_id not in requested:
                continue
            target = self._resolve_target(supported)
            if target is None:
                continue
            targets.append(target)
        return targets

    def _resolve_target(self, connection: _SupportedConnection) -> _SupportedConnection | None:
        try:
            if not self._runtime.provider_credentials.is_usable(
                connection.provider_id, connection.connection_id
            ):
                return None
            account_id = self._runtime.provider_credentials.resolve_account_id(
                connection.provider_id,
                connection.local_connection_id,
            )
        except (KeyError, ConfigError):
            return None
        return replace(connection, account_id=account_id)

    async def _snapshot_for(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        cached = self._cached_snapshot(connection.target_id)
        if cached is not None:
            return cached

        lock = self._fetch_locks.setdefault(connection.target_id, asyncio.Lock())
        async with lock:
            cached = self._cached_snapshot(connection.target_id)
            if cached is not None:
                return cached
            snapshot = await self._run_fetcher(connection)
            self._store_cache(connection.target_id, snapshot)
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
            _LOGGER.warning("Usage fetch failed for target %s: %s", connection.target_id, exc)
            return self._error_snapshot(connection, "Unavailable")

    def _error_snapshot(
        self, connection: _SupportedConnection, message: str
    ) -> ProviderUsageSnapshot:
        return ProviderUsageSnapshot(
            connection=connection.connection_id,
            account=connection.account_id,
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
        stored_at, ttl, snapshot = entry
        if self._monotonic() - stored_at >= ttl:
            return None
        return snapshot

    def _store_cache(self, connection_id: str, snapshot: ProviderUsageSnapshot) -> None:
        ttl = self._error_cache_ttl if snapshot.error else self._cache_ttl
        self._cache[connection_id] = (self._monotonic(), ttl, snapshot)

    # ------------------------------------------------------------------
    # Per-provider fetchers
    # ------------------------------------------------------------------

    async def _fetch_openai(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        provider = self._runtime.providers.get(connection.provider_id)
        connection_config = provider.get_connection(connection.local_connection_id)
        base_url = connection_config.base_url or provider.base_url

        token_getter = self._runtime.get_connection_token_getter(connection.ref)

        async def headers() -> dict[str, str]:
            token = await token_getter()
            account_id = extract_chatgpt_account_id(token)
            if not account_id:
                extra = self._runtime.get_connection_token_extra(connection.ref)
                account_id = extra.get(CHATGPT_ACCOUNT_ID_EXTRA_KEY) or None
            if not account_id:
                raise UsageFetchError("Reconnect required")
            return {
                connection_config.auth.header: f"{connection_config.auth.prefix}{token}",
                "chatgpt-account-id": account_id,
                **CODEX_EXTRA_HEADERS,
            }

        body = await self._get_json(
            _join_url(base_url, OPENAI_USAGE_PATH),
            headers,
            auth_recovery=OAuthRequestRecovery(token_getter, connection_config.auth),
        )
        return _parse_openai_usage(
            connection.connection_id,
            self._display_name(connection),
            body,
            account=connection.account_id,
        )

    async def _fetch_copilot(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        # The Copilot usage endpoint authenticates with the GitHub OAuth token
        # (token-store ``extra``) under GitHub's ``token`` scheme — NOT the
        # exchanged Copilot bearer.
        extra = self._runtime.get_connection_token_extra(connection.ref)
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
        return _parse_copilot_usage(
            connection.connection_id,
            self._display_name(connection),
            body,
            account=connection.account_id,
        )

    async def _fetch_minimax(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        provider = self._runtime.providers.get(connection.provider_id)
        connection_config = provider.get_connection(connection.local_connection_id)
        base_url = connection_config.base_url or provider.base_url

        token_getter = self._runtime.get_connection_token_getter(connection.ref)
        token = await token_getter()
        headers = {connection_config.auth.header: f"{connection_config.auth.prefix}{token}"}
        body = await self._get_json(_join_url(base_url, MINIMAX_USAGE_PATH), headers)
        return _parse_minimax_usage(
            connection.connection_id,
            self._display_name(connection),
            body,
            account=connection.account_id,
        )

    async def _fetch_ollama(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        provider = self._runtime.providers.get(connection.provider_id)
        connection_config = provider.get_connection(connection.local_connection_id)
        base_url = connection_config.base_url or provider.base_url

        token_getter = self._runtime.get_connection_token_getter(connection.ref)
        token = await token_getter()
        headers = {connection_config.auth.header: f"{connection_config.auth.prefix}{token}"}
        body = await self._get_json(_join_url(base_url, OLLAMA_USAGE_PATH), headers)
        return _parse_ollama_usage(
            connection.connection_id,
            self._display_name(connection),
            body,
            account=connection.account_id,
        )

    async def _fetch_openrouter(self, connection: _SupportedConnection) -> ProviderUsageSnapshot:
        provider = self._runtime.providers.get(connection.provider_id)
        connection_config = provider.get_connection(connection.local_connection_id)
        base_url = connection_config.base_url or provider.base_url

        token_getter = self._runtime.get_connection_token_getter(connection.ref)
        token = await token_getter()
        headers = {connection_config.auth.header: f"{connection_config.auth.prefix}{token}"}
        credits_body = await self._get_json(_join_url(base_url, OPENROUTER_CREDITS_PATH), headers)
        # The key spending-cap endpoint is supplementary: when it fails, degrade
        # to a credits-only snapshot instead of failing the whole fetch.
        key_body: Any = None
        with suppress(UsageFetchError):
            key_body = await self._get_json(_join_url(base_url, OPENROUTER_KEY_PATH), headers)
        return _parse_openrouter_usage(
            connection.connection_id,
            self._display_name(connection),
            credits_body,
            key_body,
            account=connection.account_id,
        )

    async def _get_json(
        self,
        url: str,
        headers: Mapping[str, str] | Callable[[], Awaitable[dict[str, str]]],
        *,
        auth_recovery: OAuthRequestRecovery | None = None,
    ) -> Any:
        async def request() -> Any:
            request_headers = await headers() if callable(headers) else headers
            response = await self._transport.get(
                url, headers=request_headers, timeout=self._timeout
            )
            if auth_recovery is not None:
                auth_recovery.record_response(
                    response.status_code,
                    request_headers,
                    response.text if response.status_code >= 400 else "",
                )
                if response.status_code == 401:
                    classify_http_status(401, idempotent=True)
            if response.status_code >= 400:
                raise UsageFetchError(f"HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                raise UsageFetchError("Invalid response") from exc

        if auth_recovery is None:
            return await request()
        try:
            return await auth_recovery.run(request)
        except ProviderAuthError as exc:
            if getattr(exc, "status_code", None) == 401:
                raise UsageFetchError("HTTP 401") from exc
            raise UsageFetchError("Reconnect required") from exc


def _is_meaningful(snapshot: ProviderUsageSnapshot) -> bool:
    # Enabled credits alone are meaningful: OpenRouter keys without a spending
    # cap (or with a failed /key probe) still report a credits balance.
    credits_meaningful = snapshot.credits is not None and snapshot.credits.enabled
    return bool(snapshot.windows) or credits_meaningful or bool(snapshot.error)


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
