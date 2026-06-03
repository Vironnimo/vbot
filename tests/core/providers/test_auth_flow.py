"""Tests for OAuth Device Flow provider authentication."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from core.providers.auth_flow import DeviceFlowEngine
from core.providers.providers import OAuthConfig
from core.providers.token_store import TokenStore

DEVICE_AUTH_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
OPENAI_DEVICE_AUTH_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
OPENAI_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_VERIFICATION_URI = "https://auth.openai.com/codex/device"
OPENAI_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"


def _oauth_config(*, token_exchange_url: str | None = None) -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="client-id",
        device_auth_url=DEVICE_AUTH_URL,
        token_url=TOKEN_URL,
        scopes=["read:user"],
        token_exchange_url=token_exchange_url,
    )


def _openai_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="client-id",
        device_auth_url=OPENAI_DEVICE_AUTH_URL,
        token_url=OPENAI_TOKEN_URL,
        scopes=["openid", "profile", "email", "offline_access"],
        device_flow="openai_codex",
        verification_uri=OPENAI_VERIFICATION_URI,
        redirect_uri=OPENAI_REDIRECT_URI,
        expires_in=600,
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


@respx.mock
@pytest.mark.asyncio
async def test_start_device_flow_posts_client_id_and_scope(tmp_path: Path) -> None:
    """Starting a device flow returns the user-facing session data."""
    # Arrange
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    route = respx.post(DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "device-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 3,
            },
        )
    )

    # Act
    session = await engine.start_device_flow("github-copilot", "oauth", _oauth_config())

    # Assert
    assert session.device_code == "device-code"
    assert session.user_code == "ABCD-EFGH"
    assert session.verification_uri == "https://github.com/login/device"
    assert session.expires_in == 900
    assert session.interval == 3
    assert route.calls.last.request.content == b"client_id=client-id&scope=read%3Auser"


@respx.mock
@pytest.mark.asyncio
async def test_start_openai_device_flow_posts_json_and_uses_configured_verification_uri(
    tmp_path: Path,
) -> None:
    """OpenAI Codex Device Flow uses the provider-specific JSON usercode endpoint."""
    # Arrange
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    route = respx.post(OPENAI_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "device_auth_id": "device-auth-id",
                "user_code": "WXYZ-1234",
                "interval": 2,
            },
        )
    )

    # Act
    session = await engine.start_device_flow(
        "openai-subscription",
        "oauth",
        _openai_oauth_config(),
    )

    # Assert
    assert session.device_code == "device-auth-id"
    assert session.user_code == "WXYZ-1234"
    assert session.verification_uri == OPENAI_VERIFICATION_URI
    assert session.expires_in == 600
    assert session.interval == 2
    assert json.loads(route.calls.last.request.content) == {"client_id": "client-id"}


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_success_stores_token_and_fires_on_complete(tmp_path: Path) -> None:
    """A successful poll response is persisted and reports completion."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    on_complete = AsyncMock()
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "provider-access-secret",
                "refresh_token": "provider-refresh-secret",
                "expires_in": 600,
            },
        )
    )

    # Act
    with patch("core.providers.auth_flow.datetime") as datetime_mock:
        datetime_mock.now.return_value = expires_at - timedelta(seconds=600)
        datetime_mock.fromisoformat.side_effect = datetime.fromisoformat
        await engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            1,
            900,
            on_complete,
        )

    # Assert
    token = token_store.load("github-copilot", "oauth")
    assert token is not None
    assert token.access_token == "provider-access-secret"
    assert token.refresh_token == "provider-refresh-secret"
    assert token.expires_at == expires_at
    on_complete.assert_awaited_once_with(success=True)


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_exchanges_openai_device_authorization_code(
    tmp_path: Path,
) -> None:
    """OpenAI's Device Flow polls for an auth code, then exchanges it for OAuth tokens."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    on_complete = AsyncMock()
    access_token = _jwt_with_account("acct_openai")
    device_route = respx.post(OPENAI_DEVICE_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(403, json={"message": "not authorized yet"}),
            httpx.Response(
                200,
                json={
                    "authorization_code": "authorization-code",
                    "code_verifier": "code-verifier",
                },
            ),
        ]
    )
    token_route = respx.post(OPENAI_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
            },
        )
    )

    # Act
    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        await engine._poll_for_token(
            "openai-subscription",
            "oauth",
            _openai_oauth_config(),
            "device-auth-id",
            2,
            600,
            on_complete,
            user_code="WXYZ-1234",
        )

    # Assert
    assert device_route.call_count == 2
    assert json.loads(device_route.calls[0].request.content) == {
        "device_auth_id": "device-auth-id",
        "user_code": "WXYZ-1234",
    }
    sleep_mock.assert_awaited_once_with(2)
    token_request = parse_qs(token_route.calls.last.request.content.decode("utf-8"))
    assert token_request == {
        "grant_type": ["authorization_code"],
        "client_id": ["client-id"],
        "code": ["authorization-code"],
        "code_verifier": ["code-verifier"],
        "redirect_uri": [OPENAI_REDIRECT_URI],
    }
    token = token_store.load("openai-subscription", "oauth")
    assert token is not None
    assert token.access_token == access_token
    assert token.refresh_token == "refresh-secret"
    assert token.expires_at is not None
    assert token.extra == {"chatgpt_account_id": "acct_openai"}
    on_complete.assert_awaited_once_with(success=True)


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_waits_on_authorization_pending(tmp_path: Path) -> None:
    """authorization_pending keeps polling until a token is available."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"error": "authorization_pending"}),
            httpx.Response(200, json={"access_token": "provider-access-secret"}),
        ]
    )
    on_complete = AsyncMock()

    # Act
    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        await engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            7,
            900,
            on_complete,
        )

    # Assert
    assert route.call_count == 2
    sleep_mock.assert_awaited_once_with(7)
    assert token_store.load("github-copilot", "oauth") is not None
    on_complete.assert_awaited_once_with(success=True)


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_stops_when_device_flow_session_expires(tmp_path: Path) -> None:
    """authorization_pending stops polling after the device-code session expires."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"error": "authorization_pending"})
    )
    on_complete = AsyncMock()

    # Act
    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        await engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            7,
            0,
            on_complete,
        )

    # Assert
    sleep_mock.assert_not_awaited()
    assert token_store.load("github-copilot", "oauth") is None
    on_complete.assert_awaited_once_with(success=False)


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_increases_interval_on_slow_down(tmp_path: Path) -> None:
    """slow_down increases the poll interval before the next request."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "provider-access-secret"}),
        ]
    )

    # Act
    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
        await engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            7,
            900,
            AsyncMock(),
        )

    # Assert
    sleep_mock.assert_awaited_once_with(12)


