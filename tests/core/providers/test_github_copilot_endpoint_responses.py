"""Tests for GitHubCopilotAdapter behavior."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.chat.chat import _assistant_message_from_response
from core.chat.streaming import StreamingAccumulator
from core.providers.errors import NetworkError, ProviderTimeoutError
from core.providers.github_copilot import (
    GitHubCopilotAdapter,
)
from tests.core.providers.github_copilot_test_support import (
    COPILOT_CONFIG,
    RESPONSES_URL,
    SAMPLE_MESSAGES,
    _BrokenStreamResponse,
    _copilot_metadata_lookup,
    _RotatingTokenGetter,
)
from tests.core.providers.github_copilot_test_support import (
    metadata_copilot_adapter as _metadata_copilot_adapter_fixture,
)

metadata_copilot_adapter = _metadata_copilot_adapter_fixture


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_yields_normalized_deltas(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Hi"},
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_rebuilds_headers_per_connect_attempt() -> None:
    """A retried Copilot stream connect re-consults the token getter (token refresh)."""
    token_getter = _RotatingTokenGetter(["stale-token", "fresh-token"])
    adapter = GitHubCopilotAdapter(
        COPILOT_CONFIG, token_getter, model_lookup=_copilot_metadata_lookup
    )
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    route = respx.post(RESPONSES_URL).mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"}),
        ]
    )

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass

    assert route.call_count == 2
    assert route.calls[0].request.headers.get("authorization") == "Bearer stale-token"
    assert route.calls[1].request.headers.get("authorization") == "Bearer fresh-token"


@pytest.mark.asyncio
async def test_stream_responses_raises_network_error_on_mid_stream_read_error(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        "event: response.output_text.delta",
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
        async for _ in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_raises_network_error_on_eof_without_completion(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Partial"}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    with pytest.raises(NetworkError, match="response completion event"):
        async for _ in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass


@pytest.mark.asyncio
async def test_stream_responses_raises_provider_timeout_error_on_mid_stream_timeout(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        "event: response.output_text.delta",
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
        async for _ in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_preserves_tool_call_id_across_sse_events(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","call_id":"call_stable","name":"search"}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"delta":"{\\"q\\""}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"delta":":\\"docs\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"function_call","call_id":"call_stable"}]}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    tool_call_chunks = [chunk for chunk in chunks if chunk["type"] == "tool_call_delta"]
    assert tool_call_chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "",
            "arguments_delta": '{"q"',
        },
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "",
            "arguments_delta": ':"docs"}',
        },
    ]
    assert chunks[-1] == {"type": "finish", "reason": "tool_calls"}


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_does_not_duplicate_reasoning_from_completed_event(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.reasoning_summary_text.delta\n"
        'data: {"type":"response.reasoning_summary_text.delta","delta":"Need docs lookup."}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed",'
        '"output":[{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text",'
        '"text":"Need docs lookup."}],"encrypted_content":"opaque"}]}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    assert chunks == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp-1",
                "response_output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    }
                ],
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    }
                ],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_4_responses_with_nested_tool_name_and_visible_reasoning(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-1",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    },
                ],
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        [
            {"role": "user", "content": "Look up docs"},
        ],
        model_id="gpt-5.4",
        thinking_effort="high",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                },
            }
        ],
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "gpt-5.4",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Look up docs"}]}],
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 4096,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": None,
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {
            "response_id": "resp-1",
            "response_output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"q":"docs"}',
                    },
                },
            ],
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                    "encrypted_content": "opaque",
                }
            ],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
    }


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_4_family_responses_with_nested_tool_name(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-1",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    },
                ],
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        [{"role": "user", "content": "Look up docs"}],
        model_id=model_id,
        thinking_effort="high",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                },
            }
        ],
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Look up docs"}]}],
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 4096,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": None,
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {
            "response_id": "resp-1",
            "response_output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"q":"docs"}',
                    },
                },
            ],
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                    "encrypted_content": "opaque",
                }
            ],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
    }


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_4_family_responses_with_blank_top_level_tool_name_and_arguments(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [{"role": "user", "content": "Look up docs"}],
        model_id=model_id,
        tools=[
            {
                "type": "function",
                "name": "",
                "function": {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                },
            }
        ],
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["tools"] == [
        {
            "type": "function",
            "name": "search",
            "description": "Search docs",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
    ]


@respx.mock
@pytest.mark.asyncio
async def test_send_replays_nested_tool_call_name_shape_for_gpt_5_4_responses(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search",
                "content": "result",
            },
        ],
        model_id="gpt-5.4",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_replays_nested_tool_call_arguments_when_top_level_values_are_blank(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "",
                        "arguments": "",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search",
                "content": "result",
            },
        ],
        model_id=model_id,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_replays_nested_tool_call_name_shape_for_gpt_5_4_family(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search",
                "content": "result",
            },
        ],
        model_id=model_id,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_stream_gpt_5_4_family_responses_surfaces_nested_tool_name(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","call_id":"call_stable",'
        '"function":{"name":"search"}}}\n\n'
        "event: response.reasoning_summary_text.delta\n"
        'data: {"type":"response.reasoning_summary_text.delta","delta":"Need docs lookup."}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed",'
        '"output":[{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text",'
        '"text":"Need docs lookup."}],"encrypted_content":"opaque"}],'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id=model_id):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp-1",
                "response_output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    }
                ],
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    }
                ],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": "tool_calls"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_stream_gpt_5_4_family_responses_deduplicates_replayed_arguments(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","id":"fc_1","call_id":"call_1",'
        '"name":"","arguments":"","function":{"name":"search","arguments":"{\\"q\\":\\"docs\\"}"}}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"item_id":"fc_1","call_id":"call_1","delta":"{\\"q\\":\\"docs\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id=model_id):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "search",
            "arguments_delta": '{"q":"docs"}',
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_output": [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "",
                        "arguments": "",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    }
                ]
            },
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_stream_gpt_5_4_family_item_id_only_delta_parses_into_valid_chat_tool_call(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","id":"fc_1","call_id":"call_1",'
        '"name":"tool","arguments":"","function":{"name":"bash"}}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1",'
        '"delta":"{\\"command\\":\\"pwd\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    accumulator = StreamingAccumulator()
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id=model_id):
        accumulator.add_delta(chunk)

    assistant = _assistant_message_from_response(
        f"github-copilot/{model_id}",
        accumulator.finalize_assistant_fields().to_response_dict(),
    )

    assert assistant.tool_calls is not None
    assert [tool_call.to_dict() for tool_call in assistant.tool_calls] == [
        {"id": "call_1", "name": "bash", "arguments": {"command": "pwd"}}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_backfills_only_missing_tool_argument_suffix(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","call_id":"call_stable",'
        '"function":{"name":"search","arguments":"{\\"q\\""}}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"call_id":"call_stable","delta":"{\\"q\\":\\"docs\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "search",
            "arguments_delta": '{"q"',
        },
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "",
            "arguments_delta": ':"docs"}',
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_output": [
                    {
                        "type": "function_call",
                        "call_id": "call_stable",
                        "function": {
                            "name": "search",
                            "arguments": '{"q"',
                        },
                    }
                ]
            },
        },
        {"type": "finish", "reason": "tool_calls"},
    ]
