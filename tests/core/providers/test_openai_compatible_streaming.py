"""OpenAI-compatible stream(): request, SSE framing, deltas, Tool Call slots and failures."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.utils.tokens import REASONING_TEXT_DELTA_MERGE_MAX_CHARS

from .openai_compatible_test_support import (
    MODEL_ID,
    OPENAI_URL,
    OPENROUTER_CONFIG,
    OPENROUTER_URL,
    SAMPLE_MESSAGES,
    collect,
    make_adapter,
    sent_payload,
    sse,
    sse_response,
    stream_chunks,
)


def _delta_chunk(**delta: Any) -> dict[str, Any]:
    return {"choices": [{"delta": delta}]}


def _finish_chunk(reason: str, **choice_fields: Any) -> dict[str, Any]:
    return {"choices": [{"delta": {}, "finish_reason": reason, **choice_fields}]}


def _tool_chunks(*calls: dict[str, Any]) -> list[dict[str, Any]]:
    """One chunk per raw wire Tool Call fragment."""

    return [_delta_chunk(tool_calls=[call]) for call in calls]


async def _accumulate(body: str) -> Any:
    accumulator = StreamingAccumulator()
    for delta in await stream_chunks(body):
        accumulator.add_delta(delta)
    return accumulator.finalize_assistant_fields()


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller_options", "expected_options"),
    [
        pytest.param(None, {"include_usage": True}, id="requests-usage"),
        pytest.param(
            {"foo": "bar", "include_usage": False},
            {"foo": "bar", "include_usage": True},
            id="merges-caller-options",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stream_request_asks_for_usage_and_leaves_reads_to_chat_clocks(
    caller_options, expected_options
) -> None:
    kwargs = {} if caller_options is None else {"stream_options": caller_options}
    with respx.mock:
        route = respx.post(OPENROUTER_URL).mock(
            return_value=sse_response(sse(_delta_chunk(content="Hi")))
        )
        await collect(make_adapter(OPENROUTER_CONFIG), **kwargs)

    request = route.calls.last.request
    payload = json.loads(request.content)
    assert payload["stream"] is True
    assert payload["stream_options"] == expected_options
    assert payload["max_tokens"] == 4096
    assert request.headers["http-referer"] == "https://vbot.app"
    assert request.headers["x-title"] == "vBot"
    assert request.extensions["timeout"]["read"] is None


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_content_heartbeats_and_finish_and_ignores_other_fields() -> None:
    body = sse(
        _delta_chunk(content="Hello"),
        ": ping - 2026-07-27T10:00:00Z\n\n",
        "event: keepalive\nid: 7\n\n",
        _delta_chunk(content=" world"),
        _finish_chunk("stop"),
    )

    assert await stream_chunks(body) == [
        {"type": "content_delta", "text": "Hello"},
        {"type": "heartbeat"},
        {"type": "content_delta", "text": " world"},
        {"type": "finish", "reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_stream_joins_multiline_data_frames() -> None:
    body = sse('data: {"id":"chatcmpl-1",\ndata: "choices":[{"delta":{"content":"Hello"}}]}\n\n')

    assert await stream_chunks(body) == [{"type": "content_delta", "text": "Hello"}]


@pytest.mark.parametrize(
    ("frame", "decode_error_chained"),
    [
        pytest.param('data: {"id":\n\n', True, id="malformed-json"),
        pytest.param("data: [1, 2]\n\n", False, id="non-object-json"),
    ],
)
@pytest.mark.asyncio
async def test_stream_json_that_is_not_an_object_is_fatal(frame, decode_error_chained) -> None:
    with pytest.raises(ProviderError) as exc_info:
        await stream_chunks(sse(frame))

    assert exc_info.value.retryable is False
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError) is decode_error_chained


@pytest.mark.asyncio
async def test_stream_ending_without_done_marker_raises_network_error() -> None:
    body = sse(_delta_chunk(content="Hello"), _finish_chunk("stop"), done=False)

    with pytest.raises(NetworkError):
        await stream_chunks(body)


# ---------------------------------------------------------------------------
# In-band errors and terminal outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "error_type", "retryable", "provider_detail"),
    [
        pytest.param(
            {
                "code": 429,
                "message": "Rate limit exceeded",
                "metadata": {"error_type": "rate_limit_exceeded"},
            },
            ProviderRateLimitError,
            True,
            "Rate limit exceeded",
            id="classified-object",
        ),
        pytest.param(
            {"message": "quota exceeded"}, ProviderError, False, "quota exceeded", id="unclassified"
        ),
        pytest.param(
            "upstream exploded", ProviderError, False, "upstream exploded", id="non-object"
        ),
    ],
)
@pytest.mark.asyncio
async def test_stream_in_band_error_is_classified_with_the_provider_detail(
    error, error_type, retryable, provider_detail
) -> None:
    body = sse(_delta_chunk(content="Hello"), {"error": error})

    with pytest.raises(ProviderError) as exc_info:
        await stream_chunks(body)

    assert exc_info.type is error_type
    assert exc_info.value.retryable is retryable
    assert provider_detail in str(exc_info.value)


_TOOL_FRAGMENT = _delta_chunk(tool_calls=[{"index": 0, "function": {"name": "search"}}])


@pytest.mark.parametrize(
    ("before_finish", "finish_reason", "expected"),
    [
        pytest.param([], "length", "output_truncated", id="length"),
        pytest.param([_TOOL_FRAGMENT], "stop", "tool_calls", id="stop-after-tool-fragment"),
        pytest.param([_TOOL_FRAGMENT], "provider_tool_stop", "unknown", id="unknown-stays-unsafe"),
    ],
)
@pytest.mark.asyncio
async def test_stream_finish_maps_the_terminal_outcome(
    before_finish, finish_reason, expected
) -> None:
    chunks = await stream_chunks(sse(*before_finish, _finish_chunk(finish_reason)))

    assert chunks[-1] == {"type": "finish", "reason": expected}


@pytest.mark.parametrize(
    ("native_reason", "error_type"),
    [("network_error", NetworkError), ("server_error", ProviderError)],
)
@pytest.mark.asyncio
async def test_stop_concealing_a_native_transport_failure_is_retryable(
    native_reason, error_type
) -> None:
    body = sse(
        {
            "choices": [
                {
                    "delta": {"content": ""},
                    "finish_reason": "stop",
                    "native_finish_reason": native_reason,
                }
            ]
        }
    )

    with pytest.raises(error_type) as exc_info:
        await stream_chunks(body)

    assert exc_info.type is error_type
    assert exc_info.value.retryable is True


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_reasoning_text_and_opaque_metadata() -> None:
    reasoning_details = [{"type": "reasoning.text", "text": "opaque"}]
    body = sse(
        _delta_chunk(
            reasoning_content="Think",
            encrypted_content="secret",
            reasoning_details=reasoning_details,
            unknown_provider_field="ignored",
        )
    )

    assert await stream_chunks(body) == [
        {"type": "reasoning_delta", "text": "Think"},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "encrypted_content": "secret",
                "reasoning_details": reasoning_details,
            },
        },
    ]


_TEXT_BLOCK = {"type": "reasoning.text", "format": "unknown", "index": 0}
_OVERSIZED_TEXT = "x" * (REASONING_TEXT_DELTA_MERGE_MAX_CHARS + 1)


@pytest.mark.parametrize(
    ("streamed_details", "expected"),
    [
        pytest.param(
            [
                {**_TEXT_BLOCK, "text": "Think"},
                {**_TEXT_BLOCK, "text": "ing"},
                {**_TEXT_BLOCK, "text": "."},
            ],
            [{**_TEXT_BLOCK, "text": "Thinking."}],
            id="text-fragments-merge",
        ),
        pytest.param(
            [
                {**_TEXT_BLOCK, "text": "first"},
                {**_TEXT_BLOCK, "text": _OVERSIZED_TEXT},
                {**_TEXT_BLOCK, "index": 1, "text": "second"},
            ],
            [
                {**_TEXT_BLOCK, "text": "first"},
                {**_TEXT_BLOCK, "text": _OVERSIZED_TEXT},
                {**_TEXT_BLOCK, "index": 1, "text": "second"},
            ],
            id="oversized-or-reindexed-stay-apart",
        ),
        pytest.param(
            [
                {"type": "reasoning.encrypted", "id": "rs_1", "data": "partial"},
                {"type": "reasoning.summary", "summary": "Plan."},
                {"type": "reasoning.encrypted", "id": "rs_1", "data": "final"},
            ],
            [
                {"type": "reasoning.encrypted", "id": "rs_1", "data": "final"},
                {"type": "reasoning.summary", "summary": "Plan."},
            ],
            id="stable-id-updates-in-place",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stream_accumulates_reasoning_details_across_deltas(
    streamed_details, expected
) -> None:
    body = sse(*(_delta_chunk(reasoning_details=[detail]) for detail in streamed_details))

    fields = await _accumulate(body)

    assert fields.reasoning_meta == {"reasoning_details": expected}


@pytest.mark.asyncio
async def test_sibling_tool_reasoning_details_survive_accumulation_and_replay() -> None:
    first = {"type": "reasoning.encrypted", "id": "call_first", "data": "opaque-first"}
    second = {"type": "reasoning.encrypted", "id": "call_second", "data": "opaque-second"}
    body = sse(
        _delta_chunk(reasoning_details=[first]),
        _delta_chunk(
            tool_calls=[
                {"index": 0, "id": "call_first", "function": {"name": "first", "arguments": "{}"}},
                {
                    "index": 1,
                    "id": "call_second",
                    "function": {"name": "second", "arguments": "{}"},
                },
            ]
        ),
        _delta_chunk(reasoning_details=[second]),
        _finish_chunk("tool_calls"),
    )

    fields = await _accumulate(body)
    replayed = await sent_payload(
        make_adapter(),
        [
            {
                "role": "assistant",
                "content": fields.content,
                "reasoning": fields.reasoning,
                "reasoning_meta": fields.reasoning_meta,
                "tool_calls": fields.tool_calls,
            }
        ],
    )

    assert fields.reasoning_meta == {"reasoning_details": [first, second]}
    assert [call["id"] for call in fields.tool_calls or []] == ["call_first", "call_second"]
    assert replayed["messages"][0]["reasoning_details"] == [first, second]


# ---------------------------------------------------------------------------
# Tool Call deltas and slots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_deltas_keep_stable_slots_and_attach_ids_when_known() -> None:
    """The wire index is the slot; an id is sent when first known, even after fragments."""
    body = sse(
        *_tool_chunks(
            {
                "index": 0,
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city"'},
            },
            {"index": 1, "id": "call_provider", "function": {"name": "read_file"}},
            {"index": 0, "id": "call_late", "function": {"arguments": ':"Berlin"}'}},
            {"index": 1, "function": {"arguments": '{"path":"README.md"}'}},
        ),
        _finish_chunk("tool_calls"),
    )

    chunks = await stream_chunks(body)
    accumulator = StreamingAccumulator()
    for delta in chunks:
        accumulator.add_delta(delta)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "slot": 0,
            "name_delta": "get_weather",
            "arguments_delta": '{"city"',
        },
        {
            "type": "tool_call_delta",
            "slot": 1,
            "id": "call_provider",
            "name_delta": "read_file",
            "arguments_delta": "",
        },
        {
            "type": "tool_call_delta",
            "slot": 0,
            "id": "call_late",
            "name_delta": "",
            "arguments_delta": ':"Berlin"}',
        },
        {
            "type": "tool_call_delta",
            "slot": 1,
            "name_delta": "",
            "arguments_delta": '{"path":"README.md"}',
        },
        {"type": "finish", "reason": "tool_calls"},
    ]
    assert accumulator.finalize_assistant_fields().tool_calls == [
        {"id": "call_late", "name": "get_weather", "arguments": {"city": "Berlin"}},
        {"id": "call_provider", "name": "read_file", "arguments": {"path": "README.md"}},
    ]


@pytest.mark.parametrize("name", [None, ""])
@pytest.mark.asyncio
async def test_empty_idless_tool_attempt_is_kept_for_rejection(name) -> None:
    body = sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": name, "arguments": ""}},
                            {
                                "index": 1,
                                "id": "call_valid",
                                "function": {"name": "read", "arguments": "{}"},
                            },
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )

    rejected, valid = (await _accumulate(body)).tool_calls

    assert rejected["id"] == "tool_call_0"
    assert rejected["rejection"]["code"] == "malformed_tool_call"
    assert valid == {"id": "call_valid", "name": "read", "arguments": {}}


# Ollama-compatible servers may reuse one wire index for a whole parallel batch
# and distinguish the calls only by id.
@pytest.mark.parametrize(
    ("fragments", "expected_calls"),
    [
        pytest.param(
            [
                {
                    "index": 0,
                    "id": "call_a",
                    "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'},
                },
                {
                    "index": 0,
                    "id": "call_b",
                    "function": {"name": "get_time", "arguments": '{"tz"'},
                },
                {"index": 0, "function": {"arguments": ':"UTC"}'}},
            ],
            [
                {"id": "call_a", "name": "get_weather", "arguments": {"city": "Berlin"}},
                {"id": "call_b", "name": "get_time", "arguments": {"tz": "UTC"}},
            ],
            id="new-id-opens-a-slot-and-keeps-idless-continuations",
        ),
        pytest.param(
            [
                {"index": 0, "id": "call_a", "function": {"name": "first", "arguments": '{"a":'}},
                {"index": 0, "id": "call_b", "function": {"name": "second", "arguments": '{"b":'}},
                {"index": 0, "id": "call_a", "function": {"arguments": "1}"}},
                {"index": 0, "id": "call_b", "function": {"arguments": "2}"}},
            ],
            [
                {"id": "call_a", "name": "first", "arguments": {"a": 1}},
                {"id": "call_b", "name": "second", "arguments": {"b": 2}},
            ],
            id="returning-id-resumes-its-slot",
        ),
        pytest.param(
            [
                {"index": 0, "id": "call_a", "function": {"name": "first", "arguments": "{}"}},
                {"index": 0, "id": "call_b", "function": {"name": "second", "arguments": "{}"}},
                {
                    "index": 1,
                    "id": "call_c",
                    "function": {"name": "third", "arguments": '{"value":'},
                },
                {"index": 1, "id": "call_c", "function": {"arguments": "3}"}},
            ],
            [
                {"id": "call_a", "name": "first", "arguments": {}},
                {"id": "call_b", "name": "second", "arguments": {}},
                {"id": "call_c", "name": "third", "arguments": {"value": 3}},
            ],
            id="later-native-index-avoids-virtual-slot",
        ),
        pytest.param(
            [
                {"index": 0, "id": "call_a", "function": {"name": "first", "arguments": "{}"}},
                {"index": 0, "id": "call_b", "function": {"name": "second", "arguments": "{}"}},
                {"index": 1, "function": {"name": "third", "arguments": '{"value":'}},
                {"index": 1, "id": "call_c", "function": {"arguments": "3}"}},
            ],
            [
                {"id": "call_a", "name": "first", "arguments": {}},
                {"id": "call_b", "name": "second", "arguments": {}},
                {"id": "call_c", "name": "third", "arguments": {"value": 3}},
            ],
            id="later-native-index-with-late-id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reused_wire_index_is_redirected_by_tool_call_id(fragments, expected_calls) -> None:
    fields = await _accumulate(sse(*_tool_chunks(*fragments), _finish_chunk("tool_calls")))

    assert fields.tool_calls == expected_calls


@pytest.mark.asyncio
async def test_repeated_name_is_deduplicated_but_argument_bytes_are_kept() -> None:
    # Some compatible servers resend the full name on every fragment; argument
    # fragments are true deltas even when they repeat a prefix.
    fragments = ['{"value":', '{"value":', "1}}"]
    body = sse(
        *_tool_chunks(
            *(
                {"index": 0, "id": "call_a", "function": {"name": "write", "arguments": fragment}}
                for fragment in fragments
            )
        ),
        _finish_chunk("tool_calls"),
    )

    chunks = await stream_chunks(body)
    accumulator = StreamingAccumulator()
    for delta in chunks:
        accumulator.add_delta(delta)

    tool_deltas = [delta for delta in chunks if delta["type"] == "tool_call_delta"]
    assert [delta["name_delta"] for delta in tool_deltas] == ["write", "", ""]
    assert [delta["arguments_delta"] for delta in tool_deltas] == fragments
    assert accumulator.finalize_assistant_fields().tool_calls == [
        {"id": "call_a", "name": "write", "arguments": {"value": {"value": 1}}}
    ]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("final_chunk", "expected_tail"),
    [
        pytest.param(
            {
                **_finish_chunk("stop"),
                "usage": {
                    "prompt_tokens": 42,
                    "completion_tokens": 13,
                    "prompt_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 5},
                    "completion_tokens_details": {"reasoning_tokens": 8},
                },
            },
            [
                {"type": "finish", "reason": "stop"},
                {
                    "type": "usage",
                    "input_tokens": 42,
                    "output_tokens": 13,
                    "cache_read_tokens": 30,
                    "cache_write_tokens": 5,
                    "reasoning_tokens": 8,
                },
            ],
            id="usage-follows-finish",
        ),
        pytest.param(
            {"choices": [], "usage": {"prompt_tokens": True, "completion_tokens": 5}},
            [{"type": "usage", "output_tokens": 5}],
            id="usable-counter-only",
        ),
        pytest.param(
            {**_finish_chunk("stop"), "usage": "not-a-dict"},
            [{"type": "finish", "reason": "stop"}],
            id="unusable-usage-omitted",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stream_emits_usage_from_the_usage_chunk(final_chunk, expected_tail) -> None:
    chunks = await stream_chunks(sse(_delta_chunk(content="Hi"), final_chunk))

    assert chunks == [{"type": "content_delta", "text": "Hi"}, *expected_tail]


# ---------------------------------------------------------------------------
# Connection and mid-stream failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "error_type", "attempts"),
    [
        pytest.param(httpx.Response(401, text="Unauthorized"), ProviderAuthError, 1, id="401"),
        pytest.param(httpx.TimeoutException("timed out"), ProviderTimeoutError, 4, id="timeout"),
        pytest.param(httpx.ConnectError("connection failed"), NetworkError, 4, id="connect"),
    ],
)
@pytest.mark.asyncio
async def test_stream_connect_failures_are_classified_after_retries(
    reply, error_type, attempts
) -> None:
    mock = {"return_value": reply} if isinstance(reply, httpx.Response) else {"side_effect": reply}
    with respx.mock:
        route = respx.post(OPENAI_URL).mock(**mock)
        with pytest.raises(error_type):
            await collect(make_adapter())

    assert route.call_count == attempts


class _BrokenStream(httpx.AsyncByteStream):
    """Delivers one content frame, then fails like a dropped connection."""

    def __init__(self, failure: Exception) -> None:
        self._failure = failure
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse(_delta_chunk(content="A"), done=False).encode()
        raise self._failure

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        pytest.param(httpx.ReadError("connection reset"), NetworkError, id="read-error"),
        pytest.param(httpx.ReadTimeout("stream timed out"), ProviderTimeoutError, id="timeout"),
    ],
)
@pytest.mark.asyncio
async def test_mid_stream_transport_failure_is_wrapped_and_closes_the_response(
    failure, error_type
) -> None:
    body = _BrokenStream(failure)
    received: list[dict[str, Any]] = []
    with respx.mock:
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, stream=body))
        with pytest.raises(error_type):
            async for delta in make_adapter().stream(SAMPLE_MESSAGES, model_id=MODEL_ID):
                received.append(delta)

    assert received == [{"type": "content_delta", "text": "A"}]
    assert body.closed
