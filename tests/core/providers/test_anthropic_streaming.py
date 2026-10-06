"""Anthropic Adapter streaming: SSE decoding, usage deltas, stream failures and retry."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, override

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator
from core.providers.anthropic import AnthropicAdapter
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

from .anthropic_test_support import (
    ANTHROPIC_URL,
    CUSTOM_CONFIG,
    CUSTOM_URL,
    MODEL_ID,
    SAMPLE_MESSAGES,
    make_adapter,
    sse,
    sse_response,
)

TEXT_EVENTS: tuple[dict[str, Any], ...] = (
    {"type": "message_start", "message": {"id": "msg_01", "usage": {"input_tokens": 25}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 10, "output_tokens_details": {"thinking_tokens": 6}},
    },
)


async def _collect(adapter: AnthropicAdapter, **kwargs: Any) -> list[dict[str, Any]]:
    return [chunk async for chunk in adapter.stream(SAMPLE_MESSAGES, model_id=MODEL_ID, **kwargs)]


async def stream_chunks(body: str) -> list[dict[str, Any]]:
    """Stream one mocked SSE body through the native Adapter."""

    with respx.mock:
        respx.post(ANTHROPIC_URL).mock(return_value=sse_response(body))
        return await _collect(make_adapter())


# ---------------------------------------------------------------------------
# Request and SSE framing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_request_carries_stream_flag_defaults_and_native_headers() -> None:
    with respx.mock:
        route = respx.post(CUSTOM_URL).mock(return_value=sse_response(sse()))
        await _collect(make_adapter(CUSTOM_CONFIG), temperature=None)

    request = route.calls.last.request
    payload = json.loads(request.content)
    assert payload["stream"] is True
    assert payload["temperature"] == 0.7
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["x-custom-header"] == "custom-value"


@pytest.mark.asyncio
async def test_stream_yields_text_finish_then_usage_and_skips_bookkeeping_frames() -> None:
    body = sse({"type": "ping"}, ": keep-alive comment\n\n", *TEXT_EVENTS)

    assert await stream_chunks(body) == [
        {"type": "usage", "input_tokens": 25},
        {"type": "content_delta", "text": "Hello"},
        {"type": "content_delta", "text": " world"},
        {"type": "finish", "reason": "stop"},
        {"type": "usage", "input_tokens": 25, "output_tokens": 10, "reasoning_tokens": 6},
    ]


@pytest.mark.asyncio
async def test_stream_joins_multiline_sse_data_frames() -> None:
    body = sse(
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,\n'
        'data: "delta":{"type":"text_delta","text":"Hello"}}\n\n'
    )

    assert await stream_chunks(body) == [{"type": "content_delta", "text": "Hello"}]


@pytest.mark.asyncio
async def test_malformed_sse_json_is_a_fatal_provider_error() -> None:
    with pytest.raises(ProviderError) as exc_info:
        await stream_chunks(sse('event: content_block_delta\ndata: {"type":\n\n', stop=False))

    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_stream_ending_without_message_stop_is_a_network_error() -> None:
    with pytest.raises(NetworkError):
        await stream_chunks(sse(*TEXT_EVENTS, stop=False))


# ---------------------------------------------------------------------------
# Reasoning, Tool calls and terminal outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_streams_visibly_and_accumulates_opaque_reasoning_blocks() -> None:
    thinking = {"type": "thinking", "thinking": "Need weather.", "signature": "opaque-signature"}
    redacted = {"type": "redacted_thinking", "data": "opaque-redacted"}
    body = sse(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Need"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": " weather."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "opaque-signature"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": redacted},
        {"type": "content_block_stop", "index": 1},
    )

    assert await stream_chunks(body) == [
        {"type": "reasoning_delta", "text": "Need"},
        {"type": "reasoning_delta", "text": " weather."},
        {"type": "reasoning_meta", "reasoning_meta": {"content_blocks": [thinking]}},
        {"type": "reasoning_meta", "reasoning_meta": {"content_blocks": [thinking, redacted]}},
    ]


@pytest.mark.asyncio
async def test_tool_use_streams_name_then_argument_fragments_and_finishes_as_tool_calls() -> None:
    body = sse(
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_abc",
                "name": "get_weather",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"city"'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": ':"Berlin"}'},
        },
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    )

    assert await stream_chunks(body) == [
        {
            "type": "tool_call_delta",
            "id": "toolu_abc",
            "name_delta": "get_weather",
            "arguments_delta": "",
        },
        {
            "type": "tool_call_delta",
            "id": "toolu_abc",
            "name_delta": "",
            "arguments_delta": '{"city"',
        },
        {
            "type": "tool_call_delta",
            "id": "toolu_abc",
            "name_delta": "",
            "arguments_delta": ':"Berlin"}',
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@pytest.mark.parametrize("name", [None, ""])
@pytest.mark.asyncio
async def test_tool_call_with_invalid_name_survives_for_canonical_rejection(name) -> None:
    def tool_use(index: int, call_id: str, tool_name: object) -> tuple[dict, dict]:
        block = {"type": "tool_use", "id": call_id, "name": tool_name, "input": {}}
        return (
            {"type": "content_block_start", "index": index, "content_block": block},
            {"type": "content_block_stop", "index": index},
        )

    body = sse(
        *tool_use(0, "invalid_call", name),
        *tool_use(1, "valid_call", "status"),
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    )
    accumulator = StreamingAccumulator()
    for delta in await stream_chunks(body):
        accumulator.add_delta(delta)

    result = accumulator.finalize_assistant_fields()
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None
    assert [call["id"] for call in result.tool_calls] == ["invalid_call", "valid_call"]
    assert result.tool_calls[0]["rejection"]["code"] == "malformed_tool_call"
    assert "rejection" not in result.tool_calls[1]


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("max_tokens", "output_truncated"), ({}, "unknown")],
)
@pytest.mark.asyncio
async def test_stream_stop_reason_maps_to_terminal_outcome(stop_reason, expected) -> None:
    body = sse({"type": "message_delta", "delta": {"stop_reason": stop_reason}})

    assert await stream_chunks(body) == [{"type": "finish", "reason": expected}]


# ---------------------------------------------------------------------------
# Usage deltas
# ---------------------------------------------------------------------------

_NO_USAGE = object()


_START_WITH_CACHE = {
    "input_tokens": 25,
    "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 200,
}
_INPUT_WITH_CACHE = {"input_tokens": 1225, "cache_read_tokens": 1000, "cache_write_tokens": 200}


@pytest.mark.parametrize(
    ("start_usage", "terminal_usage", "expected"),
    [
        pytest.param(
            _START_WITH_CACHE,
            {"output_tokens": 10},
            [_INPUT_WITH_CACHE, {**_INPUT_WITH_CACHE, "output_tokens": 10}],
            id="cache-folded-into-input",
        ),
        pytest.param(
            # Counters a later event omits or nulls keep their earlier value.
            _START_WITH_CACHE,
            {"input_tokens": 25, "cache_read_input_tokens": None, "output_tokens": 10},
            [_INPUT_WITH_CACHE, {**_INPUT_WITH_CACHE, "output_tokens": 10}],
            id="terminal-keeps-start-cache",
        ),
        pytest.param(
            {"input_tokens": 0, "output_tokens": 0},
            {
                "input_tokens": 648,
                "output_tokens": 1180,
                "cache_read_input_tokens": 125568,
                "output_tokens_details": {"thinking_tokens": 44},
            },
            [
                {
                    "input_tokens": 126216,
                    "output_tokens": 1180,
                    "cache_read_tokens": 125568,
                    "reasoning_tokens": 44,
                }
            ],
            id="terminal-snapshot-replaces-start",
        ),
        pytest.param(
            _NO_USAGE, {"output_tokens": 10}, [{"output_tokens": 10}], id="output-without-input"
        ),
        pytest.param(
            # A stream cut before its terminal usage keeps the input.
            {"input_tokens": 25},
            _NO_USAGE,
            [{"input_tokens": 25}],
            id="no-terminal-usage",
        ),
        pytest.param(
            {"input_tokens": 2589},
            {"output_tokens": 0},
            [{"input_tokens": 2589}, {"input_tokens": 2589, "output_tokens": 0}],
            id="zero-output",
        ),
        pytest.param(
            {"input_tokens": 17},
            {"input_tokens": 23},
            [{"input_tokens": 17}, {"input_tokens": 23}],
            id="terminal-input-only",
        ),
        pytest.param(
            {
                "input_tokens": 17,
                "cache_read_input_tokens": True,
                "cache_creation_input_tokens": -20,
            },
            {"output_tokens": 8},
            [{"input_tokens": 17}, {"input_tokens": 17, "output_tokens": 8}],
            id="unusable-cache-counters",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stream_usage_reports_cumulative_measured_counters(
    start_usage, terminal_usage, expected
) -> None:
    message: dict[str, Any] = {"id": "msg_01"}
    if start_usage is not _NO_USAGE:
        message["usage"] = start_usage
    terminal: dict[str, Any] = {"type": "message_delta"}
    if terminal_usage is not _NO_USAGE:
        terminal["usage"] = terminal_usage

    chunks = await stream_chunks(sse({"type": "message_start", "message": message}, terminal))

    assert chunks == [{"type": "usage", **usage} for usage in expected]


# ---------------------------------------------------------------------------
# Failures and retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "expected_type", "retryable"),
    [
        ("overloaded_error", ProviderError, True),
        ("rate_limit_error", ProviderRateLimitError, True),
        ("timeout_error", ProviderTimeoutError, True),
        ("invalid_request_error", ProviderError, False),
        ("authentication_error", ProviderAuthError, False),
        ("future_unknown_error", ProviderError, False),
    ],
)
@pytest.mark.asyncio
async def test_in_band_error_event_is_classified_by_its_documented_type(
    error_type, expected_type, retryable
) -> None:
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "error", "error": {"type": error_type, "message": "stream failed"}},
        stop=False,
    )

    with pytest.raises(ProviderError, match="stream failed") as exc_info:
        await stream_chunks(body)

    assert type(exc_info.value) is expected_type
    assert exc_info.value.retryable is retryable


@pytest.mark.parametrize(
    ("attempt", "expected_type", "calls"),
    [
        pytest.param(httpx.Response(401, text="Unauthorized"), ProviderAuthError, 1, id="401"),
        pytest.param(httpx.TimeoutException("timed out"), ProviderTimeoutError, 4, id="timeout"),
        pytest.param(httpx.ConnectError("connection failed"), NetworkError, 4, id="connect-error"),
    ],
)
@pytest.mark.asyncio
async def test_stream_connect_failures_are_classified_after_retries(
    attempt, expected_type, calls
) -> None:
    with respx.mock:
        route = respx.post(ANTHROPIC_URL).mock(side_effect=[attempt] * 4)
        with pytest.raises(expected_type):
            await _collect(make_adapter())

    assert route.call_count == calls


class _BrokenStream(httpx.AsyncByteStream):
    """A response body that fails after the connection is established."""

    def __init__(self, failure: Exception) -> None:
        self._failure = failure
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'event: ping\ndata: {"type":"ping"}\n\n'
        raise self._failure

    @override
    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (httpx.ReadError("socket closed"), NetworkError),
        (httpx.TimeoutException("timed out"), ProviderTimeoutError),
    ],
)
@pytest.mark.asyncio
async def test_mid_stream_failure_is_wrapped_and_closes_the_response(
    failure, expected_type
) -> None:
    body = _BrokenStream(failure)

    with respx.mock:
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, stream=body))
        with pytest.raises(expected_type):
            await _collect(make_adapter())

    assert body.closed is True


class _RotatingTokenGetter:
    """Token getter that yields a fresh token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token


@pytest.mark.asyncio
async def test_stream_connect_retry_rebuilds_auth_headers() -> None:
    adapter = make_adapter(token_getter=_RotatingTokenGetter(["stale-token", "fresh-token"]))

    with respx.mock:
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[httpx.Response(503, text="Service Unavailable"), sse_response(sse())]
        )
        assert await _collect(adapter) == []

    assert [call.request.headers["x-api-key"] for call in route.calls] == [
        "stale-token",
        "fresh-token",
    ]
    assert route.calls[1].request.headers["anthropic-version"] == "2023-06-01"
