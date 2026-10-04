"""Local Ollama native ``/api/chat`` wire: request shaping, the ``think`` control,
completed-response and NDJSON stream normalization, and transport errors."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import NetworkError, ProviderError
from core.tools import tool_success
from core.utils.retry import caller_owns_retries
from tests.core.providers.ollama_test_support import (
    CLOUD_CHAT_URL,
    CLOUD_CONFIG,
    LOCAL_CHAT_URL,
    SAMPLE_MESSAGES,
    TEXT_RESPONSE,
    TOOL_CALL_RESPONSE,
    cloud_adapter,
    local_adapter,
    ndjson,
    sent_body,
)

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get the weather.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


@respx.mock
@pytest.mark.asyncio
async def test_native_request_uses_ollama_message_tool_and_option_shapes() -> None:
    route = respx.post(LOCAL_CHAT_URL).mock(return_value=httpx.Response(200, json=TEXT_RESPONSE))
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_dmop6zf4", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_dmop6zf4",
            "name": "get_weather",
            "content": "Sunny, 25°C",
        },
        {"role": "assistant", "content": "Sunny.", "reasoning": "The user wants the weather."},
    ]

    await local_adapter().send(
        messages,
        model_id="ministral-3:8b",
        tools=[WEATHER_TOOL],
        temperature=0.2,
        top_p=0.95,
    )

    payload = sent_body(route)
    # The keyless local Connection sends no auth header.
    assert "Authorization" not in route.calls.last.request.headers
    assert payload["stream"] is False
    assert payload["tools"] == [{"type": "function", "function": WEATHER_TOOL}]
    assert payload["options"] == {"temperature": 0.2, "top_p": 0.95}
    user, tool_call_turn, tool, answer = payload["messages"]
    # Canonical media blocks become the per-message bare-base64 images array.
    assert user == {"role": "user", "content": "What is in this image?", "images": ["aW1hZ2U="]}
    # Arguments replay as JSON objects.
    assert tool_call_turn == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_dmop6zf4",
                "function": {"name": "get_weather", "arguments": {"city": "Berlin"}},
            }
        ],
    }
    assert tool == {
        "role": "tool",
        "content": "Sunny, 25°C",
        "tool_call_id": "call_dmop6zf4",
        "tool_name": "get_weather",
    }
    # Readable reasoning replays via ``thinking``.
    assert answer == {
        "role": "assistant",
        "content": "Sunny.",
        "thinking": "The user wants the weather.",
    }


@pytest.mark.parametrize(
    ("resolver_window", "request_kwargs", "expected_payload_fields"),
    [
        pytest.param(
            None, {"temperature": None, "tools": None}, {}, id="none-kwargs-never-reach-the-wire"
        ),
        pytest.param(16384, {}, {"options": {"num_ctx": 16384}}, id="flagged-local-window"),
        pytest.param(None, {}, {}, id="non-local-model-gets-no-num-ctx"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_options_carry_only_set_values_and_the_enforced_local_window(
    resolver_window: int | None,
    request_kwargs: dict[str, Any],
    expected_payload_fields: dict[str, Any],
) -> None:
    route = respx.post(LOCAL_CHAT_URL).mock(return_value=httpx.Response(200, json=TEXT_RESPONSE))
    adapter = local_adapter(local_context_resolver=lambda _model_id: resolver_window)

    await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b", **request_kwargs)

    payload = sent_body(route)
    assert {key: payload[key] for key in ("options", "tools") if key in payload} == (
        expected_payload_fields
    )


@pytest.mark.parametrize(
    ("model_id", "effort", "think"),
    [
        pytest.param("thinking-model", "high", True, id="on-off-on"),
        pytest.param("thinking-model", "none", False, id="on-off-off"),
        pytest.param("thinking-model", None, None, id="no-effort-keeps-provider-default"),
        pytest.param("plain-model", "high", None, id="non-thinking-model-rejects-think"),
        pytest.param("unknown-model", "high", None, id="unknown-support-omits-think"),
        pytest.param("gpt-oss:20b", "xhigh", "high", id="levels-send-snapped-level-string"),
        pytest.param("gpt-oss:20b", "none", None, id="gpt-oss-cannot-accept-boolean-off"),
        pytest.param("deepseek-v4-flash", "none", False, id="levels-off-uses-boolean-switch"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_think_control_follows_catalog_reasoning_facts(
    model_id: str, effort: str | None, think: bool | str | None
) -> None:
    route = respx.post(LOCAL_CHAT_URL).mock(return_value=httpx.Response(200, json=TEXT_RESPONSE))

    await local_adapter().send(SAMPLE_MESSAGES, model_id=model_id, thinking_effort=effort)

    payload = sent_body(route)
    assert payload.get("think") == think
    assert ("think" in payload) is (think is not None)


@respx.mock
@pytest.mark.asyncio
async def test_rich_tool_result_images_use_a_request_only_user_fallback() -> None:
    route = respx.post(LOCAL_CHAT_URL).mock(return_value=httpx.Response(200, json=TEXT_RESPONSE))

    await local_adapter().send(
        [
            {
                "role": "tool",
                "tool_call_id": "call_image",
                "content": json.dumps(tool_success({"content": "image result"})),
                TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
                    {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
                    {"type": "text", "text": "[Image path: C:/diagram.png]"},
                ],
            }
        ],
        model_id="ministral-3:8b",
    )

    assert sent_body(route)["messages"] == [
        {
            "role": "tool",
            "content": "image result\n\n[Image path: C:/diagram.png]",
            "tool_call_id": "call_image",
        },
        {"role": "user", "content": "", "images": ["aW1hZ2U="]},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_non_image_media_is_rejected_before_the_request() -> None:
    route = respx.post(LOCAL_CHAT_URL)

    with pytest.raises(ProviderError) as caught:
        await local_adapter().send(
            [
                {
                    "role": "user",
                    "content": [{"type": "media", "base64": "d2F2", "media_type": "audio/wav"}],
                }
            ],
            model_id="ministral-3:8b",
        )

    assert caught.value.retryable is False
    assert not route.called


def test_completed_response_maps_content_reasoning_and_usage() -> None:
    normalized = local_adapter().normalize_response(
        {
            "message": {"role": "assistant", "content": "Answer.", "thinking": "Pondering."},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 558,
            "eval_count": 4,
        }
    )

    assert normalized["role"] == "assistant"
    assert normalized["content"] == "Answer."
    assert normalized["reasoning"] == "Pondering."
    assert normalized["tool_calls"] is None
    assert normalized["usage"] == {"input_tokens": 558, "output_tokens": 4}


@pytest.mark.parametrize(
    ("raw_tool_calls", "expected"),
    [
        pytest.param(
            TOOL_CALL_RESPONSE["message"]["tool_calls"],
            [{"id": "call_dmop6zf4", "name": "get_weather", "arguments": {"city": "Berlin"}}],
            id="object-arguments",
        ),
        pytest.param(
            [{"function": {"name": "get_weather", "arguments": {}}}],
            [{"id": "tool_call_0", "name": "get_weather", "arguments": {}}],
            id="missing-id-gets-positional-fallback",
        ),
        pytest.param(
            {
                "id": "call_one",
                "function": {"name": "get_weather", "arguments": {"city": "Berlin"}},
            },
            [{"id": "call_one", "name": "get_weather", "arguments": {"city": "Berlin"}}],
            id="collapsed-single-call-object",
        ),
    ],
)
def test_completed_response_normalizes_tool_calls(raw_tool_calls: Any, expected: list) -> None:
    normalized = local_adapter().normalize_response(
        {
            "message": {"role": "assistant", "content": "", "tool_calls": raw_tool_calls},
            "done": True,
        }
    )

    assert normalized["content"] is None
    assert normalized["tool_calls"] == expected


def test_malformed_tool_arguments_become_a_rejected_call() -> None:
    normalized = local_adapter().normalize_response(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_bad", "function": {"name": "get_weather", "arguments": "{broken"}}
                ],
            },
            "done": True,
        }
    )

    assert normalized["tool_calls"][0]["arguments"] == {}
    assert normalized["tool_calls"][0]["rejection"]["code"] == "malformed_tool_arguments"


@pytest.mark.parametrize(
    ("counters", "expected_usage"),
    [
        pytest.param(
            {"prompt_eval_count": 611, "eval_count": 12},
            {"input_tokens": 611, "output_tokens": 12},
            id="both-counters",
        ),
        pytest.param({"eval_count": 2572}, {"output_tokens": 2572}, id="cloud-omits-prompt-count"),
        pytest.param({}, None, id="no-counters-no-usage"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_usage_counters_map_on_completed_responses_and_streams(
    counters: dict[str, int], expected_usage: dict[str, int] | None
) -> None:
    final = {"message": {"content": "x"}, "done": True, "done_reason": "stop", **counters}
    adapter = local_adapter()
    respx.post(LOCAL_CHAT_URL).mock(return_value=ndjson(final))

    completed = adapter.normalize_response(final)
    deltas = [d async for d in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b")]

    assert completed.get("usage") == expected_usage
    assert [
        {k: v for k, v in d.items() if k != "type"} for d in deltas if d["type"] == "usage"
    ] == ([expected_usage] if expected_usage else [])


@respx.mock
@pytest.mark.asyncio
async def test_ndjson_stream_yields_reasoning_content_usage_and_finish() -> None:
    # Chunk shapes from the live probe.
    route = respx.post(LOCAL_CHAT_URL).mock(
        return_value=ndjson(
            {"model": "m", "message": {"role": "assistant", "thinking": "Hmm"}, "done": False},
            {"model": "m", "message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"model": "m", "message": {"role": "assistant", "content": "lo"}, "done": False},
            {
                "model": "m",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 558,
                "eval_count": 4,
            },
        )
    )

    deltas = [d async for d in local_adapter().stream(SAMPLE_MESSAGES, model_id="thinking-model")]

    assert sent_body(route)["stream"] is True
    assert deltas == [
        {"type": "reasoning_delta", "text": "Hmm"},
        {"type": "content_delta", "text": "Hel"},
        {"type": "content_delta", "text": "lo"},
        {"type": "usage", "input_tokens": 558, "output_tokens": 4},
        {"type": "finish", "reason": "stop"},
    ]


@pytest.mark.parametrize(
    ("arguments", "arguments_delta"),
    [
        pytest.param({"city": "Berlin"}, '{"city":"Berlin"}', id="whole-object-serialized"),
        pytest.param("{broken", '"{broken"', id="malformed-preserved-for-chat-rejection"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_streamed_tool_call_arrives_whole_with_tool_finish(
    arguments: Any, arguments_delta: str
) -> None:
    respx.post(LOCAL_CHAT_URL).mock(
        return_value=ndjson(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "get_weather", "arguments": arguments},
                        }
                    ],
                },
                "done": False,
            },
            {"message": {"content": ""}, "done": True, "done_reason": "stop"},
        )
    )

    deltas = [d async for d in local_adapter().stream(SAMPLE_MESSAGES, model_id="ministral-3:8b")]

    assert deltas[0] == {
        "type": "tool_call_delta",
        "id": "call_1",
        "name_delta": "get_weather",
        "arguments_delta": arguments_delta,
    }
    assert deltas[-1] == {"type": "finish", "reason": "tool_calls"}


@pytest.mark.parametrize(
    ("done_reason", "has_tool_calls", "expected"),
    [
        ("stop", False, "stop"),
        pytest.param("stop", True, "tool_calls", id="stop-with-calls"),
        ("tool_calls", True, "tool_calls"),
        pytest.param("length", False, "output_truncated", id="length-is-output-truncation"),
        pytest.param("length", True, "output_truncated", id="length-never-authorizes-dispatch"),
        pytest.param("unrecognized", True, "unknown", id="unrecognized-cannot-authorize-dispatch"),
        pytest.param(None, True, "unknown", id="missing-cannot-authorize-dispatch"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_completed_response_and_stream_share_fail_closed_terminal_outcome(
    done_reason: object, has_tool_calls: bool, expected: str
) -> None:
    message: dict[str, Any] = {"role": "assistant", "content": "answer"}
    if has_tool_calls:
        message["tool_calls"] = [{"id": "call_1", "function": {"name": "status", "arguments": {}}}]
    response = {"message": message, "done": True, "done_reason": done_reason}
    adapter = local_adapter()
    respx.post(LOCAL_CHAT_URL).mock(return_value=ndjson(response))

    normalized = adapter.normalize_response(response)
    accumulator = StreamingAccumulator()
    async for delta in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b"):
        accumulator.add_delta(delta)
    streamed = accumulator.finalize_assistant_fields()

    assert normalized["terminal_outcome"] == expected
    assert streamed.finish_reason == expected
    assert streamed.tool_calls == normalized["tool_calls"]


@pytest.mark.parametrize("batched", [False, True])
@respx.mock
@pytest.mark.asyncio
async def test_native_stream_idless_calls_survive_chat_accumulation(batched: bool) -> None:
    calls = [
        {"function": {"name": "read", "arguments": {"path": "a"}}},
        {"function": {"name": "search", "arguments": {"q": "b"}}},
        {"id": "real_call", "function": {"name": "read", "arguments": {"path": "c"}}},
    ]
    chunks = [
        {"message": {"tool_calls": group}, "done": False}
        for group in ([calls] if batched else [[call] for call in calls])
    ]
    respx.post(LOCAL_CHAT_URL).mock(
        return_value=ndjson(*chunks, {"message": {}, "done": True, "done_reason": "stop"})
    )
    accumulator = StreamingAccumulator()

    async for delta in local_adapter().stream(SAMPLE_MESSAGES, model_id="ministral-3:8b"):
        accumulator.add_delta(delta)

    fields = accumulator.finalize_assistant_fields()
    normalized = fields.tool_calls
    assert normalized is not None
    # Fallback ids count Calls across the whole response, not per NDJSON chunk.
    assert len({call["id"] for call in normalized}) == 3
    assert [(call["name"], call["arguments"]) for call in normalized] == [
        ("read", {"path": "a"}),
        ("search", {"q": "b"}),
        ("read", {"path": "c"}),
    ]
    assert normalized[-1]["id"] == "real_call"
    assert fields.finish_reason == "tool_calls"


@pytest.mark.parametrize(
    ("chunk", "error_class", "retryable"),
    [
        pytest.param(
            {"error": "model not found"}, ProviderError, False, id="in-band-error-is-fatal"
        ),
        pytest.param(
            {"message": {"content": "partial"}, "done": False},
            NetworkError,
            True,
            id="missing-done-chunk",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_stream_failures(
    chunk: dict[str, Any], error_class: type[ProviderError], retryable: bool
) -> None:
    respx.post(LOCAL_CHAT_URL).mock(return_value=ndjson(chunk))

    with pytest.raises(error_class) as caught:
        async for _ in local_adapter().stream(SAMPLE_MESSAGES, model_id="ministral-3:8b"):
            pass

    assert caught.value.retryable is retryable


@pytest.mark.parametrize("cloud", [False, True])
@respx.mock
@pytest.mark.asyncio
async def test_refused_connection_names_the_mode_specific_cause(cloud: bool) -> None:
    if cloud:
        connection = CLOUD_CONFIG.get_connection("api-key")
        adapter: Any = cloud_adapter(CLOUD_CONFIG, connection.base_url, connection.auth)
        url, cause = CLOUD_CHAT_URL, "Ollama Cloud is not reachable"
    else:
        adapter, url, cause = local_adapter(), LOCAL_CHAT_URL, "is the Ollama service running?"
    respx.post(url).mock(side_effect=httpx.ConnectError("connection refused"))

    with caller_owns_retries(), pytest.raises(NetworkError) as caught:
        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b")

    assert caught.value.retryable is True
    assert cause in str(caught.value)
    # Cloud failures never suggest starting a local daemon.
    assert ("service running" in str(caught.value)) is not cloud
    await adapter.aclose()
