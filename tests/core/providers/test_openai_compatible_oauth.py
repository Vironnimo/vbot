"""OAuth token getter integration tests for OpenAI-compatible adapter."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.nous import NousAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.token_getter import OAuthTokenGetter
from core.providers.token_store import OAuthToken, TokenStore
from core.providers.xai import XAIAdapter

COPILOT_URL = "https://api.githubcopilot.com/chat/completions"
TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
}

OAUTH_WIRES = [
    ("openai", "gpt-5.6-sol", "responses"),
    ("xai", "grok-4.5", "responses"),
    ("nous", "anthropic/claude-sonnet-4.6", "chat_completions"),
    ("minimax", "MiniMax-M2.7", "messages"),
    ("github-copilot", "gpt-5.4", "responses"),
    ("github-copilot", "claude-haiku-4.5", "messages"),
    ("github-copilot", "gemini-3.1-pro-preview", "chat_completions"),
    ("opencode-zen", "gpt-5.6-sol", "responses"),
    ("opencode-zen", "claude-sonnet-5", "messages"),
    ("opencode-zen", "deepseek-v4-flash", "chat_completions"),
    ("opencode-zen", "gemini-3.5-flash", "gemini_generate_content"),
]
ADAPTERS = {
    "openai": OpenAIAdapter,
    "xai": XAIAdapter,
    "nous": NousAdapter,
    "minimax": MiniMaxAdapter,
    "github-copilot": GitHubCopilotAdapter,
    "opencode-zen": OpenCodeZenAdapter,
}


def _test_jwt(account: str) -> str:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class RefreshingGetter:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.token = _test_jwt("old-test-account")
        self.rejected: list[str] = []
        self.failure = failure

    async def __call__(self) -> str:
        return self.token

    async def refresh_after_rejection(
        self, rejected: str, *, status_code: int, response_body: str
    ) -> str | None:
        if status_code != 401:
            return None
        self.rejected.append(rejected)
        if self.failure is not None:
            raise self.failure
        self.token = _test_jwt("new-test-account")
        return self.token


@respx.mock
@pytest.mark.asyncio
async def test_oauth_does_not_restart_a_stream_after_visible_output() -> None:
    getter = RefreshingGetter()
    adapter = OpenAICompatibleAdapter(_copilot_config(), getter)
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "visible output"}}]},
        {
            "error": {
                "type": "authentication_error",
                "code": "invalid_api_key",
                "message": "test rejection",
            }
        },
    ]
    route = respx.post(COPILOT_URL).mock(
        return_value=httpx.Response(
            200,
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks),
            headers={"Content-Type": "text/event-stream"},
        )
    )
    stream = adapter.stream([{"role": "user", "content": "test"}], model_id="test-model")
    try:
        assert (await anext(stream))["text"] == "visible output"
        with pytest.raises(ProviderAuthError):
            await anext(stream)
        assert route.call_count == 1
        assert getter.rejected == []
    finally:
        await adapter.aclose()


def _success_wire_response(wire: str, streaming: bool) -> httpx.Response:
    if wire == "chat_completions":
        payload: dict[str, Any] = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ]
        }
        events = [payload]
    elif wire == "responses":
        payload = {
            "id": "resp-test",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
        }
        events = [
            {"type": "response.output_text.delta", "delta": "ok"},
            {"type": "response.completed", "response": payload},
        ]
    elif wire == "messages":
        payload = {
            "id": "msg-test",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        events = [
            {"type": "message_start", "message": {**payload, "content": [], "stop_reason": None}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    else:
        payload = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ]
        }
        events = [payload]
    if not streaming:
        return httpx.Response(200, json=payload)
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    if wire == "chat_completions":
        body += "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_id", "model_id", "wire"), OAUTH_WIRES)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "outcome", ["success", "rejected_again", "forbidden", "refresh_failure", "static_key"]
)
async def test_oauth_recovery_across_bundled_provider_wires(
    provider_id: str,
    model_id: str,
    wire: str,
    streaming: bool,
    outcome: str,
) -> None:
    config = ProviderRegistry.load(Path("resources")).get(provider_id)
    connection = next(item for item in config.connections if item.type == "oauth")
    endpoint = {
        "responses": "/codex/responses" if provider_id == "openai" else "/responses",
        "chat_completions": "/chat/completions",
        "messages": "/v1/messages" if provider_id == "github-copilot" else "/messages",
        "gemini_generate_content": f"/models/{model_id}:"
        + ("streamGenerateContent?alt=sse" if streaming else "generateContent"),
    }[wire]
    metadata: dict[str, Any] = {}
    if provider_id == "opencode-zen":
        metadata = {"opencode_zen": {"protocol": wire}}
    elif provider_id == "github-copilot":
        metadata = {
            "github_copilot": {
                "family": model_id,
                "supported_endpoints": [endpoint],
                "streaming": True,
            }
        }
    model = Model(
        model_id=model_id,
        name=model_id,
        family=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=32768,
        max_output_tokens=4096,
        metadata=metadata,
    )
    failure = (
        ProviderError("test refresh unavailable", retryable=True)
        if outcome == "refresh_failure"
        else None
    )
    getter = RefreshingGetter(failure=failure)
    adapter = ADAPTERS[provider_id](
        config,
        getter.token if outcome == "static_key" else getter,
        base_url=connection.base_url or config.base_url,
        auth_config=connection.auth,
        model_lookup=lambda _model_id: model,
        connection_mode=connection.mode,
    )
    initial_status = 403 if outcome == "forbidden" else 401
    second_response = (
        httpx.Response(401, json={"error": {"type": "AuthError"}})
        if outcome == "rejected_again"
        else _success_wire_response(wire, streaming or provider_id == "openai")
    )
    route = respx.post((connection.base_url or config.base_url).rstrip("/") + endpoint).mock(
        side_effect=[
            httpx.Response(initial_status, json={"error": {"type": "AuthError"}}),
            second_response,
        ]
    )

    async def invoke() -> Any:
        messages = [{"role": "user", "content": "Test request"}]
        if streaming:
            return [delta async for delta in adapter.stream(messages, model_id=model_id)]
        return await adapter.send(messages, model_id=model_id)

    try:
        if outcome == "success":
            assert await invoke()
        else:
            with pytest.raises(ProviderError) as caught:
                await invoke()
            if failure is not None:
                assert caught.value is failure
            else:
                assert isinstance(caught.value, ProviderAuthError)
    finally:
        await adapter.aclose()
    should_refresh = outcome not in {"forbidden", "static_key"}
    assert getter.rejected == ([_test_jwt("old-test-account")] if should_refresh else [])
    assert route.call_count == (2 if outcome in {"success", "rejected_again"} else 1)
    if route.call_count == 2:
        auth_header = (
            "x-goog-api-key"
            if wire == "gemini_generate_content"
            else "x-api-key"
            if provider_id == "opencode-zen" and wire == "messages"
            else connection.auth.header
        )
        prefix = "" if auth_header in {"x-goog-api-key", "x-api-key"} else connection.auth.prefix
        assert route.calls[1].request.headers[auth_header] == prefix + _test_jwt("new-test-account")
        if provider_id == "openai":
            assert route.calls[1].request.headers["chatgpt-account-id"] == "new-test-account"


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
