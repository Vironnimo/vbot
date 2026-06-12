"""Tests for the unified OpenAI provider adapter.

Covers both the default ``/chat/completions`` mode (``api-key`` connection)
and the Codex Responses mode (``subscription`` connection with
``connection_mode="codex_responses"``).
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError
from core.providers.openai import (
    CODEX_EXTRA_HEADERS,
    CODEX_RESPONSES_ENDPOINT,
    CODEX_RESPONSES_MODE,
    OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS,
    OpenAIAdapter,
)
from core.providers.openai_compatible import CHAT_COMPLETIONS_ENDPOINT
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENAI_API_KEY_URL = f"https://api.openai.com/v1{CHAT_COMPLETIONS_ENDPOINT}"
OPENAI_SUBSCRIPTION_URL = f"https://chatgpt.com/backend-api{CODEX_RESPONSES_ENDPOINT}"
SAMPLE_MESSAGES = [
    {"role": "system", "content": "Use concise answers."},
    {"role": "user", "content": "Hello"},
]


def _platform_config() -> ProviderConfig:
    """Provider config matching the OpenAI Platform ``api-key`` connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


def _subscription_config(*, include_mode: bool = True) -> ProviderConfig:
    """Provider config matching the ChatGPT ``subscription`` connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://chatgpt.com/backend-api",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                mode=CODEX_RESPONSES_MODE if include_mode else None,
            )
        ],
        defaults={"max_tokens": 8192},
    )


class _RotatingTokenGetter:
    """Async token getter that yields a fresh token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token


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


# ------------------------------------------------------------------
# Codex Responses mode (subscription connection)
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_posts_responses_payload_with_account_and_beta_headers() -> None:
    """Codex send() targets ``/codex/responses`` with the unified Codex headers."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_1",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Hi"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )
    )

    response = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-codex",
        thinking_effort="max",
        response_format={"type": "json_object"},
        temperature=0.2,
        tools=[
            {
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object"},
            }
        ],
    )

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {access_token}"
    assert request.headers["chatgpt-account-id"] == "acct_openai"
    assert request.headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert request.headers["originator"] == CODEX_EXTRA_HEADERS["originator"]
    payload = json.loads(request.content)
    assert payload == {
        "model": "gpt-5-codex",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "instructions": "Use concise answers.",
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object"},
            }
        ],
        "reasoning": {"effort": "xhigh", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "text": {"format": {"type": "json_object"}},
        "store": False,
    }
    assert adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Hi",
        "reasoning": None,
        "reasoning_meta": {"response_id": "resp_1"},
        "tool_calls": None,
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_preserves_xhigh_reasoning_effort() -> None:
    """GPT-5.5 advertises xhigh reasoning through the Codex models endpoint."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(200, json={"id": "resp_1", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5", thinking_effort="xhigh")

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_adds_default_instructions_without_system_message() -> None:
    """The Codex backend requires instructions even without a system prompt."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(200, json={"id": "resp_1", "output": []})
    )

    await adapter.send([{"role": "user", "content": "Hello"}], model_id="gpt-5.5")

    payload = json.loads(route.calls.last.request.content)
    assert payload["instructions"] == OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS
    assert payload["store"] is False
    assert "max_output_tokens" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_unsupported_output_token_limits() -> None:
    """The Codex backend rejects Responses output-token limit parameters."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(200, json={"id": "resp_1", "output": []})
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.5",
        max_tokens=2048,
        max_output_tokens=1024,
    )

    payload = json.loads(route.calls.last.request.content)
    assert "max_output_tokens" not in payload


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

    with pytest.raises(ProviderAuthError, match="account id"):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_codex_stream_yields_normalized_responses_deltas() -> None:
    """stream() parses Responses SSE events from the Codex backend."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hel"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed",'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    )
    respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for chunk in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-codex"):
        chunks.append(chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Hel"},
        {"type": "reasoning_meta", "reasoning_meta": {"response_id": "resp_1"}},
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_codex_stream_rebuilds_headers_per_connect_attempt() -> None:
    """A retried Codex stream connect re-consults the token getter (OAuth refresh)."""

    token_getter = _RotatingTokenGetter(
        [_jwt_with_account("acct-stale"), _jwt_with_account("acct-fresh")]
    )
    adapter = OpenAIAdapter(
        _subscription_config(),
        token_getter,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n'
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"}),
        ]
    )

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-codex"):
            pass

    # The retry rebuilds headers, so the refreshed token's account id is used.
    assert route.call_count == 2
    assert route.calls[0].request.headers.get("chatgpt-account-id") == "acct-stale"
    assert route.calls[1].request.headers.get("chatgpt-account-id") == "acct-fresh"


def test_codex_discovery_headers_merge_extra_headers() -> None:
    """Discovery merges the adapter-owned Codex headers on top of caller headers."""

    access_token = _jwt_with_account("acct_openai")
    headers = OpenAIAdapter.discovery_headers(
        _subscription_config(),
        access_token,
        {"User-Agent": "vbot-test"},
    )

    assert headers["User-Agent"] == "vbot-test"
    assert headers["chatgpt-account-id"] == "acct_openai"
    assert headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert headers["originator"] == CODEX_EXTRA_HEADERS["originator"]


# ------------------------------------------------------------------
# Default mode (api-key connection → /chat/completions)
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_default_mode_send_targets_chat_completions_endpoint() -> None:
    """Default mode delegates to the inherited ``/chat/completions`` request."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    route = respx.post(OPENAI_API_KEY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello back",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )
    )

    response = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-test"
    # Codex-specific headers must NOT leak into the Platform request.
    assert "OpenAI-Beta" not in request.headers
    assert "originator" not in request.headers
    assert "chatgpt-account-id" not in request.headers
    payload = json.loads(request.content)
    assert payload == {
        "model": "gpt-5.2",
        "messages": [
            {"role": "system", "content": "Use concise answers."},
            {"role": "user", "content": "Hello"},
        ],
        "max_tokens": 8192,
    }
    assert response == {
        "id": "chatcmpl-1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello back",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }


@respx.mock
@pytest.mark.asyncio
async def test_default_mode_normalize_response_falls_back_to_openai_compatible() -> None:
    """Default-mode normalize_response uses the inherited chat/completions shape."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Hi there",
                }
            }
        ]
    }

    normalized = adapter.normalize_response(response)
    assert normalized == {
        "role": "assistant",
        "content": "Hi there",
        "reasoning": None,
        "reasoning_meta": None,
        "tool_calls": None,
    }


def test_default_mode_inherits_connection_mode_none() -> None:
    """Without ``connection_mode`` the adapter defaults to chat/completions mode."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    assert adapter._connection_mode is None


def test_codex_mode_stores_connection_mode() -> None:
    """The adapter records the connection mode set at construction time."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
    )
    assert adapter._connection_mode == CODEX_RESPONSES_MODE
