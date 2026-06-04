"""Tests for the OpenAI Subscription provider adapter."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError
from core.providers.openai_subscription import (
    CODEX_RESPONSES_ENDPOINT,
    OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS,
    OpenAISubscriptionAdapter,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENAI_SUBSCRIPTION_URL = f"https://chatgpt.com/backend-api{CODEX_RESPONSES_ENDPOINT}"
SAMPLE_MESSAGES = [
    {"role": "system", "content": "Use concise answers."},
    {"role": "user", "content": "Hello"},
]


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="openai-subscription",
        name="OpenAI Subscription",
        adapter="openai_subscription",
        base_url="https://chatgpt.com/backend-api",
        connections=[
            ConnectionConfig(
                id="oauth",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={
            "OpenAI-Beta": "responses=experimental",
            "originator": "vbot",
        },
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
async def test_send_posts_responses_payload_with_subscription_headers() -> None:
    """send() targets the Codex Responses endpoint with OAuth and account headers."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAISubscriptionAdapter(_config(), access_token)
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
    assert request.headers["OpenAI-Beta"] == "responses=experimental"
    assert request.headers["originator"] == "vbot"
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
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 8192,
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
async def test_send_adds_default_instructions_without_system_message() -> None:
    """The Codex backend requires instructions even for agents without a system prompt."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAISubscriptionAdapter(_config(), access_token)
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(200, json={"id": "resp_1", "output": []})
    )

    await adapter.send([{"role": "user", "content": "Hello"}], model_id="gpt-5.5")

    payload = json.loads(route.calls.last.request.content)
    assert payload["instructions"] == OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS
    assert payload["store"] is False


@respx.mock
@pytest.mark.asyncio
async def test_send_rejects_oauth_token_without_account_id() -> None:
    """Subscription requests need the ChatGPT account id claim from the OAuth JWT."""

    adapter = OpenAISubscriptionAdapter(_config(), "not-a-jwt")
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(ProviderAuthError, match="account id"):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_stream_yields_normalized_responses_deltas() -> None:
    """stream() parses Responses SSE events from the Codex backend."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAISubscriptionAdapter(_config(), access_token)
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
