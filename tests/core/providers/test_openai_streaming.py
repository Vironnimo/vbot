"""Openai: streaming behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.openai import (
    CODEX_RESPONSES_MODE,
    OpenAIAdapter,
)
from tests.core.providers.openai_helpers import (
    OPENAI_SUBSCRIPTION_URL,
    SAMPLE_MESSAGES,
    _jwt_with_account,
    _RotatingTokenGetter,
    _subscription_config,
)


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
