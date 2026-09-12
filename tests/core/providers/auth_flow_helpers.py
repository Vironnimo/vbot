"""Shared fixtures and fakes for auth flow behavior tests."""

from __future__ import annotations

from core.providers.providers import OAuthConfig

DEVICE_AUTH_URL = "https://github.com/login/device/code"

TOKEN_URL = "https://github.com/login/oauth/access_token"

OPENAI_DEVICE_AUTH_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"

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
