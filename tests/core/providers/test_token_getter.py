"""Tests for provider token getter implementations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError
from core.providers.providers import OAuthConfig
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter
from core.providers.token_store import OAuthToken, TokenStore

PROVIDER_ID = "github-copilot"
CONNECTION_ID = "oauth"
TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"


class StubAsyncClient:
    def __init__(self, response: httpx.Response | None = None, **_kwargs: object) -> None:
        self.closed = False
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._response = response or httpx.Response(
            200,
            json={
                "token": "fresh-copilot-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )

    async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        self.requests.append((url, headers))
        return self._response

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture()
def oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="client-id",
        device_auth_url="https://github.com/login/device/code",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["copilot"],
        token_exchange_url=TOKEN_EXCHANGE_URL,
    )


@pytest.mark.asyncio
async def test_static_token_getter_returns_value() -> None:
    """StaticTokenGetter returns the configured token."""

    getter = StaticTokenGetter("static-secret")

    token = await getter()

    assert token == "static-secret"


@pytest.mark.asyncio
async def test_oauth_token_getter_returns_valid_stored_token(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """OAuthTokenGetter returns a non-expired stored access token."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="copilot-api-token",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    token = await getter()

    assert token == "copilot-api-token"


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_refreshes_expired_token_with_exchange_url(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """Expired Copilot tokens refresh through the token exchange URL."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    route = respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"token": "fresh-copilot-token", "expires_at": expires_at.isoformat()},
        )
    )
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    token = await getter()

    assert token == "fresh-copilot-token"
    assert route.call_count == 1
    assert route.calls.last.request.headers.get("authorization") == "token github-oauth-secret"
    stored = token_store.load(PROVIDER_ID, CONNECTION_ID)
    assert stored is not None
    assert stored.access_token == "fresh-copilot-token"
    assert stored.expires_at == expires_at
    assert stored.extra["github_oauth_token"] == "github-oauth-secret"


@pytest.mark.asyncio
async def test_oauth_token_getter_expired_without_refresh_path_raises(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """Expired tokens without an exchange URL require reconnect."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    getter = OAuthTokenGetter(
        token_store,
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthConfig(
            flow="device",
            client_id="client-id",
            device_auth_url="https://github.com/login/device/code",
            token_url="https://github.com/login/oauth/access_token",
            scopes=["copilot"],
        ),
    )

    with pytest.raises(ProviderAuthError, match="expired"):
        await getter()


@pytest.mark.asyncio
async def test_oauth_token_getter_missing_token_raises(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """Missing stored OAuth tokens require provider connection first."""

    getter = OAuthTokenGetter(TokenStore(tmp_path), PROVIDER_ID, CONNECTION_ID, oauth_config)

    with pytest.raises(ProviderAuthError, match="No OAuth token"):
        await getter()


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_concurrent_refresh_uses_single_http_call(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """Concurrent calls serialize refresh so only one exchange request is made."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    route = respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "fresh-copilot-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )
    )
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    tokens = await asyncio.gather(getter(), getter())

    assert tokens == ["fresh-copilot-token", "fresh-copilot-token"]
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_oauth_token_getter_preserves_injected_client_lifecycle(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """OAuthTokenGetter does not close caller-injected clients."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    client = StubAsyncClient()

    async with OAuthTokenGetter(
        token_store,
        PROVIDER_ID,
        CONNECTION_ID,
        oauth_config,
        client=client,  # type: ignore[arg-type]
    ) as getter:
        token = await getter()

    assert token == "fresh-copilot-token"
    assert client.closed is False
    assert client.requests[0][0] == TOKEN_EXCHANGE_URL


@pytest.mark.asyncio
async def test_oauth_token_getter_aclose_closes_owned_created_client(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internally-created clients are closed by the async context manager."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-copilot-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    clients: list[StubAsyncClient] = []

    def make_client(**_kwargs: object) -> StubAsyncClient:
        client = StubAsyncClient()
        clients.append(client)
        return client

    monkeypatch.setattr("core.providers.token_getter.httpx.AsyncClient", make_client)
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    async with getter:
        token = await getter()

    assert token == "fresh-copilot-token"
    assert len(clients) == 1
    assert clients[0].closed is True
