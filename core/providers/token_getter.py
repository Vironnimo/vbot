"""Async provider token getters for static and OAuth credentials."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from core.providers._http_shared import wrap_network_error
from core.providers.accounts import DEFAULT_ACCOUNT_ID
from core.providers.errors import ProviderAuthError, ProviderError, ProviderRateLimitError
from core.providers.openai_subscription_auth import openai_subscription_token_extra
from core.providers.providers import OPENAI_CODEX_DEVICE_FLOW, OAuthConfig
from core.providers.token_store import OAuthToken, TokenStore
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("providers.token_getter")

TOKEN_EXPIRY_BUFFER_SECONDS = 30
TOKEN_EXCHANGE_FALLBACK_MINUTES = 25
GITHUB_OAUTH_TOKEN_EXTRA_KEY = "github_oauth_token"
TOKEN_EXCHANGE_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503})
COPILOT_INTEGRATION_ID = "vscode-chat"
COPILOT_EDITOR_VERSION = "vBot/0.1.0"


class TokenGetter(Protocol):
    """Async callable that returns the current provider auth token."""

    async def __call__(self) -> str: ...


class StaticTokenGetter:
    """Token getter for static API-key credentials."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def __call__(self) -> str:
        """Return the configured static token."""

        return self._token


class OAuthTokenGetter:
    """Token getter that refreshes stored OAuth provider tokens on expiry."""

    def __init__(
        self,
        token_store: TokenStore,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
        client: httpx.AsyncClient | None = None,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        self._token_store = token_store
        self._provider_id = provider_id
        self._local_connection_id = local_connection_id
        self._oauth_config = oauth_config
        self._account_id = account_id
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> OAuthTokenGetter:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally-owned HTTP client, if one was created."""

        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __call__(self) -> str:
        """Return a valid OAuth-backed API token, refreshing when needed."""

        async with self._lock:
            token = self._token_store.load(
                self._provider_id,
                self._local_connection_id,
                account_id=self._account_id,
            )
            if token is None:
                _LOGGER.warning(
                    "No usable OAuth token for provider=%s connection=%s account=%s — "
                    "reconnect required",
                    self._provider_id,
                    self._local_connection_id,
                    self._account_id,
                )
                raise ProviderAuthError("No OAuth token — please connect this provider first")
            if not _is_expiring(token):
                return token.access_token
            return await self._refresh_expired_token(token)

    async def _refresh_expired_token(self, token: OAuthToken) -> str:
        token_exchange_url = self._oauth_config.token_exchange_url
        github_oauth_token = token.extra.get(GITHUB_OAUTH_TOKEN_EXTRA_KEY)
        if token_exchange_url and github_oauth_token:
            return await self._refresh_token_exchange(token, token_exchange_url, github_oauth_token)
        if token.refresh_token:
            return await self._refresh_oauth_token(token)
        _LOGGER.warning(
            "OAuth token expired with no refresh path for provider=%s connection=%s account=%s — "
            "reconnect required",
            self._provider_id,
            self._local_connection_id,
            self._account_id,
        )
        raise ProviderAuthError("OAuth token expired — please reconnect")

    async def _refresh_token_exchange(
        self,
        token: OAuthToken,
        token_exchange_url: str,
        github_oauth_token: str,
    ) -> str:
        now = datetime.now(UTC)
        try:
            response_data = await retry_async(
                self._exchange_token,
                token_exchange_url,
                github_oauth_token,
            )
        except ProviderError as exc:
            self._log_refresh_failure(exc)
            raise
        access_token = _required_token_string(response_data.get("token"))
        refreshed_token = OAuthToken(
            access_token=access_token,
            refresh_token=token.refresh_token,
            expires_at=_parse_exchange_expiry(response_data.get("expires_at"), now),
            extra={**token.extra, GITHUB_OAUTH_TOKEN_EXTRA_KEY: github_oauth_token},
        )
        self._token_store.save(
            self._provider_id,
            self._local_connection_id,
            refreshed_token,
            account_id=self._account_id,
        )
        self._log_refresh_success()
        return refreshed_token.access_token

    async def _refresh_oauth_token(self, token: OAuthToken) -> str:
        if not token.refresh_token:
            _LOGGER.warning(
                "OAuth refresh requested without a refresh token for provider=%s "
                "connection=%s account=%s — reconnect required",
                self._provider_id,
                self._local_connection_id,
                self._account_id,
            )
            raise ProviderAuthError("OAuth token expired — please reconnect")
        now = datetime.now(UTC)
        try:
            response_data = await retry_async(self._post_refresh_token, token.refresh_token)
        except ProviderError as exc:
            self._log_refresh_failure(exc)
            raise
        access_token = _required_token_string(response_data.get("access_token"))
        refresh_token = response_data.get("refresh_token")
        extra = dict(token.extra)
        if self._oauth_config.device_flow == OPENAI_CODEX_DEVICE_FLOW:
            extra.update(openai_subscription_token_extra(access_token))
        refreshed_token = OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else token.refresh_token,
            expires_at=_parse_oauth_expiry(response_data, now),
            extra=extra,
        )
        self._token_store.save(
            self._provider_id,
            self._local_connection_id,
            refreshed_token,
            account_id=self._account_id,
        )
        self._log_refresh_success()
        return refreshed_token.access_token

    def _log_refresh_success(self) -> None:
        _LOGGER.info(
            "Refreshed OAuth token for provider=%s connection=%s account=%s",
            self._provider_id,
            self._local_connection_id,
            self._account_id,
        )

    def _log_refresh_failure(self, exc: Exception) -> None:
        _LOGGER.warning(
            "OAuth token refresh failed for provider=%s connection=%s account=%s: %s",
            self._provider_id,
            self._local_connection_id,
            self._account_id,
            exc,
        )

    async def _exchange_token(
        self, token_exchange_url: str, github_oauth_token: str
    ) -> dict[str, object]:
        client = self._client
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=60.0)
            close_client = True
        try:
            try:
                response = await client.get(
                    token_exchange_url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {github_oauth_token}",
                        "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                        "Editor-Version": COPILOT_EDITOR_VERSION,
                    },
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
        finally:
            if close_client:
                await client.aclose()

        _classify_token_exchange_status(response.status_code, response.text)
        data = response.json()
        if not isinstance(data, dict):
            raise ProviderAuthError("OAuth token refresh failed — please reconnect")
        return data

    async def _post_refresh_token(self, refresh_token: str) -> dict[str, object]:
        client = self._client
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=60.0)
            close_client = True
        try:
            try:
                response = await client.post(
                    self._oauth_config.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._oauth_config.client_id,
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
        finally:
            if close_client:
                await client.aclose()

        _classify_token_exchange_status(response.status_code, response.text)
        data = response.json()
        if not isinstance(data, dict):
            raise ProviderAuthError("OAuth token refresh failed — please reconnect")
        return data


def _is_expiring(token: OAuthToken) -> bool:
    if token.expires_at is None:
        return False
    expiry_threshold = datetime.now(UTC) + timedelta(seconds=TOKEN_EXPIRY_BUFFER_SECONDS)
    return token.expires_at <= expiry_threshold


def _parse_exchange_expiry(value: object, now: datetime) -> datetime:
    fallback = now + timedelta(minutes=TOKEN_EXCHANGE_FALLBACK_MINUTES)
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_oauth_expiry(data: dict[str, object], now: datetime) -> datetime:
    expires_at = data.get("expires_at")
    if isinstance(expires_at, str) and expires_at:
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)

    expires_in = data.get("expires_in")
    if isinstance(expires_in, bool):
        expires_in = None
    if isinstance(expires_in, int):
        return now + timedelta(seconds=expires_in)
    if isinstance(expires_in, str) and expires_in.isdecimal():
        return now + timedelta(seconds=int(expires_in))
    return now + timedelta(minutes=TOKEN_EXCHANGE_FALLBACK_MINUTES)


def _required_token_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderAuthError("OAuth token refresh failed — please reconnect")
    return value


def _classify_token_exchange_status(status_code: int, response_body: str) -> None:
    if status_code < 400:
        return
    detail = f"{status_code} {response_body}".strip() if response_body else str(status_code)
    if status_code == 429:
        raise ProviderRateLimitError(f"Rate limited: {detail}")
    if status_code in TOKEN_EXCHANGE_RETRYABLE_STATUS_CODES:
        raise ProviderError(f"Provider error: {detail}", retryable=True)
    raise ProviderAuthError("OAuth token refresh failed — please reconnect")
