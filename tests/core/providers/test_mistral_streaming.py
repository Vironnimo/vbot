"""Mistral: streaming behavior."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.providers.mistral import MistralAdapter
from tests.core.providers.mistral_helpers import (
    MISTRAL_URL,
    SAMPLE_MESSAGES,
)
from tests.core.providers.mistral_helpers import (
    mistral_adapter as mistral_adapter,
)
from tests.core.providers.mistral_helpers import (
    mistral_config as mistral_config,
)


@respx.mock
@pytest.mark.asyncio
async def test_stream_requests_usage_and_yields_content_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    sse_body = 'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    route = respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for chunk in mistral_adapter.stream(SAMPLE_MESSAGES, model_id="mistral-large-latest"):
        chunks.append(chunk)

    request_body = json.loads(route.calls.last.request.content)
    assert chunks == [{"type": "content_delta", "text": "Hi"}]
    assert request_body["stream"] is True
    assert request_body["stream_options"] == {"include_usage": True}


@respx.mock
@pytest.mark.asyncio
async def test_stream_yields_reasoning_delta_for_delta_thinking(
    mistral_adapter: MistralAdapter,
) -> None:
    sse_body = 'data: {"choices":[{"delta":{"thinking":"Reasoning delta"}}]}\n\ndata: [DONE]\n\n'
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for chunk in mistral_adapter.stream(SAMPLE_MESSAGES, model_id="mistral-large-latest"):
        chunks.append(chunk)

    assert chunks == [{"type": "reasoning_delta", "text": "Reasoning delta"}]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_list_delta_thinking_yields_reasoning_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": [
                        {"type": "thinking", "thinking": "Think1"},
                    ]
                }
            }
        ]
    }
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [{"type": "reasoning_delta", "text": "Think1"}]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_content_preserves_following_tool_call_deltas(
    mistral_adapter: MistralAdapter,
) -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "content": [
                            {"type": "thinking", "thinking": "Need a tool."},
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_123",
                                "function": {
                                    "name": "status",
                                    "arguments": "{}",
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    sse_body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    sse_body += "data: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    normalized = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        normalized.append(stream_chunk)

    assert normalized == [
        {"type": "reasoning_delta", "text": "Need a tool."},
        {
            "type": "tool_call_delta",
            "slot": 0,
            "name_delta": "status",
            "arguments_delta": "{}",
            "id": "call_123",
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "content_chunks": [
                    {"type": "thinking", "thinking": "Need a tool."},
                ]
            },
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_list_delta_text_yields_content_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": [
                        {"type": "text", "text": "Text1"},
                    ]
                }
            }
        ]
    }
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [{"type": "content_delta", "text": "Text1"}]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_list_delta_yields_finish_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": [
                        {"type": "text", "text": "Text1"},
                    ]
                },
                "finish_reason": "stop",
            }
        ]
    }
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Text1"},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "content_chunks": [{"type": "text", "text": "Text1"}],
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_list_delta_yields_usage_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": [
                        {"type": "text", "text": "Text1"},
                    ]
                }
            }
        ],
        "usage": {"prompt_tokens": 21, "completion_tokens": 8},
    }
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Text1"},
        {"type": "usage", "input_tokens": 21, "output_tokens": 8},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_list_delta_with_finish_and_usage_preserves_order(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": [
                        {"type": "thinking", "thinking": "Think1"},
                        {"type": "text", "text": "Text1"},
                    ]
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 34, "completion_tokens": 13},
    }
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [
        {"type": "reasoning_delta", "text": "Think1"},
        {"type": "content_delta", "text": "Text1"},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "content_chunks": [
                    {"type": "thinking", "thinking": "Think1"},
                    {"type": "text", "text": "Text1"},
                ],
            },
        },
        {"type": "finish", "reason": "stop"},
        {"type": "usage", "input_tokens": 34, "output_tokens": 13},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_string_content_delta_delegates_to_base(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {"choices": [{"delta": {"content": "Hi"}}]}
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [{"type": "content_delta", "text": "Hi"}]
