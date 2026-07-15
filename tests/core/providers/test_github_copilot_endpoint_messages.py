"""Tests for GitHubCopilotAdapter behavior."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.errors import NetworkError, ProviderError, ProviderTimeoutError
from core.providers.github_copilot import (
    GitHubCopilotAdapter,
)
from core.providers.github_copilot_policy import CHAT_COMPLETIONS_ENDPOINT
from tests.core.providers.github_copilot_test_support import (
    API_KEY,
    COPILOT_CONFIG,
    COPILOT_URL,
    MESSAGES_URL,
    SAMPLE_MESSAGES,
    _BrokenStreamResponse,
    _copilot_metadata_lookup,
    _copilot_model_with_metadata,
)
from tests.core.providers.github_copilot_test_support import (
    copilot_adapter as _copilot_adapter_fixture,
)
from tests.core.providers.github_copilot_test_support import (
    metadata_copilot_adapter as _metadata_copilot_adapter_fixture,
)

copilot_adapter = _copilot_adapter_fixture
metadata_copilot_adapter = _metadata_copilot_adapter_fixture


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_messages_visible_thinking_text_block(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                    {"type": "text", "text": "Claude reply"},
                ],
                "usage": {"input_tokens": 5, "output_tokens": 6},
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Claude reply",
        "reasoning": "Need to inspect first.",
        "reasoning_meta": {
            "content_blocks": [
                {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"}
            ]
        },
        "tool_calls": None,
        "usage": {"input_tokens": 5, "output_tokens": 6},
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_requests_visible_thinking_controls(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                    {"type": "text", "text": "Claude reply"},
                ]
            },
        )
    )

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_omits_budget_and_output_config(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        thinking_budget=2048,
        thinking={"type": "enabled", "budget_tokens": 2048},
        output_config={"effort": "high"},
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_visible_thinking_without_reasoning_effort_support(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                    {"type": "text", "text": "Claude reply"},
                ]
            },
        )
    )

    adapter = GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=lambda model: (
            _copilot_model_with_metadata(
                model,
                {
                    "github_copilot": {
                        "vendor": "Anthropic",
                        "family": "claude-haiku-4.5",
                        "version": "claude-haiku-4.5",
                        "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/v1/messages"],
                        "reasoning_efforts": [],
                        "adaptive_thinking": True,
                        "streaming": True,
                        "tool_calls": True,
                    }
                },
            )
            if model == "claude-haiku-4.5"
            else _copilot_metadata_lookup(model)
        ),
    )

    response = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Claude reply",
        "reasoning": "Need to inspect first.",
        "reasoning_meta": {
            "content_blocks": [
                {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"}
            ]
        },
        "tool_calls": None,
    }


def test_normalize_response_extracts_gemini_visible_thinking_from_reasoning_details(
    copilot_adapter: GitHubCopilotAdapter,
) -> None:
    response = {
        "id": "chatcmpl-gemini-1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Gemini reply",
                    "reasoning_details": [{"type": "reasoning.text", "text": "Need docs lookup."}],
                },
                "finish_reason": "stop",
            }
        ],
    }

    assert copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Gemini reply",
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {
            "reasoning_details": [{"type": "reasoning.text", "text": "Need docs lookup."}]
        },
        "tool_calls": None,
    }


@respx.mock
@pytest.mark.asyncio
async def test_stream_gemini_3_1_preview_extracts_visible_thinking_from_reasoning_details(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text",'
        '"text":"Need docs lookup."}]}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(COPILOT_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES,
        model_id="gemini-3.1-pro-preview",
        thinking_effort="high",
    ):
        chunks.append(chunk)

    assert chunks == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "reasoning_details": [{"type": "reasoning.text", "text": "Need docs lookup."}]
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_yields_normalized_deltas(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Hi"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-sonnet-4.6"
    ):
        chunks.append(chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Hi"},
        {"type": "finish", "reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_stream_messages_raises_network_error_on_mid_stream_read_error(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        httpx.ReadError("connection reset"),
    )

    with (
        patch.object(
            metadata_copilot_adapter,
            "_connect_stream",
            new=AsyncMock(return_value=broken_response),
        ),
        pytest.raises(NetworkError, match="Stream read failed: connection reset"),
    ):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_raises_network_error_on_eof_without_stop_reason(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Partial"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    with pytest.raises(NetworkError, match="message stop reason"):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_raises_provider_error_on_malformed_json(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: not-json\n\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    with pytest.raises(ProviderError, match="malformed JSON"):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass


@pytest.mark.asyncio
async def test_stream_messages_raises_provider_timeout_error_on_mid_stream_timeout(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        httpx.TimeoutException("timed out"),
    )

    with (
        patch.object(
            metadata_copilot_adapter,
            "_connect_stream",
            new=AsyncMock(return_value=broken_response),
        ),
        pytest.raises(ProviderTimeoutError, match="timed out"),
    ):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_surfaces_visible_thinking_text_block_variant(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"thinking","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Need docs lookup."}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"signature_delta","signature":"sig-stream"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-haiku-4.5"
    ):
        chunks.append(chunk)

    assert chunks == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "content_blocks": [
                    {"type": "thinking", "text": "Need docs lookup.", "signature": "sig-stream"}
                ]
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_normalizes_tool_use_finish_reason(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_1","name":"search"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-sonnet-4.6"
    ):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "toolu_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_falls_back_to_tool_calls_finish_when_tool_block_is_present(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_1","name":"search"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"copilot_tool_stop"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-sonnet-4.6"
    ):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "toolu_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "finish", "reason": "tool_calls"},
    ]
