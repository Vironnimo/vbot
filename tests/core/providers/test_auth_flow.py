"""Tests for OAuth Device Flow provider authentication."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from core.providers.auth_flow import DeviceFlowEngine, DeviceFlowTerminalError
from core.providers.errors import ProviderError
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
MINIMAX_DEVICE_AUTH_URL = "https://api.minimax.io/oauth/code"
MINIMAX_TOKEN_URL = "https://api.minimax.io/oauth/token"
MINIMAX_VERIFICATION_URI = "https://api.minimax.io/oauth/verify"
XAI_DEVICE_AUTH_URL = "https://auth.x.ai/oauth2/device/code"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"


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


def _minimax_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="minimax-client-id",
        device_auth_url=MINIMAX_DEVICE_AUTH_URL,
        token_url=MINIMAX_TOKEN_URL,
        scopes=["group_id", "profile", "model.completion"],
        device_flow="minimax_oauth",
    )


def _xai_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="xai-client-id",
        device_auth_url=XAI_DEVICE_AUTH_URL,
        token_url=XAI_TOKEN_URL,
        scopes=["openid", "offline_access", "grok-cli:access", "api:access"],
        device_flow="xai_oauth",
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
async def test_start_xai_flow_prefers_complete_verification_uri(tmp_path: Path) -> None:
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    route = respx.post(XAI_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "xai-device-code",
                "user_code": "XAI-CODE",
                "verification_uri": "https://auth.x.ai/device",
                "verification_uri_complete": "https://auth.x.ai/device?user_code=XAI-CODE",
                "expires_in": 900,
                "interval": 5,
            },
        )
    )

    session = await engine.start_device_flow("xai", "subscription", _xai_oauth_config())

    assert session.verification_uri == "https://auth.x.ai/device?user_code=XAI-CODE"
    assert parse_qs(route.calls.last.request.content.decode()) == {
        "client_id": ["xai-client-id"],
        "scope": ["openid offline_access grok-cli:access api:access"],
    }


@respx.mock
@pytest.mark.asyncio
async def test_xai_flow_accepts_http_400_pending_then_saves_rotated_token(
    tmp_path: Path,
) -> None:
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    responses = [
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(
            200,
            json={
                "access_token": "xai-access",
                "refresh_token": "xai-refresh",
                "expires_in": 900,
            },
        ),
    ]
    route = respx.post(XAI_TOKEN_URL).mock(side_effect=responses)

    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock):
        await engine._poll_for_token(
            "xai",
            "subscription",
            _xai_oauth_config(),
            "xai-device-code",
            5,
            900,
            AsyncMock(),
        )

    assert route.call_count == 2
    request_form = parse_qs(route.calls.last.request.content.decode())
    assert request_form == {
        "client_id": ["xai-client-id"],
        "device_code": ["xai-device-code"],
        "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
    }
    token = token_store.load("xai", "subscription")
    assert token is not None
    assert token.access_token == "xai-access"
    assert token.refresh_token == "xai-refresh"


@respx.mock
@pytest.mark.asyncio
async def test_xai_flow_rejects_non_polling_http_400(tmp_path: Path) -> None:
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    respx.post(XAI_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_client"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError):
            await engine._request_device_token(
                client,
                _xai_oauth_config(),
                "xai-device-code",
            )


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
        "openai",
        "subscription",
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
            "openai",
            "subscription",
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
    token = token_store.load("openai", "subscription")
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
async def test_poll_loop_saves_token_under_named_account(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A poll loop for a named account stores its token under that account only."""
    # Arrange
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    on_complete = AsyncMock()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "work-access-secret"})
    )

    # Act
    with caplog.at_level(logging.INFO, logger="vbot.providers.auth_flow"):
        await engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            1,
            900,
            on_complete,
            account_id="work",
        )

    # Assert
    work_token = token_store.load("github-copilot", "oauth", account_id="work")
    assert work_token is not None
    assert work_token.access_token == "work-access-secret"
    assert token_store.load("github-copilot", "oauth") is None
    on_complete.assert_awaited_once_with(success=True)
    auth_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.providers.auth_flow"
    ]
    assert auth_logs == ["OAuth provider connected (provider=github-copilot connection=oauth)"]
    assert "work" not in " ".join(auth_logs)
    assert "work-access-secret" not in " ".join(auth_logs)


