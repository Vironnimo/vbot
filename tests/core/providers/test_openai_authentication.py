"""Openai: authentication behavior."""

from __future__ import annotations

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError
from core.providers.openai import (
    CODEX_RESPONSES_MODE,
    OpenAIAdapter,
)
from tests.core.providers.openai_helpers import (
    OPENAI_SUBSCRIPTION_URL,
    SAMPLE_MESSAGES,
    _codex_sse_response,
    _jwt_with_account,
    _subscription_config,
)


class _RefreshableTokenGetter:
    """Token getter that replaces one Provider-rejected OAuth token."""

    def __init__(self, stale_token: str, fresh_token: str) -> None:
        self._token = stale_token
        self._fresh_token = fresh_token
        self.refresh_calls: list[str] = []

    async def __call__(self) -> str:
        return self._token

    async def refresh_after_rejection(
        self, rejected_access_token: str, *, status_code: int, response_body: str
    ) -> str | None:
        if status_code != 401:
            return None
        self.refresh_calls.append(rejected_access_token)
        self._token = self._fresh_token
        return self._token


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_rejects_oauth_token_without_account_id() -> None:
    """Subscription requests need the ChatGPT account id claim from the OAuth JWT."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        "not-a-jwt",
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(ProviderAuthError):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_refreshes_provider_rejected_oauth_token_once() -> None:
    """A Provider 401 forces one refresh even when local expiry has not elapsed."""

    stale_token = _jwt_with_account("acct-stale")
    fresh_token = _jwt_with_account("acct-fresh")
    token_getter = _RefreshableTokenGetter(stale_token, fresh_token)
    adapter = OpenAIAdapter(
        _subscription_config(),
        token_getter,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        side_effect=[
            httpx.Response(
                401,
                json={"error": {"code": "token_expired", "message": "Token expired"}},
            ),
            _codex_sse_response({"id": "resp_1", "status": "completed", "output": []}),
        ]
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    assert route.call_count == 2
    assert token_getter.refresh_calls == [stale_token]
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {stale_token}"
    assert route.calls[0].request.headers["chatgpt-account-id"] == "acct-stale"
    assert route.calls[1].request.headers["Authorization"] == f"Bearer {fresh_token}"
    assert route.calls[1].request.headers["chatgpt-account-id"] == "acct-fresh"


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_does_not_loop_when_refreshed_token_is_rejected() -> None:
    """A second 401 surfaces after the single auth-recovery attempt."""

    stale_token = _jwt_with_account("acct-stale")
    token_getter = _RefreshableTokenGetter(
        stale_token,
        _jwt_with_account("acct-fresh"),
    )
    adapter = OpenAIAdapter(
        _subscription_config(),
        token_getter,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"code": "token_expired", "message": "Token expired"}},
        )
    )

    with pytest.raises(ProviderAuthError) as exc_info:
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    assert getattr(exc_info.value, "status_code", None) == 401
    assert route.call_count == 2
    assert token_getter.refresh_calls == [stale_token]
