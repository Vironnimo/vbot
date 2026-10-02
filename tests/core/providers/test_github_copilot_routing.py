"""GitHub Copilot Adapter routing: endpoint selection, request shaping, headers and limits."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import respx

from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_UNKNOWN,
)
from core.providers.errors import ProviderError
from core.providers.github_copilot import (
    CHAT_COMPLETIONS_ENDPOINT,
    MESSAGES_ENDPOINT,
    RESPONSES_ENDPOINT,
)
from core.providers.github_copilot_responses import REASONING_ENCRYPTED_CONTENT_INCLUDE
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY
from tests.core.providers.github_copilot_test_support import (
    API_KEY,
    COPILOT_CONFIG,
    ENDPOINT_URLS,
    copilot_metadata,
    make_adapter,
    send_exchange,
    stream_deltas,
)

_SEARCH_TOOL: dict[str, Any] = {
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
_OPENAI_METADATA = copilot_metadata(
    "OpenAI",
    "gpt-5.2",
    [CHAT_COMPLETIONS_ENDPOINT, RESPONSES_ENDPOINT],
    reasoning_efforts=["low", "medium", "high", "xhigh"],
    tool_calls=True,
    parallel_tool_calls=True,
    streaming=True,
    structured_outputs=True,
)
_IMAGE = {"type": "media", "media_type": "image/png", "base64": "aW1hZ2VkYXRh"}


# ---------------------------------------------------------------------------
# Endpoint selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "adapter_kwargs", "expected_endpoint"),
    [
        pytest.param("gpt-5.2", {"metadata": _OPENAI_METADATA}, RESPONSES_ENDPOINT, id="openai"),
        pytest.param(
            "claude-sonnet-4.6",
            {
                "metadata": copilot_metadata(
                    "Anthropic", "claude-sonnet-4.6", [CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT]
                )
            },
            MESSAGES_ENDPOINT,
            id="anthropic",
        ),
        pytest.param(
            "gemini-3.1-pro-preview",
            {
                "metadata": copilot_metadata(
                    "Google",
                    "gemini-3.1-pro-preview",
                    [CHAT_COMPLETIONS_ENDPOINT, RESPONSES_ENDPOINT],
                )
            },
            CHAT_COMPLETIONS_ENDPOINT,
            id="google-stays-chat-first",
        ),
        pytest.param(
            "gemini-2.5-pro", {}, CHAT_COMPLETIONS_ENDPOINT, id="catalog-without-endpoints"
        ),
        pytest.param("unknown-copilot-model", {}, CHAT_COMPLETIONS_ENDPOINT, id="unknown-model"),
        pytest.param(
            "gpt-5-mini", {"lookup": None}, CHAT_COMPLETIONS_ENDPOINT, id="static-fallback"
        ),
        pytest.param(
            "gpt-5-mini",
            {"metadata": copilot_metadata("OpenAI", "gpt-5-mini", [RESPONSES_ENDPOINT])},
            RESPONSES_ENDPOINT,
            id="metadata-wins-over-static-fallback",
        ),
        pytest.param(
            "copilot-model",
            {"metadata": copilot_metadata("", "", [RESPONSES_ENDPOINT, MESSAGES_ENDPOINT])},
            MESSAGES_ENDPOINT,
            id="first-advertised-wire-without-a-preference",
        ),
        pytest.param(
            "claude-next",
            {"metadata": copilot_metadata("", "", [CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT])},
            MESSAGES_ENDPOINT,
            id="model-id-prefix-without-vendor",
        ),
        pytest.param(
            "gpt-5.2",
            {
                "metadata": copilot_metadata(
                    "OpenAI", "gpt-5.2", [CHAT_COMPLETIONS_ENDPOINT, "ws:/responses"]
                )
            },
            CHAT_COMPLETIONS_ENDPOINT,
            id="websocket-responses-is-not-an-http-wire",
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_selects_the_endpoint_from_model_facts(
    model_id: str, adapter_kwargs: dict[str, Any], expected_endpoint: str
) -> None:
    exchange = await send_exchange(make_adapter(**adapter_kwargs), model_id=model_id)

    assert exchange.endpoint == expected_endpoint
    assert exchange.payload["model"] == model_id


# ---------------------------------------------------------------------------
# /chat/completions shaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "adapter_kwargs", "request_kwargs", "expected", "absent"),
    [
        pytest.param(
            "unknown-copilot-model",
            {},
            {
                "thinking_effort": "high",
                "tools": [_SEARCH_TOOL],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_output_tokens": 2048,
            },
            {"temperature": 0.2},
            {"reasoning_effort", "tools", "response_format", "max_output_tokens"},
            id="unknown-model-omits-optional-controls",
        ),
        pytest.param(
            "gpt-5-mini",
            {"lookup": None},
            {
                "thinking_effort": "high",
                "tools": [_SEARCH_TOOL],
                "temperature": 0.4,
                "top_p": 0.9,
                "max_output_tokens": 2048,
            },
            {"reasoning_effort": "high", "temperature": 0.4, "top_p": 0.9},
            {"max_output_tokens"},
            id="static-fallback-keeps-validated-controls",
        ),
        pytest.param(
            "gemini-3.1-pro-preview",
            {
                "metadata": copilot_metadata(
                    "Google",
                    "gemini-3.1-pro-preview",
                    [CHAT_COMPLETIONS_ENDPOINT, RESPONSES_ENDPOINT],
                    reasoning_efforts=["low", "medium", "high"],
                    tool_calls=True,
                    structured_outputs=True,
                )
            },
            {
                "thinking_effort": "high",
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            {"temperature": 0.2, "response_format": {"type": "json_object"}},
            {"reasoning_effort"},
            id="gemini-override-clears-efforts",
        ),
        pytest.param(
            "gemini-2.5-pro",
            {},
            {
                "thinking_budget": 4096,
                "thinking": {"type": "enabled", "budget_tokens": 4096},
                "output_config": {"effort": "high"},
            },
            {},
            {"thinking_budget", "thinking", "output_config", "reasoning_effort"},
            id="endpoint-specific-thinking-is-stripped",
        ),
    ],
)
@pytest.mark.asyncio
async def test_chat_route_forwards_only_controls_the_model_supports(
    model_id: str,
    adapter_kwargs: dict[str, Any],
    request_kwargs: dict[str, Any],
    expected: dict[str, Any],
    absent: set[str],
) -> None:
    exchange = await send_exchange(
        make_adapter(**adapter_kwargs), model_id=model_id, **request_kwargs
    )

    assert exchange.endpoint == CHAT_COMPLETIONS_ENDPOINT
    assert {key: exchange.payload.get(key) for key in expected} == expected
    if "tools" in request_kwargs and "tools" not in absent:
        assert [tool["function"]["name"] for tool in exchange.payload["tools"]] == ["search"]
    assert not absent & exchange.payload.keys()


_REASONING_DETAILS = [{"type": "reasoning.text", "text": "Need docs lookup."}]


def test_chat_normalize_response_surfaces_visible_reasoning_details() -> None:
    response = {
        "id": "chatcmpl-gemini-1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Gemini reply",
                    "reasoning_details": _REASONING_DETAILS,
                },
                "finish_reason": "stop",
            }
        ],
    }

    assert make_adapter(lookup=None).normalize_response(response) == {
        "role": "assistant",
        "content": "Gemini reply",
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {"reasoning_details": _REASONING_DETAILS},
        "tool_calls": None,
        "terminal_outcome": TERMINAL_OUTCOME_STOP,
    }


@pytest.mark.asyncio
async def test_chat_stream_backfills_visible_reasoning_from_reasoning_details() -> None:
    body = (
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text",'
        '"text":"Need docs lookup."}]}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    deltas = await stream_deltas(
        make_adapter(), body, model_id="gemini-3.1-pro-preview", thinking_effort="high"
    )

    assert deltas == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {"type": "reasoning_meta", "reasoning_meta": {"reasoning_details": _REASONING_DETAILS}},
        {"type": "finish", "reason": TERMINAL_OUTCOME_STOP},
    ]


# ---------------------------------------------------------------------------
# /responses shaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responses_route_sends_exact_payload_and_normalizes_the_reply() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
        "encrypted_content": "opaque",
    }
    function_call = {
        "type": "function_call",
        "call_id": "call_1",
        "function": {"name": "search", "arguments": '{"q":"docs"}'},
    }
    reply = {
        "id": "resp-1",
        "output": [reasoning_item, function_call],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    adapter = make_adapter()

    exchange = await send_exchange(
        adapter,
        [{"role": "user", "content": "Look up docs"}],
        model_id="gpt-5-mini",
        reply=reply,
        thinking_effort="high",
        tools=[_SEARCH_TOOL],
        response_format={"type": "json_object"},
        temperature=0.25,
    )

    assert exchange.endpoint == RESPONSES_ENDPOINT
    assert exchange.payload == {
        "model": "gpt-5-mini",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Look up docs"}]}],
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": _SEARCH_TOOL["function"]["parameters"],
                "strict": False,
            }
        ],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": [REASONING_ENCRYPTED_CONTENT_INCLUDE],
        "text": {"format": {"type": "json_object"}},
        # The catalog output ceiling wins over the Provider default.
        "max_output_tokens": 64000,
    }
    assert adapter.normalize_response(exchange.response) == {
        "terminal_outcome": TERMINAL_OUTCOME_UNKNOWN,
        "role": "assistant",
        "content": None,
        "reasoning": "Need docs lookup.",
        "reasoning_summary": ["Need docs lookup."],
        "reasoning_meta": {
            "response_id": "resp-1",
            "response_output": [reasoning_item, function_call],
            "reasoning_items": [reasoning_item],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }


@pytest.mark.parametrize(
    ("model_id", "adapter_kwargs", "request_kwargs", "expected_controls"),
    [
        pytest.param(
            "gpt-5.4-partial",
            {},
            {"thinking_effort": "high", "temperature": 0.25, "top_p": 0.9},
            {"max_output_tokens": 4096, "top_p": 0.9},
            id="partial-metadata-stays-conservative",
        ),
        pytest.param(
            "gpt-5-mini",
            {
                "metadata": copilot_metadata(
                    "OpenAI", "gpt-5-mini", [RESPONSES_ENDPOINT], reasoning_efforts=["low"]
                )
            },
            {"thinking_effort": "high", "reasoning_effort": "max", "max_output_tokens": 2048},
            {
                "reasoning": {"effort": "low", "summary": "auto"},
                "include": [REASONING_ENCRYPTED_CONTENT_INCLUDE],
                "max_output_tokens": 2048,
            },
            id="effort-maps-to-nearest-allowed",
        ),
        pytest.param(
            "gpt-5.2",
            {
                "metadata": copilot_metadata(
                    "OpenAI",
                    "gpt-5.2",
                    [CHAT_COMPLETIONS_ENDPOINT, RESPONSES_ENDPOINT],
                    reasoning_efforts=["low", "medium", "high", "xhigh"],
                    tool_calls=True,
                    parallel_tool_calls=False,
                    structured_outputs=False,
                )
            },
            {
                "thinking_effort": "xhigh",
                "parallel_tool_calls": True,
                "response_format": {"type": "json_object"},
            },
            {
                "reasoning": {"effort": "xhigh", "summary": "auto"},
                "include": [REASONING_ENCRYPTED_CONTENT_INCLUDE],
                "max_output_tokens": 4096,
            },
            id="unsupported-features-are-dropped",
        ),
    ],
)
@pytest.mark.asyncio
async def test_responses_route_forwards_only_controls_the_metadata_allows(
    model_id: str,
    adapter_kwargs: dict[str, Any],
    request_kwargs: dict[str, Any],
    expected_controls: dict[str, Any],
) -> None:
    exchange = await send_exchange(
        make_adapter(**adapter_kwargs), model_id=model_id, **request_kwargs
    )

    assert exchange.endpoint == RESPONSES_ENDPOINT
    assert exchange.payload == {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        **expected_controls,
    }


# ---------------------------------------------------------------------------
# Headers and local limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "expected_endpoint"),
    [
        ("unknown-copilot-model", CHAT_COMPLETIONS_ENDPOINT),
        ("gpt-5-mini", RESPONSES_ENDPOINT),
        ("claude-sonnet-4.6", MESSAGES_ENDPOINT),
    ],
)
@pytest.mark.asyncio
async def test_every_endpoint_sends_auth_extra_headers_and_user_initiator(
    model_id: str, expected_endpoint: str
) -> None:
    config = replace(COPILOT_CONFIG, extra_headers={"Editor-Version": "vBot/test"})

    exchange = await send_exchange(make_adapter(config), model_id=model_id)

    assert exchange.endpoint == expected_endpoint
    headers = exchange.request.headers
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert headers["Editor-Version"] == "vBot/test"
    assert headers["x-initiator"] == "user"
    assert "Copilot-Vision-Request" not in headers


@pytest.mark.asyncio
async def test_request_after_tool_result_is_agent_initiated() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Run it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "name": "run", "arguments": "{}"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]

    exchange = await send_exchange(make_adapter(), messages, model_id="unknown-copilot-model")

    assert exchange.request.headers["x-initiator"] == "agent"


def _vision_metadata(**vision: Any) -> dict[str, Any]:
    return copilot_metadata(
        "OpenAI", "gpt-5.4", [RESPONSES_ENDPOINT], tool_calls=True, vision=vision
    )


@pytest.mark.asyncio
async def test_image_within_catalog_limits_is_sent_with_the_vision_header() -> None:
    adapter = make_adapter(
        metadata=_vision_metadata(max_prompt_images=1, max_prompt_image_size=1024)
    )

    exchange = await send_exchange(
        adapter,
        [{"role": "user", "content": [{"type": "text", "text": "Look"}, _IMAGE]}],
        model_id="gpt-5.4",
    )

    assert exchange.request.headers["Copilot-Vision-Request"] == "true"


@pytest.mark.parametrize(
    ("metadata", "content", "limit_field"),
    [
        pytest.param(
            copilot_metadata("OpenAI", "gpt-5.4", [RESPONSES_ENDPOINT], max_prompt_tokens=1),
            "Hello",
            "max_prompt_tokens=1",
            id="prompt-tokens",
        ),
        pytest.param(
            _vision_metadata(max_prompt_images=1),
            [_IMAGE, _IMAGE],
            "max_prompt_images=1",
            id="image-count",
        ),
        pytest.param(
            _vision_metadata(max_prompt_image_size=4),
            [_IMAGE],
            "max_prompt_image_size=4",
            id="image-size",
        ),
    ],
)
@pytest.mark.asyncio
async def test_catalog_prompt_limits_fail_locally_before_any_request(
    metadata: dict[str, Any], content: Any, limit_field: str
) -> None:
    adapter = make_adapter(metadata=metadata)

    with respx.mock:
        routes = [respx.post(url) for url in ENDPOINT_URLS.values()]
        with pytest.raises(ProviderError) as caught:
            await adapter.send([{"role": "user", "content": content}], model_id="gpt-5.4")

    assert caught.value.retryable is False
    assert limit_field in str(caught.value)
    assert not any(route.called for route in routes)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(2048, 2048, id="positive"),
        pytest.param(0, None, id="non-positive"),
        pytest.param(True, None, id="bool"),
        pytest.param("2048", None, id="string"),
    ],
)
def test_image_size_limit_exposes_only_positive_integer_byte_limits(
    value: Any, expected: int | None
) -> None:
    adapter = make_adapter(metadata=_vision_metadata(max_prompt_image_size=value))

    assert adapter.image_size_limit("vision-model") == expected


@pytest.mark.parametrize(
    ("adapter_kwargs", "expected"),
    [
        pytest.param({}, IMAGE_WIRE_MEDIA_TYPES, id="without-vision-metadata"),
        pytest.param(
            {"metadata": _vision_metadata(supported_media_types=["image/png", "audio/wav"])},
            frozenset({"image/png"}),
            id="advertised-image-types",
        ),
    ],
)
def test_wire_media_support_carries_only_catalog_image_types(
    adapter_kwargs: dict[str, Any], expected: frozenset[str]
) -> None:
    assert make_adapter(**adapter_kwargs).wire_media_support("gpt-5.4") == expected


def test_reasoning_replay_keeps_full_history_on_every_endpoint() -> None:
    model_ids = ("gpt-5-mini", "claude-sonnet-4.6", "claude-haiku-4.5", "gemini-3.1-pro-preview")

    assert {
        model_id: make_adapter().reasoning_replay_policy(model_id) for model_id in model_ids
    } == dict.fromkeys(model_ids, REASONING_REPLAY_FULL_HISTORY)
    assert make_adapter(lookup=None).reasoning_replay_policy("unknown-model") == (
        REASONING_REPLAY_FULL_HISTORY
    )