@respx.mock
@pytest.mark.asyncio
async def test_cancel_flow_is_account_scoped(tmp_path: Path) -> None:
    """Cancelling one account's flow leaves another account's flow running."""
    # Arrange
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"error": "authorization_pending"})
    )
    sleeps_started: asyncio.Queue[None] = asyncio.Queue()
    release_sleep = asyncio.Event()

    async def sleep_until_released(_interval: int) -> None:
        sleeps_started.put_nowait(None)
        await release_sleep.wait()

    work_task = asyncio.create_task(
        engine._poll_for_token(
            "github-copilot",
            "oauth",
            _oauth_config(),
            "device-code",
            1,
            900,
            AsyncMock(),
            account_id="work",
        )
    )
    default_task = asyncio.create_task(
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
        await sleeps_started.get()
        await sleeps_started.get()
        engine.cancel_flow("github-copilot", "oauth", "work")

        with pytest.raises(asyncio.CancelledError):
            await work_task

        # Assert
        assert work_task.cancelled()
        assert not default_task.done()
        assert ("github-copilot", "oauth", "default") in engine._active_flows
        await engine.aclose()


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


@respx.mock
@pytest.mark.asyncio
async def test_start_minimax_flow_posts_pkce_and_normalizes_millisecond_fields(
    tmp_path: Path,
) -> None:
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    captured_form: dict[str, list[str]] = {}

    def authorization_response(request: httpx.Request) -> httpx.Response:
        captured_form.update(parse_qs(request.content.decode()))
        expires_at_milliseconds = int(
            (datetime.now(UTC) + timedelta(minutes=10)).timestamp() * 1000
        )
        return httpx.Response(
            200,
            json={
                "user_code": "MINIMAX-CODE",
                "verification_uri": MINIMAX_VERIFICATION_URI,
                "expired_in": expires_at_milliseconds,
                "interval": 2500,
                "state": captured_form["state"][0],
            },
        )

    route = respx.post(MINIMAX_DEVICE_AUTH_URL).mock(side_effect=authorization_response)

    session = await engine.start_device_flow("minimax", "subscription", _minimax_oauth_config())

    assert session.device_code == "MINIMAX-CODE"
    assert session.user_code == "MINIMAX-CODE"
    assert session.verification_uri == MINIMAX_VERIFICATION_URI
    assert 595 <= session.expires_in <= 600
    assert session.interval == 3
    assert captured_form["response_type"] == ["code"]
    assert captured_form["client_id"] == ["minimax-client-id"]
    assert captured_form["scope"] == ["group_id profile model.completion"]
    assert captured_form["code_challenge_method"] == ["S256"]
    assert captured_form["code_challenge"][0]
    assert captured_form["state"][0]
    assert route.calls.last.request.headers["x-request-id"]


@respx.mock
@pytest.mark.asyncio
async def test_start_minimax_flow_rejects_state_mismatch(tmp_path: Path) -> None:
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    respx.post(MINIMAX_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "user_code": "MINIMAX-CODE",
                "verification_uri": MINIMAX_VERIFICATION_URI,
                "expired_in": 600,
                "state": "wrong-state",
            },
        )
    )

    with pytest.raises(DeviceFlowTerminalError, match="state_mismatch"):
        await engine.start_device_flow("minimax", "subscription", _minimax_oauth_config())


@respx.mock
@pytest.mark.asyncio
async def test_minimax_flow_polls_pending_then_saves_rotatable_token(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    authorization_form: dict[str, list[str]] = {}
    token_forms: list[dict[str, list[str]]] = []

    def authorization_response(request: httpx.Request) -> httpx.Response:
        authorization_form.update(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "user_code": "MINIMAX-CODE",
                "verification_uri": MINIMAX_VERIFICATION_URI,
                "expired_in": 600,
                "interval": 2000,
                "state": authorization_form["state"][0],
            },
        )

    token_response_count = 0

    def token_response(request: httpx.Request) -> httpx.Response:
        nonlocal token_response_count
        token_response_count += 1
        token_forms.append(parse_qs(request.content.decode()))
        if token_response_count == 1:
            return httpx.Response(200, json={"status": "pending"})
        expires_at_milliseconds = int(
            (datetime.now(UTC) + timedelta(minutes=15)).timestamp() * 1000
        )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "minimax-access",
                "refresh_token": "minimax-refresh",
                "expired_in": expires_at_milliseconds,
            },
        )

    respx.post(MINIMAX_DEVICE_AUTH_URL).mock(side_effect=authorization_response)
    respx.post(MINIMAX_TOKEN_URL).mock(side_effect=token_response)
    session = await engine.start_device_flow("minimax", "subscription", _minimax_oauth_config())
    on_complete = AsyncMock()

    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock):
        await engine._poll_for_token(
            "minimax",
            "subscription",
            _minimax_oauth_config(),
            session.device_code,
            session.interval,
            session.expires_in,
            on_complete,
            user_code=session.user_code,
        )

    token = token_store.load("minimax", "subscription")
    assert token is not None
    assert token.access_token == "minimax-access"
    assert token.refresh_token == "minimax-refresh"
    assert token.expires_at is not None
    assert 890 <= (token.expires_at - datetime.now(UTC)).total_seconds() <= 900
    assert token_response_count == 2
    assert token_forms[-1]["grant_type"] == ["urn:ietf:params:oauth:grant-type:user_code"]
    assert token_forms[-1]["user_code"] == ["MINIMAX-CODE"]
    verifier = token_forms[-1]["code_verifier"][0]
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert authorization_form["code_challenge"] == [expected_challenge]
    on_complete.assert_awaited_once_with(success=True)
