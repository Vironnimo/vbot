"""Shared OAuth flow configurations and token helpers for the Provider auth tests."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

from core.providers.providers import (
    MINIMAX_OAUTH_DEVICE_FLOW,
    NOUS_OAUTH_DEVICE_FLOW,
    OPENAI_CODEX_DEVICE_FLOW,
    OPENCODE_OAUTH_DEVICE_FLOW,
    XAI_OAUTH_DEVICE_FLOW,
    OAuthConfig,
)
from core.providers.token_store import OAuthToken

GITHUB_DEVICE_AUTH_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"

OPENAI_DEVICE_AUTH_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
OPENAI_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_VERIFICATION_URI = "https://auth.openai.com/codex/device"
OPENAI_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"

MINIMAX_DEVICE_AUTH_URL = "https://api.minimax.io/oauth/code"
MINIMAX_TOKEN_URL = "https://api.minimax.io/oauth/token"
XAI_DEVICE_AUTH_URL = "https://auth.x.ai/oauth2/device/code"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
NOUS_DEVICE_AUTH_URL = "https://portal.nousresearch.com/api/oauth/device/code"
NOUS_TOKEN_URL = "https://portal.nousresearch.com/api/oauth/token"
OPENCODE_DEVICE_AUTH_URL = "https://opencode.ai/console/auth/device/code"
OPENCODE_TOKEN_URL = "https://opencode.ai/console/auth/device/token"


def github_oauth_config(*, token_exchange: bool = False) -> OAuthConfig:
    """The standard RFC 8628 flow, optionally with GitHub Copilot's token exchange."""

    return OAuthConfig(
        flow="device",
        client_id="client-id",
        device_auth_url=GITHUB_DEVICE_AUTH_URL,
        token_url=GITHUB_TOKEN_URL,
        scopes=["read:user"],
        token_exchange_url=COPILOT_TOKEN_EXCHANGE_URL if token_exchange else None,
    )


def openai_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="openai-client-id",
        device_auth_url=OPENAI_DEVICE_AUTH_URL,
        token_url=OPENAI_TOKEN_URL,
        scopes=["openid", "profile", "email", "offline_access"],
        device_flow=OPENAI_CODEX_DEVICE_FLOW,
        verification_uri=OPENAI_VERIFICATION_URI,
        redirect_uri=OPENAI_REDIRECT_URI,
        expires_in=600,
    )


def minimax_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="minimax-client-id",
        device_auth_url=MINIMAX_DEVICE_AUTH_URL,
        token_url=MINIMAX_TOKEN_URL,
        scopes=["group_id", "profile", "model.completion"],
        device_flow=MINIMAX_OAUTH_DEVICE_FLOW,
    )


def xai_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="xai-client-id",
        device_auth_url=XAI_DEVICE_AUTH_URL,
        token_url=XAI_TOKEN_URL,
        scopes=["openid", "offline_access", "grok-cli:access", "api:access"],
        device_flow=XAI_OAUTH_DEVICE_FLOW,
    )


def nous_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="hermes-cli",
        device_auth_url=NOUS_DEVICE_AUTH_URL,
        token_url=NOUS_TOKEN_URL,
        scopes=["inference:invoke"],
        device_flow=NOUS_OAUTH_DEVICE_FLOW,
    )


def opencode_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="opencode-cli",
        device_auth_url=OPENCODE_DEVICE_AUTH_URL,
        token_url=OPENCODE_TOKEN_URL,
        scopes=[],
        device_flow=OPENCODE_OAUTH_DEVICE_FLOW,
    )


def jwt_with_account(account_id: str) -> str:
    """An unsigned OpenAI-style access token carrying a ChatGPT account claim."""

    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def expired_token(
    *,
    access_token: str = "expired-access",
    refresh_token: str | None = "refresh-secret",
    extra: dict[str, str] | None = None,
) -> OAuthToken:
    """A stored token already past its expiry, so the next getter call refreshes it."""

    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        extra=extra or {},
    )


def seconds_until(moment: datetime | None) -> float:
    assert moment is not None
    return (moment - datetime.now(UTC)).total_seconds()


def request_body(request: httpx.Request) -> dict[str, Any]:
    """Decode a form or JSON request body into one flat mapping."""

    if request.headers.get("content-type", "").startswith("application/json"):
        return dict(json.loads(request.content))
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
