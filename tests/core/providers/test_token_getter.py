"""Token getter: refresh behavior."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from core.providers._http_shared import classify_http_status, post_json_with_retry
from core.providers.errors import ProviderAuthError
from core.providers.providers import AuthConfig, OAuthConfig
from core.providers.token_getter import (
    OAuthRequestRecovery,
    OAuthTokenGetter,
    StaticTokenGetter,
    copilot_token_extra,
)
from core.providers.token_store import OAuthToken, TokenStore
from tests.core.providers.token_getter_helpers import (
    CONNECTION_ID,
    PROVIDER_ID,
    TOKEN_EXCHANGE_URL,
    _minimax_oauth_config,
    _nous_oauth_config,
    _opencode_oauth_config,
    _xai_oauth_config,
)
from tests.core.providers.token_getter_helpers import (
    oauth_config as oauth_config,
)

OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "device_flow", ["oauth2", "openai_codex", "minimax_oauth", "nous_oauth", "opencode_oauth"]
)
async def test_xai_403_vocabulary_does_not_enable_other_oauth_flows(
    tmp_path: Path,
    device_flow: str,
) -> None:
    config = replace(_xai_oauth_config(), device_flow=device_flow)
    async with OAuthTokenGetter(TokenStore(tmp_path), "test", "oauth", config) as getter:
        # No token exists: a non-token rejection must not even try to load one.
        assert (
            await getter.refresh_after_rejection(
                "test-token",
                status_code=403,
                response_body='{"code":"unauthenticated:bad-credentials"}',
            )
            is None
        )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "rejection_status"),
    [
        ("openai", 401),
        ("xai", 401),
        ("xai", 403),
        ("nous", 401),
        ("minimax", 401),
        ("github-copilot", 401),
        ("opencode-zen", 401),
    ],
)
async def test_concurrent_rejected_requests_share_one_account_refresh(
    tmp_path: Path,
    oauth_config: OAuthConfig,
    provider_id: str,
    rejection_status: int,
) -> None:
    config = {
        "openai": _openai_oauth_config(),
        "xai": _xai_oauth_config(),
        "nous": _nous_oauth_config(),
        "minimax": _minimax_oauth_config(),
        "github-copilot": oauth_config,
        "opencode-zen": _opencode_oauth_config(),
    }[provider_id]
    store = TokenStore(tmp_path)
    old_token = _jwt_with_account("old-test-account")
    new_token = _jwt_with_account("new-test-account")
    store.save(
        provider_id,
        "oauth",
        OAuthToken(
            access_token=old_token,
            refresh_token="old-test-refresh",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            extra={"github_oauth_token": "test-github-token"}
            if provider_id == "github-copilot"
            else {},
        ),
        account_id="work",
    )
    exchange = (
        respx.get(config.token_exchange_url)
        if config.token_exchange_url
        else respx.post(config.token_url)
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": new_token,
                "token": new_token,
                "refresh_token": "new-test-refresh",
                "expires_in": 3600,
                "expired_in": 3600,
                "status": "success",
                "scope": "inference:invoke",
            },
        )
    )
    barrier = asyncio.Event()
    arrived = 0

    async def serve(request: httpx.Request) -> httpx.Response:
        nonlocal arrived
        if request.headers["Authorization"] == f"Bearer {old_token}":
            arrived += 1
            if arrived == 4:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=5)
            return httpx.Response(
                rejection_status, json={"code": "unauthenticated:bad-credentials"}
            )
        assert request.headers["Authorization"] == f"Bearer {new_token}"
        return httpx.Response(200, json={"ok": True})

    route = respx.post("https://inference.example/request").mock(side_effect=serve)
    auth = AuthConfig(header="Authorization", prefix="Bearer ")

    def classify(status: int, body: str, headers: httpx.Headers) -> None:
        classify_http_status(status, idempotent=False, detail=body, response_headers=headers)

    async def invoke() -> dict[str, Any]:
        async with (
            OAuthTokenGetter(store, provider_id, "oauth", config, account_id="work") as getter,
            httpx.AsyncClient(base_url="https://inference.example") as client,
        ):

            async def headers() -> dict[str, str]:
                return {"Authorization": f"Bearer {await getter()}"}

            return await post_json_with_retry(
                client,
                "/request",
                {},
                build_headers=headers,
                handle_error_status=classify,
                provider_context="test",
                auth_recovery=OAuthRequestRecovery(getter, auth),
            )

    assert await asyncio.gather(*(invoke() for _ in range(4))) == [{"ok": True}] * 4
    assert route.call_count == 8
    assert exchange.call_count == 1
    saved = store.load(provider_id, "oauth", account_id="work")
    assert saved is not None and saved.access_token == new_token
    assert saved.refresh_token == (
        "old-test-refresh" if config.token_exchange_url else "new-test-refresh"
    )
    assert store.load(provider_id, "oauth") is None


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
        *(
            getter.refresh_after_rejection(stale_access_token, status_code=401, response_body="")
            for getter in getters
        )
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
