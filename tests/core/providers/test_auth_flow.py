"""Auth flow: device flows behavior."""

from __future__ import annotations

import base64
import hashlib
import json
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
from tests.core.providers.auth_flow_helpers import (
    DEVICE_AUTH_URL,
    OPENAI_DEVICE_AUTH_URL,
    OPENAI_VERIFICATION_URI,
    _oauth_config,
    _openai_oauth_config,
)

MINIMAX_DEVICE_AUTH_URL = "https://api.minimax.io/oauth/code"

MINIMAX_TOKEN_URL = "https://api.minimax.io/oauth/token"

MINIMAX_VERIFICATION_URI = "https://api.minimax.io/oauth/verify"

XAI_DEVICE_AUTH_URL = "https://auth.x.ai/oauth2/device/code"

XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"

NOUS_DEVICE_AUTH_URL = "https://portal.nousresearch.com/api/oauth/device/code"

NOUS_TOKEN_URL = "https://portal.nousresearch.com/api/oauth/token"

OPENCODE_DEVICE_AUTH_URL = "https://console.opencode.ai/auth/device/code"

OPENCODE_TOKEN_URL = "https://console.opencode.ai/auth/device/token"


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


def _nous_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="hermes-cli",
        device_auth_url=NOUS_DEVICE_AUTH_URL,
        token_url=NOUS_TOKEN_URL,
        scopes=["inference:invoke"],
        device_flow="nous_oauth",
    )


def _opencode_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="opencode-cli",
        device_auth_url=OPENCODE_DEVICE_AUTH_URL,
        token_url=OPENCODE_TOKEN_URL,
        scopes=[],
        device_flow="opencode_oauth",
    )


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
    session = await engine._request_device_session("github-copilot", "oauth", _oauth_config())

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

    session = await engine._request_device_session("xai", "subscription", _xai_oauth_config())

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
async def test_nous_flow_uses_inference_scope_and_accepts_http_400_pending(
    tmp_path: Path,
) -> None:
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    device_route = respx.post(NOUS_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "nous-device-code",
                "user_code": "NOUS-CODE",
                "verification_uri": "https://portal.nousresearch.com/device",
                "verification_uri_complete": (
                    "https://portal.nousresearch.com/device?user_code=NOUS-CODE"
                ),
                "expires_in": 900,
                "interval": 5,
            },
        )
    )
    token_route = respx.post(NOUS_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200,
                json={
                    "access_token": "nous-access",
                    "refresh_token": "nous-refresh",
                    "expires_in": 900,
                    "scope": "inference:invoke",
                },
            ),
        ]
    )

    session = await engine._request_device_session("nous", "subscription", _nous_oauth_config())
    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock):
        await engine._poll_for_token(
            "nous",
            "subscription",
            _nous_oauth_config(),
            session.device_code,
            session.interval,
            session.expires_in,
            AsyncMock(),
        )

    assert session.verification_uri.endswith("user_code=NOUS-CODE")
    assert parse_qs(device_route.calls.last.request.content.decode()) == {
        "client_id": ["hermes-cli"],
        "scope": ["inference:invoke"],
    }
    assert token_route.call_count == 2
    stored = token_store.load("nous", "subscription")
    assert stored is not None
    assert stored.access_token == "nous-access"
    assert stored.refresh_token == "nous-refresh"
    assert stored.extra == {"oauth_scope": "inference:invoke"}


@respx.mock
@pytest.mark.asyncio
async def test_nous_flow_rejects_explicitly_missing_inference_scope(tmp_path: Path) -> None:
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    respx.post(NOUS_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "wrong-access",
                "refresh_token": "wrong-refresh",
                "expires_in": 900,
                "scope": "profile",
            },
        )
    )

    async with httpx.AsyncClient() as client:
        data = await engine._request_device_token(
            client,
            _nous_oauth_config(),
            "nous-device-code",
        )
        with pytest.raises(DeviceFlowTerminalError, match="missing_inference_invoke_scope"):
            await engine._build_token(client, _nous_oauth_config(), data)


@respx.mock
@pytest.mark.asyncio
async def test_opencode_flow_posts_json_accepts_pending_and_stores_rotating_token(
    tmp_path: Path,
) -> None:
    token_store = TokenStore(tmp_path)
    engine = DeviceFlowEngine(token_store)
    device_route = respx.post(OPENCODE_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "zen-device-code",
                "user_code": "ZEN-CODE",
                "verification_uri": "https://console.opencode.ai/device",
                "verification_uri_complete": (
                    "https://console.opencode.ai/device?user_code=ZEN-CODE"
                ),
                "expires_in": 900,
                "interval": 5,
            },
        )
    )
    token_route = respx.post(OPENCODE_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200,
                json={
                    "access_token": "zen-access",
                    "refresh_token": "zen-refresh",
                    "expires_in": 900,
                },
            ),
        ]
    )

    session = await engine._request_device_session(
        "opencode-zen",
        "account",
        _opencode_oauth_config(),
    )
    with patch("core.providers.auth_flow.asyncio.sleep", new_callable=AsyncMock):
        await engine._poll_for_token(
            "opencode-zen",
            "account",
            _opencode_oauth_config(),
            session.device_code,
            session.interval,
            session.expires_in,
            AsyncMock(),
        )

    assert json.loads(device_route.calls.last.request.content) == {"client_id": "opencode-cli"}
    assert token_route.call_count == 2
    assert json.loads(token_route.calls.last.request.content) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": "zen-device-code",
        "client_id": "opencode-cli",
    }
    stored = token_store.load("opencode-zen", "account")
    assert stored is not None
    assert stored.access_token == "zen-access"
    assert stored.refresh_token == "zen-refresh"


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
    session = await engine._request_device_session(
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

    session = await engine._request_device_session(
        "minimax", "subscription", _minimax_oauth_config()
    )

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
        await engine._request_device_session("minimax", "subscription", _minimax_oauth_config())


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
    session = await engine._request_device_session(
        "minimax", "subscription", _minimax_oauth_config()
    )
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
