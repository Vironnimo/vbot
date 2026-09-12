"""Shared fixtures and fakes for token getter behavior tests."""

from __future__ import annotations

import pytest

from core.providers.providers import OAuthConfig

PROVIDER_ID = "github-copilot"

CONNECTION_ID = "oauth"

TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"

MINIMAX_TOKEN_URL = "https://api.minimax.io/oauth/token"

XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"

NOUS_TOKEN_URL = "https://portal.nousresearch.com/api/oauth/token"

OPENCODE_TOKEN_URL = "https://console.opencode.ai/auth/device/token"


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
