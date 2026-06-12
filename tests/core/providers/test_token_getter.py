"""Tests for provider token getter implementations."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

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
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"


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
        scopes=["read:user"],
        token_exchange_url=TOKEN_EXCHANGE_URL,
    )


def _openai_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="openai-client-id",
        device_auth_url="https://auth.openai.com/api/accounts/deviceauth/usercode",
        token_url=OPENAI_TOKEN_URL,
        scopes=["openid", "profile", "email", "offline_access"],
        device_flow="openai_codex",
    )


def _jwt_with_account(account_id: str = "acct_vbot") -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


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
    exchange_headers = route.calls.last.request.headers
    assert exchange_headers.get("accept") == "application/json"
    assert exchange_headers.get("authorization") == "Bearer github-oauth-secret"
    assert exchange_headers.get("copilot-integration-id") == "vscode-chat"
    assert exchange_headers.get("editor-version") == "vBot/0.1.0"
    stored = token_store.load(PROVIDER_ID, CONNECTION_ID)
    assert stored is not None
    assert stored.access_token == "fresh-copilot-token"
    assert stored.expires_at == expires_at
    assert stored.extra["github_oauth_token"] == "github-oauth-secret"


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_refresh_saves_under_the_same_account(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """A refresh for a named account loads and saves only that account's token."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(access_token="default-copilot-token"),
    )
    token_store.save(
        PROVIDER_ID,
        CONNECTION_ID,
        OAuthToken(
            access_token="expired-work-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-work-secret"},
        ),
        account_id="work",
    )
    respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "fresh-work-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )
    )
    getter = OAuthTokenGetter(
        token_store,
        PROVIDER_ID,
        CONNECTION_ID,
        oauth_config,
        account_id="work",
    )

    token = await getter()

    assert token == "fresh-work-token"
    stored_work = token_store.load(PROVIDER_ID, CONNECTION_ID, account_id="work")
    assert stored_work is not None
    assert stored_work.access_token == "fresh-work-token"
    stored_default = token_store.load(PROVIDER_ID, CONNECTION_ID)
    assert stored_default is not None
    assert stored_default.access_token == "default-copilot-token"


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_refreshes_expired_openai_codex_token(
    tmp_path: Path,
) -> None:
    """Expired OpenAI Codex OAuth tokens refresh through the refresh_token grant.

    The Codex (ChatGPT subscription) connection is provider ``openai`` with
    local connection id ``subscription``; the token-store key is therefore
    ``openai`` + ``-`` + ``subscription`` + ``.json`` (per the
    ``<provider>-<connection>`` rule).
    """

    token_store = TokenStore(tmp_path)
    token_store.save(
        "openai",
        "subscription",
        OAuthToken(
            access_token=_jwt_with_account("acct_old"),
            refresh_token="refresh-secret",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"chatgpt_account_id": "acct_old"},
        ),
    )
    refreshed_access_token = _jwt_with_account("acct_new")
    route = respx.post(OPENAI_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": refreshed_access_token,
                "refresh_token": "new-refresh-secret",
                "expires_in": 120,
            },
        )
    )
    getter = OAuthTokenGetter(
        token_store,
        "openai",
        "subscription",
        _openai_oauth_config(),
    )

    token = await getter()

    assert token == refreshed_access_token
    assert route.call_count == 1
    refresh_request = parse_qs(route.calls.last.request.content.decode("utf-8"))
    assert refresh_request == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-secret"],
        "client_id": ["openai-client-id"],
    }
    stored = token_store.load("openai", "subscription")
    assert stored is not None
    assert stored.access_token == refreshed_access_token
    assert stored.refresh_token == "new-refresh-secret"
    assert stored.expires_at is not None
    assert stored.expires_at > datetime.now(UTC)
    assert stored.extra == {"chatgpt_account_id": "acct_new"}


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
            scopes=["read:user"],
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
    assert client.requests[0][1] == {
        "Accept": "application/json",
        "Authorization": "Bearer github-oauth-secret",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vBot/0.1.0",
    }


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
