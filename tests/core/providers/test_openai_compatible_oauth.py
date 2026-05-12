"""OAuth token getter integration tests for OpenAI-compatible adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, OAuthConfig, ProviderConfig
from core.providers.token_getter import OAuthTokenGetter
from core.providers.token_store import OAuthToken, TokenStore

COPILOT_URL = "https://api.githubcopilot.com/chat/completions"
TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
}


def _copilot_config() -> ProviderConfig:
    return ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="openai_compatible",
        base_url="https://api.githubcopilot.com",
        connections=[
            ConnectionConfig(
                id="oauth",
                type="oauth",
                label="Sign in with GitHub",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                oauth=_oauth_config(),
            )
        ],
    )


def _oauth_config() -> OAuthConfig:
    return OAuthConfig(
        flow="device",
        client_id="client-id",
        device_auth_url="https://github.com/login/device/code",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["copilot"],
        token_exchange_url=TOKEN_EXCHANGE_URL,
    )


@respx.mock
@pytest.mark.asyncio
async def test_openai_adapter_send_awaits_oauth_token_getter(tmp_path: Path) -> None:
    """send() obtains the OAuth token before building auth headers."""

    token_store = TokenStore(tmp_path)
    token_store.save("github-copilot", "oauth", OAuthToken(access_token="stored-token"))
    getter = OAuthTokenGetter(token_store, "github-copilot", "oauth", _oauth_config())
    adapter = OpenAICompatibleAdapter(_copilot_config(), getter)
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send([{"role": "user", "content": "Hello"}], model_id="gpt-4o")

    assert route.calls.last.request.headers.get("authorization") == "Bearer stored-token"


@respx.mock
@pytest.mark.asyncio
async def test_openai_adapter_send_uses_refreshed_oauth_token(tmp_path: Path) -> None:
    """send() transparently uses a refreshed token when the stored one expired."""

    token_store = TokenStore(tmp_path)
    token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(
            access_token="expired-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            extra={"github_oauth_token": "github-oauth-secret"},
        ),
    )
    respx.get(TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"token": "refreshed-token"})
    )
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    getter = OAuthTokenGetter(token_store, "github-copilot", "oauth", _oauth_config())
    adapter = OpenAICompatibleAdapter(_copilot_config(), getter)

    await adapter.send([{"role": "user", "content": "Hello"}], model_id="gpt-4o")

    assert route.calls.last.request.headers.get("authorization") == "Bearer refreshed-token"
