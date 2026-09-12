"""Token getter: provider refresh behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.token_getter import (
    OAuthTokenGetter,
)
from core.providers.token_store import OAuthToken, TokenStore
from tests.core.providers.token_getter_helpers import (
    MINIMAX_TOKEN_URL,
    NOUS_TOKEN_URL,
    OPENCODE_TOKEN_URL,
    XAI_TOKEN_URL,
    _minimax_oauth_config,
    _nous_oauth_config,
    _opencode_oauth_config,
    _xai_oauth_config,
)


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
