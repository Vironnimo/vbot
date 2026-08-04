"""Async provider token getters for static and OAuth credentials."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse

import httpx

from core.providers._http_shared import wrap_network_error
from core.providers.accounts import DEFAULT_ACCOUNT_ID
from core.providers.errors import ProviderAuthError, ProviderError, ProviderRateLimitError
from core.providers.openai_subscription_auth import openai_subscription_token_extra
from core.providers.providers import (
    MINIMAX_OAUTH_DEVICE_FLOW,
    NOUS_OAUTH_DEVICE_FLOW,
    OPENAI_CODEX_DEVICE_FLOW,
    XAI_OAUTH_DEVICE_FLOW,
    OAuthConfig,
    resolve_minimax_oauth_expiry,
)
from core.providers.token_store import OAuthToken, TokenStore
from core.utils.http_status import is_retryable_status
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("providers.token_getter")

TOKEN_EXPIRY_BUFFER_SECONDS = 30
TOKEN_EXCHANGE_FALLBACK_MINUTES = 25
GITHUB_OAUTH_TOKEN_EXTRA_KEY = "github_oauth_token"
COPILOT_API_ENDPOINT_EXTRA_KEY = "copilot_api_endpoint"
COPILOT_INTEGRATION_ID = "vscode-chat"
COPILOT_EDITOR_VERSION = "vscode/1.128.0"
NOUS_INFERENCE_INVOKE_SCOPE = "inference:invoke"
OAUTH_SCOPE_EXTRA_KEY = "oauth_scope"
ROTATING_REFRESH_DEVICE_FLOWS = frozenset(
    {MINIMAX_OAUTH_DEVICE_FLOW, NOUS_OAUTH_DEVICE_FLOW, XAI_OAUTH_DEVICE_FLOW}
)
_COPILOT_API_HOST_SUFFIXES = (
    ".githubcopilot.com",
    ".ghe.com",
)
_COPILOT_PROXY_ENDPOINT_PATTERN = re.compile(r"(?:^|;)\s*proxy-ep=([^;\s]+)")


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
                    "No usable OAuth token (provider=%s connection=%s) — reconnect required",
                    self._provider_id,
                    self._local_connection_id,
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
            "OAuth token expired with no refresh path (provider=%s connection=%s) — "
            "reconnect required",
            self._provider_id,
            self._local_connection_id,
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
            extra={
                **token.extra,
                **copilot_token_extra(response_data, github_oauth_token, access_token),
            },
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
                "OAuth refresh requested without a refresh token "
                "(provider=%s connection=%s) — reconnect required",
                self._provider_id,
                self._local_connection_id,
            )
            raise ProviderAuthError("OAuth token expired — please reconnect")
        now = datetime.now(UTC)
        try:
            if self._oauth_config.device_flow == NOUS_OAUTH_DEVICE_FLOW:
                # Nous refresh tokens are single-use. Retrying a POST after an
                # ambiguous transport failure can replay the retired token and
                # revoke the entire session chain.
                response_data = await self._post_refresh_token(token.refresh_token)
            else:
                response_data = await retry_async(self._post_refresh_token, token.refresh_token)
            if (
                self._oauth_config.device_flow == MINIMAX_OAUTH_DEVICE_FLOW
                and response_data.get("status") != "success"
            ):
                raise ProviderAuthError("OAuth token refresh failed — please reconnect")
            access_token = _required_token_string(response_data.get("access_token"))
            refresh_token = response_data.get("refresh_token")
            extra = dict(token.extra)
            if self._oauth_config.device_flow == NOUS_OAUTH_DEVICE_FLOW:
                validate_nous_oauth_scope(response_data, self._oauth_config.scopes)
                extra[OAUTH_SCOPE_EXTRA_KEY] = oauth_scope_value(
                    response_data, self._oauth_config.scopes
                )
            if self._oauth_config.device_flow == OPENAI_CODEX_DEVICE_FLOW:
                extra.update(openai_subscription_token_extra(access_token))
            if self._oauth_config.device_flow == MINIMAX_OAUTH_DEVICE_FLOW:
                try:
                    expires_at = resolve_minimax_oauth_expiry(
                        response_data.get("expired_in"), now=now
                    )
                except ValueError as exc:
                    raise ProviderAuthError(
                        "OAuth token refresh failed — please reconnect"
                    ) from exc
            else:
                expires_at = _parse_oauth_expiry(response_data, now)
            refreshed_token = OAuthToken(
                access_token=access_token,
                refresh_token=(
                    refresh_token if isinstance(refresh_token, str) else token.refresh_token
                ),
                expires_at=expires_at,
                extra=extra,
            )
            self._token_store.save(
                self._provider_id,
                self._local_connection_id,
                refreshed_token,
                account_id=self._account_id,
            )
        except ProviderError as exc:
            if (
                isinstance(exc, ProviderAuthError)
                and self._oauth_config.device_flow in ROTATING_REFRESH_DEVICE_FLOWS
            ):
                self._token_store.delete(
                    self._provider_id,
                    self._local_connection_id,
                    account_id=self._account_id,
                )
            self._log_refresh_failure(exc)
            raise
        self._log_refresh_success()
        return refreshed_token.access_token

    def _log_refresh_success(self) -> None:
        _LOGGER.info(
            "Refreshed OAuth token (provider=%s connection=%s)",
            self._provider_id,
            self._local_connection_id,
        )

    def _log_refresh_failure(self, exc: Exception) -> None:
        _LOGGER.warning(
            "OAuth token refresh failed (provider=%s connection=%s): %s",
            self._provider_id,
            self._local_connection_id,
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
                data: dict[str, object] = {
                    "grant_type": "refresh_token",
                    "client_id": self._oauth_config.client_id,
                }
                headers = {"Accept": "application/json"}
                if self._oauth_config.device_flow == NOUS_OAUTH_DEVICE_FLOW:
                    headers["x-nous-refresh-token"] = refresh_token
                else:
                    data["refresh_token"] = refresh_token
                response = await client.post(
                    self._oauth_config.token_url,
                    data=data,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
        finally:
            if close_client:
                await client.aclose()

        if self._oauth_config.device_flow == NOUS_OAUTH_DEVICE_FLOW:
            _classify_nous_refresh_status(response.status_code, response.text)
        else:
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
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return fallback
    if not isinstance(value, str) or not value:
        return fallback
    if value.isdecimal():
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
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


def _oauth_scope_tokens(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(token for token in value.split() if token)
    if isinstance(value, list) and all(isinstance(token, str) for token in value):
        return tuple(token for token in value if token)
    return ()


def oauth_scope_value(data: dict[str, object], requested_scopes: list[str]) -> str:
    """Return the advertised OAuth scope, or the exact requested scope."""

    advertised = _oauth_scope_tokens(data.get("scope"))
    return " ".join(advertised or tuple(requested_scopes))


def validate_nous_oauth_scope(data: dict[str, object], requested_scopes: list[str]) -> None:
    """Reject a Nous token response that explicitly lacks inference access."""

    advertised = _oauth_scope_tokens(data.get("scope"))
    if advertised and NOUS_INFERENCE_INVOKE_SCOPE not in advertised:
        raise ProviderAuthError(
            "Nous Portal login did not grant inference access — please reconnect"
        )
    if NOUS_INFERENCE_INVOKE_SCOPE not in requested_scopes:
        raise ProviderAuthError("Nous Portal connection is missing the inference scope")


def _classify_nous_refresh_status(status_code: int, response_body: str) -> None:
    if status_code < 400:
        return
    normalized = response_body.casefold()
    if "refresh_token_reused" in normalized or "reuse detected" in normalized:
        raise ProviderAuthError(
            "Nous Portal detected refresh-token reuse and revoked this login — reconnect"
        )
    _classify_token_exchange_status(status_code, response_body)


def _classify_token_exchange_status(status_code: int, response_body: str) -> None:
    if status_code < 400:
        return
    detail = f"{status_code} {response_body}".strip() if response_body else str(status_code)
    if status_code == 429:
        raise ProviderRateLimitError(f"Rate limited: {detail}")
    # OAuth token exchange is a non-idempotent POST: authorization codes are
    # single-use, so a 500 (possibly already-consumed code, often deterministic)
    # must not be blindly retried.
    if is_retryable_status(status_code, idempotent=False):
        raise ProviderError(f"Provider error: {detail}", retryable=True)
    raise ProviderAuthError("OAuth token refresh failed — please reconnect")


def copilot_token_extra(
    response_data: dict[str, object],
    github_oauth_token: str,
    copilot_api_token: str,
) -> dict[str, str]:
    """Return the safe persisted metadata from one Copilot token exchange."""

    extra = {GITHUB_OAUTH_TOKEN_EXTRA_KEY: github_oauth_token}
    api_endpoint = _copilot_api_endpoint(response_data, copilot_api_token)
    if api_endpoint is not None:
        extra[COPILOT_API_ENDPOINT_EXTRA_KEY] = api_endpoint
    return extra


def _copilot_api_endpoint(
    response_data: dict[str, object],
    copilot_api_token: str,
) -> str | None:
    endpoints = response_data.get("endpoints")
    if isinstance(endpoints, dict):
        api_endpoint = _validated_copilot_api_endpoint(endpoints.get("api"))
        if api_endpoint is not None:
            return api_endpoint

    proxy_match = _COPILOT_PROXY_ENDPOINT_PATTERN.search(copilot_api_token)
    if proxy_match is None:
        return None
    proxy_endpoint = proxy_match.group(1).strip().rstrip("/")
    parsed_proxy = urlparse(
        proxy_endpoint if "://" in proxy_endpoint else f"https://{proxy_endpoint}"
    )
    proxy_host = (parsed_proxy.hostname or "").lower()
    if proxy_host.startswith("proxy."):
        proxy_host = f"api.{proxy_host.removeprefix('proxy.')}"
    return _validated_copilot_api_endpoint(f"https://{proxy_host}")


def _validated_copilot_api_endpoint(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    endpoint = value.strip().rstrip("/")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    if host == "api.githubcopilot.com" or host.endswith(_COPILOT_API_HOST_SUFFIXES):
        return endpoint
    return None
