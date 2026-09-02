"""Tests for provider token getter implementations."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.providers import OAuthConfig
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter, copilot_token_extra
from core.providers.token_store import OAuthToken, TokenStore

PROVIDER_ID = "github-copilot"
CONNECTION_ID = "oauth"
TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
MINIMAX_TOKEN_URL = "https://api.minimax.io/oauth/token"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
NOUS_TOKEN_URL = "https://portal.nousresearch.com/api/oauth/token"
OPENCODE_TOKEN_URL = "https://console.opencode.ai/auth/device/token"


def test_copilot_token_extra_accepts_only_official_exchange_endpoints() -> None:
    trusted = copilot_token_extra(
        {"endpoints": {"api": "https://api.example.enterprise.githubcopilot.com/"}},
        "github-token",
        "copilot-token",
    )
    untrusted = copilot_token_extra(
        {"endpoints": {"api": "https://attacker.example/api"}},
        "github-token",
        "proxy-ep=proxy.business.githubcopilot.com;exp=123",
    )

    assert trusted["copilot_api_endpoint"] == ("https://api.example.enterprise.githubcopilot.com")
    assert untrusted["copilot_api_endpoint"] == "https://api.business.githubcopilot.com"


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


def _minimax_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="minimax-client-id",
        device_auth_url="https://api.minimax.io/oauth/code",
        token_url=MINIMAX_TOKEN_URL,
        scopes=["group_id", "profile", "model.completion"],
        device_flow="minimax_oauth",
    )


def _xai_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="xai-client-id",
        device_auth_url="https://auth.x.ai/oauth2/device/code",
        token_url=XAI_TOKEN_URL,
        scopes=["openid", "offline_access", "grok-cli:access", "api:access"],
        device_flow="xai_oauth",
    )


def _nous_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="hermes-cli",
        device_auth_url="https://portal.nousresearch.com/api/oauth/device/code",
        token_url=NOUS_TOKEN_URL,
        scopes=["inference:invoke"],
        device_flow="nous_oauth",
    )


def _opencode_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="opencode-cli",
        device_auth_url="https://console.opencode.ai/auth/device/code",
        token_url=OPENCODE_TOKEN_URL,
        scopes=[],
        device_flow="opencode_oauth",
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
            json={
                "token": "fresh-copilot-token",
                "expires_at": expires_at.timestamp(),
                "endpoints": {"api": "https://api.enterprise.githubcopilot.com"},
            },
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
    assert exchange_headers.get("editor-version") == "vscode/1.128.0"
    stored = token_store.load(PROVIDER_ID, CONNECTION_ID)
    assert stored is not None
    assert stored.access_token == "fresh-copilot-token"
    assert stored.expires_at == expires_at
    assert stored.extra["github_oauth_token"] == "github-oauth-secret"
    assert stored.extra["copilot_api_endpoint"] == ("https://api.enterprise.githubcopilot.com")


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_refresh_saves_under_the_same_account(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    caplog: pytest.LogCaptureFixture,
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

    with caplog.at_level(logging.INFO, logger="vbot.providers.token_getter"):
        token = await getter()

    assert token == "fresh-work-token"
    stored_work = token_store.load(PROVIDER_ID, CONNECTION_ID, account_id="work")
    assert stored_work is not None
    assert stored_work.access_token == "fresh-work-token"
    stored_default = token_store.load(PROVIDER_ID, CONNECTION_ID)
    assert stored_default is not None
    assert stored_default.access_token == "default-copilot-token"
    refresh_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.providers.token_getter"
    ]
    assert refresh_logs == [
        f"Refreshed OAuth token (provider={PROVIDER_ID} connection={CONNECTION_ID})"
    ]
    assert "work" not in " ".join(refresh_logs)


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


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getters_coalesce_forced_refresh_of_rejected_token(
    tmp_path: Path,
) -> None:
    """Separate getters share one refresh when the Provider rejects their token."""

    token_store = TokenStore(tmp_path)
    stale_access_token = _jwt_with_account("acct_old")
    token_store.save(
        "openai",
        "subscription",
        OAuthToken(
            access_token=stale_access_token,
            refresh_token="refresh-secret",
            expires_at=datetime.now(UTC) + timedelta(days=7),
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
    getters = [
        OAuthTokenGetter(
            token_store,
            "openai",
            "subscription",
            _openai_oauth_config(),
        )
        for _ in range(2)
    ]

    tokens = await asyncio.gather(
        *(getter.refresh_after_unauthorized(stale_access_token) for getter in getters)
    )

    assert tokens == [refreshed_access_token, refreshed_access_token]
    assert route.call_count == 1


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

    with pytest.raises(ProviderAuthError):
        await getter()


@pytest.mark.asyncio
async def test_oauth_token_getter_missing_token_raises(
    tmp_path: Path,
    oauth_config: OAuthConfig,
) -> None:
    """Missing stored OAuth tokens require provider connection first."""

    getter = OAuthTokenGetter(TokenStore(tmp_path), PROVIDER_ID, CONNECTION_ID, oauth_config)

    with pytest.raises(ProviderAuthError):
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
        "Editor-Version": "vscode/1.128.0",
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


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_logs_info_on_successful_refresh(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    caplog: Any,
) -> None:
    """A successful token refresh logs at info with non-secret identifiers only."""

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
    respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "fresh-copilot-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )
    )
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    with caplog.at_level(logging.INFO, logger="vbot.providers.token_getter"):
        token = await getter()

    assert token == "fresh-copilot-token"
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert info_records
    log_text = caplog.text
    assert PROVIDER_ID in log_text
    assert CONNECTION_ID in log_text
    # No secrets leak into the logs.
    assert "fresh-copilot-token" not in log_text
    assert "github-oauth-secret" not in log_text


@respx.mock
@pytest.mark.asyncio
async def test_oauth_token_getter_logs_warning_on_refresh_request_failure(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    caplog: Any,
) -> None:
    """A failed refresh request logs at warning (no traceback) before raising."""

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
    respx.get(TOKEN_EXCHANGE_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    getter = OAuthTokenGetter(token_store, PROVIDER_ID, CONNECTION_ID, oauth_config)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderAuthError),
    ):
        await getter()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected a warning log for the failed refresh"
    failure_record = warning_records[0]
    assert failure_record.exc_info is None
    assert PROVIDER_ID in caplog.text


@pytest.mark.asyncio
async def test_oauth_token_getter_logs_warning_when_no_token(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    caplog: Any,
) -> None:
    """A missing stored token logs at warning before requiring a reconnect."""

    getter = OAuthTokenGetter(TokenStore(tmp_path), PROVIDER_ID, CONNECTION_ID, oauth_config)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderAuthError),
    ):
        await getter()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("No usable OAuth token" in r.getMessage() for r in warning_records)
    assert all(r.exc_info is None for r in warning_records)


@respx.mock
@pytest.mark.asyncio
async def test_minimax_refresh_accepts_absolute_millisecond_expiry(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "minimax",
        "subscription",
        OAuthToken(
            access_token="expired-access",
            refresh_token="refresh-secret",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    expires_at_milliseconds = int((datetime.now(UTC) + timedelta(minutes=15)).timestamp() * 1000)
    route = respx.post(MINIMAX_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expired_in": expires_at_milliseconds,
            },
        )
    )
    getter = OAuthTokenGetter(
        token_store,
        "minimax",
        "subscription",
        _minimax_oauth_config(),
    )

    access_token = await getter()

    assert access_token == "fresh-access"
    request_form = parse_qs(route.calls.last.request.content.decode())
    assert request_form == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-secret"],
        "client_id": ["minimax-client-id"],
    }
    stored = token_store.load("minimax", "subscription")
    assert stored is not None
    assert stored.refresh_token == "fresh-refresh"
    assert stored.expires_at is not None
    assert 890 <= (stored.expires_at - datetime.now(UTC)).total_seconds() <= 900


@respx.mock
@pytest.mark.asyncio
async def test_minimax_terminal_refresh_failure_quarantines_token(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "minimax",
        "subscription",
        OAuthToken(
            access_token="expired-access",
            refresh_token="burned-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    respx.post(MINIMAX_TOKEN_URL).mock(
        return_value=httpx.Response(400, text="invalid_grant: refresh_token_reused")
    )
    getter = OAuthTokenGetter(
        token_store,
        "minimax",
        "subscription",
        _minimax_oauth_config(),
    )

    with pytest.raises(ProviderAuthError, match="reconnect"):
        await getter()

    assert token_store.load("minimax", "subscription") is None


@respx.mock
@pytest.mark.asyncio
async def test_nous_refresh_rotates_single_use_token_in_header(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "nous",
        "subscription",
        OAuthToken(
            access_token="expired-access",
            refresh_token="old-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    route = respx.post(NOUS_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 900,
                "scope": "inference:invoke",
            },
        )
    )
    getter = OAuthTokenGetter(token_store, "nous", "subscription", _nous_oauth_config())

    assert await getter() == "fresh-access"
    assert route.calls.last.request.headers["x-nous-refresh-token"] == "old-refresh"
    assert parse_qs(route.calls.last.request.content.decode()) == {
        "grant_type": ["refresh_token"],
        "client_id": ["hermes-cli"],
    }
    stored = token_store.load("nous", "subscription")
    assert stored is not None
    assert stored.refresh_token == "rotated-refresh"
    assert stored.extra == {"oauth_scope": "inference:invoke"}


@respx.mock
@pytest.mark.asyncio
async def test_nous_refresh_reuse_quarantines_token_without_retry(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "nous",
        "subscription",
        OAuthToken(
            access_token="expired-access",
            refresh_token="burned-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    route = respx.post(NOUS_TOKEN_URL).mock(
        return_value=httpx.Response(400, text="refresh_token_reused: reuse detected")
    )
    getter = OAuthTokenGetter(token_store, "nous", "subscription", _nous_oauth_config())

    with pytest.raises(ProviderAuthError):
        await getter()

    assert route.call_count == 1
    assert token_store.load("nous", "subscription") is None


@respx.mock
@pytest.mark.asyncio
async def test_nous_retryable_refresh_failure_is_not_replayed(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    original = OAuthToken(
        access_token="expired-access",
        refresh_token="still-valid-refresh",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    token_store.save("nous", "subscription", original)
    route = respx.post(NOUS_TOKEN_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    getter = OAuthTokenGetter(token_store, "nous", "subscription", _nous_oauth_config())

    with pytest.raises(ProviderError):
        await getter()

    assert route.call_count == 1
    assert token_store.load("nous", "subscription") == original


@respx.mock
@pytest.mark.asyncio
async def test_opencode_refresh_posts_json_and_persists_rotated_refresh_token(
    tmp_path: Path,
) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "opencode-zen",
        "account",
        OAuthToken(
            access_token="expired-access",
            refresh_token="old-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    route = respx.post(OPENCODE_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 900,
            },
        )
    )
    getter = OAuthTokenGetter(
        token_store,
        "opencode-zen",
        "account",
        _opencode_oauth_config(),
    )

    assert await getter() == "fresh-access"
    assert json.loads(route.calls.last.request.content) == {
        "grant_type": "refresh_token",
        "client_id": "opencode-cli",
        "refresh_token": "old-refresh",
    }
    stored = token_store.load("opencode-zen", "account")
    assert stored is not None
    assert stored.refresh_token == "rotated-refresh"


@respx.mock
@pytest.mark.asyncio
async def test_opencode_refresh_auth_failure_quarantines_token_without_retry(
    tmp_path: Path,
) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "opencode-zen",
        "account",
        OAuthToken(
            access_token="expired-access",
            refresh_token="burned-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    route = respx.post(OPENCODE_TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_refresh_token"})
    )
    getter = OAuthTokenGetter(
        token_store,
        "opencode-zen",
        "account",
        _opencode_oauth_config(),
    )

    with pytest.raises(ProviderAuthError, match="reconnect"):
        await getter()

    assert route.call_count == 1
    assert token_store.load("opencode-zen", "account") is None


@respx.mock
@pytest.mark.asyncio
async def test_xai_refresh_rotates_refresh_token(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "xai",
        "subscription",
        OAuthToken(
            access_token="expired-access",
            refresh_token="old-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    route = respx.post(XAI_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 900,
            },
        )
    )
    getter = OAuthTokenGetter(token_store, "xai", "subscription", _xai_oauth_config())

    assert await getter() == "fresh-access"
    assert parse_qs(route.calls.last.request.content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["old-refresh"],
        "client_id": ["xai-client-id"],
    }
    stored = token_store.load("xai", "subscription")
    assert stored is not None
    assert stored.refresh_token == "rotated-refresh"


@respx.mock
@pytest.mark.asyncio
async def test_xai_terminal_refresh_failure_quarantines_token(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "xai",
        "subscription",
        OAuthToken(
            access_token="expired-access",
            refresh_token="burned-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
    )
    respx.post(XAI_TOKEN_URL).mock(return_value=httpx.Response(400, text="invalid_grant"))
    getter = OAuthTokenGetter(token_store, "xai", "subscription", _xai_oauth_config())

    with pytest.raises(ProviderAuthError, match="reconnect"):
        await getter()

    assert token_store.load("xai", "subscription") is None


@respx.mock
@pytest.mark.asyncio
async def test_xai_retryable_refresh_failure_preserves_token(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    original = OAuthToken(
        access_token="expired-access",
        refresh_token="still-valid-refresh",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    token_store.save("xai", "subscription", original)
    route = respx.post(XAI_TOKEN_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    getter = OAuthTokenGetter(token_store, "xai", "subscription", _xai_oauth_config())

    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
        pytest.raises(ProviderError),
    ):
        await getter()

    assert route.call_count == 4
    assert sleep_mock.await_count == 3
    assert token_store.load("xai", "subscription") == original