@pytest.mark.parametrize("error_code", ["expired_token", "access_denied"])
@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_terminal_errors_fire_failure(
    tmp_path: Path,
    error_code: str,
) -> None:
    """Terminal Device Flow errors report unsuccessful completion."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    on_complete = AsyncMock()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"error": error_code}))

    # Act
    await engine._poll_for_token(
        "github-copilot",
        "oauth",
        _oauth_config(),
        "device-code",
        1,
        900,
        on_complete,
    )

    # Assert
    assert token_store.load("github-copilot", "oauth") is None
    on_complete.assert_awaited_once_with(success=False)


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_exchanges_copilot_token_and_stores_github_token(
    tmp_path: Path,
) -> None:
    """Copilot exchanges the GitHub OAuth token before storing provider auth."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    expires_at = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "github-oauth-secret"})
    )
    exchange_route = respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"token": "copilot-api-secret", "expires_at": expires_at.isoformat()},
        )
    )

    # Act
    await engine._poll_for_token(
        "github-copilot",
        "oauth",
        _oauth_config(token_exchange_url=TOKEN_EXCHANGE_URL),
        "device-code",
        1,
        900,
        AsyncMock(),
    )

    # Assert
    token = token_store.load("github-copilot", "oauth")
    assert token is not None
    assert token.access_token == "copilot-api-secret"
    assert token.refresh_token is None
    assert token.expires_at == expires_at
    assert token.extra == {"github_oauth_token": "github-oauth-secret"}
    exchange_headers = exchange_route.calls.last.request.headers
    assert exchange_headers["Accept"] == "application/json"
    assert exchange_headers["Authorization"] == "Bearer github-oauth-secret"
    assert exchange_headers["Copilot-Integration-Id"] == "vscode-chat"
    assert exchange_headers["Editor-Version"] == "vBot/0.1.0"


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_reports_failure_when_copilot_token_exchange_fails(
    tmp_path: Path,
) -> None:
    """A post-authorization Copilot exchange failure notifies the UI waiter."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    on_complete = AsyncMock()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "github-oauth-secret"})
    )
    respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )

    # Act
    await engine._poll_for_token(
        "github-copilot",
        "oauth",
        _oauth_config(token_exchange_url=TOKEN_EXCHANGE_URL),
        "device-code",
        1,
        900,
        on_complete,
    )

    # Assert
    assert token_store.load("github-copilot", "oauth") is None
    on_complete.assert_awaited_once_with(success=False)


@respx.mock
@pytest.mark.asyncio
async def test_poll_loop_reports_failure_before_reraising_unexpected_errors(
    tmp_path: Path,
) -> None:
    """Unexpected polling task crashes still release the UI from waiting."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    on_complete = AsyncMock()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))

    # Act / Assert
    with pytest.raises(KeyError):
        await engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            1,
            900,
            on_complete,
        )

    assert token_store.load("github-copilot", "oauth") is None
    on_complete.assert_awaited_once_with(success=False)


@respx.mock
@pytest.mark.asyncio
async def test_cancel_flow_cancels_in_flight_polling_task(tmp_path: Path) -> None:
    """Cancelling an active flow cancels its polling task."""
    # Arrange
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"error": "authorization_pending"})
    )
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    async def sleep_until_released(_interval: int) -> None:
        sleep_started.set()
        await release_sleep.wait()

    task = asyncio.create_task(
        engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            1,
            900,
            AsyncMock(),
        )
    )

    # Act
    with patch("core.providers.auth_flow.asyncio.sleep", side_effect=sleep_until_released):
        await sleep_started.wait()
        engine.cancel_flow("github-copilot", "oauth")

        with pytest.raises(asyncio.CancelledError):
            await task

    # Assert
    assert task.cancelled()


@respx.mock
@pytest.mark.asyncio
async def test_aclose_cancels_active_polling_tasks(tmp_path: Path) -> None:
    """Closing the engine cancels and awaits active polling tasks."""
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"error": "authorization_pending"})
    )
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    async def sleep_until_released(_interval: int) -> None:
        sleep_started.set()
        await release_sleep.wait()

    task = asyncio.create_task(
        engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            1,
            900,
            AsyncMock(),
        )
    )

    with patch("core.providers.auth_flow.asyncio.sleep", side_effect=sleep_until_released):
        await sleep_started.wait()
        await engine.aclose()

    assert task.cancelled()
    assert engine._active_flows == {}
