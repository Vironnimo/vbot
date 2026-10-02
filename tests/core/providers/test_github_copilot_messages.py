"""GitHub Copilot ``/v1/messages`` route: payload translation, thinking, responses, streams."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from core.providers.adapter import (
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
    request_input_budget,
)
from core.providers.errors import ProviderError
from core.providers.github_copilot import CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT
from core.providers.openai_compatible import DEFAULT_MAX_OUTPUT_TOKENS
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS
from tests.core.providers.github_copilot_test_support import (
    COPILOT_CONFIG,
    NO_DEFAULTS_CONFIG,
    copilot_metadata,
    make_adapter,
    send_exchange,
    sse_events,
    stream_deltas,
)


def _claude_metadata(family: str = "claude-sonnet-4.6", **overrides: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "reasoning_efforts": ["low", "medium", "high", "xhigh"],
        "min_thinking_budget": 1024,
        "max_thinking_budget": 32000,
        "adaptive_thinking": True,
        "tool_calls": True,
        "parallel_tool_calls": False,
        "streaming": True,
        "structured_outputs": False,
    }
    facts.update(overrides)
    return copilot_metadata(
        "Anthropic", family, [CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT], **facts
    )


_HELLO = [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]
_BUDGET_ONLY = _claude_metadata("claude-budget", reasoning_efforts=[], adaptive_thinking=False)


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_translates_conversation_and_tools_and_drops_unsafe_fields() -> None:
    exchange = await send_exchange(
        make_adapter(metadata=_claude_metadata()),
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "Search."},
            {
                "role": "assistant",
                "reasoning_meta": {
                    "content_blocks": [
                        {"type": "thinking", "thinking": "Need a lookup.", "signature": "sig-1"}
                    ]
                },
                "content": "I will search.",
                "tool_calls": [{"id": "toolu_1", "name": "search", "arguments": {"query": "vBot"}}],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "Found result."},
        ],
        model_id="claude-sonnet-4.6",
        tools=[
            {
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object", "properties": {}},
            },
            # The history Tool maps like any other Tool, without a special case.
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "parameters": HISTORY_TOOL_PARAMETERS,
            },
        ],
        tool_choice={"type": "tool", "name": "search"},
        parallel_tool_calls=True,
        max_tokens=512,
        response_format={"type": "json_object"},
        cache_control={"type": "ephemeral"},
        metadata={"ignored": True},
        text={"format": {"type": "json_schema"}},
    )

    assert exchange.endpoint == MESSAGES_ENDPOINT
    assert exchange.payload == {
        "model": "claude-sonnet-4.6",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Search."}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Need a lookup.", "signature": "sig-1"},
                    {"type": "text", "text": "I will search."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "search",
                        "input": {"query": "vBot"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Found result."}
                ],
            },
        ],
        "system": "Be precise.",
        "tools": [
            {
                "name": "search",
                "description": "Search docs",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "input_schema": HISTORY_TOOL_PARAMETERS,
            },
        ],
        "tool_choice": {"type": "tool", "name": "search"},
        "max_tokens": 512,
    }


def _image_block(data: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


@pytest.mark.asyncio
async def test_send_renders_user_and_tool_result_images_as_image_blocks() -> None:
    exchange = await send_exchange(
        make_adapter(metadata=_claude_metadata()),
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "media", "media_type": "image/png", "base64": "aW1n"},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_image",
                "content": '{"ok":true}',
                TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
                    {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"}
                ],
            },
        ],
        model_id="claude-sonnet-4.6",
    )

    assert exchange.payload["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "What is this?"}, _image_block("aW1n")],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_image",
                    "content": [{"type": "text", "text": '{"ok":true}'}, _image_block("aW1hZ2U=")],
                }
            ],
        },
    ]


@pytest.mark.parametrize(
    "media_block",
    [
        pytest.param(
            {"type": "media", "media_type": "audio/wav", "base64": "YXVkaW8="}, id="audio"
        ),
        pytest.param({"type": "media", "media_type": "image/png"}, id="missing-data"),
    ],
)
@pytest.mark.asyncio
async def test_send_rejects_media_the_endpoint_cannot_carry(media_block: dict[str, Any]) -> None:
    # Chat scopes an input budget around every send, so no local estimate runs first.
    with (
        request_input_budget("claude-sonnet-4.6", 0),
        pytest.raises(ProviderError) as caught,
    ):
        await send_exchange(
            make_adapter(metadata=_claude_metadata()),
            [{"role": "user", "content": [media_block]}],
            model_id="claude-sonnet-4.6",
        )

    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("tool_choice", "expected_choice"),
    [
        pytest.param("any", {"tool_choice": {"type": "any"}}, id="named-mode"),
        pytest.param("none", {}, id="mode-without-messages-equivalent"),
    ],
)
@pytest.mark.asyncio
async def test_send_forwards_only_tool_choices_the_endpoint_understands(
    tool_choice: str, expected_choice: dict[str, Any]
) -> None:
    search = {"name": "search", "description": "Search", "parameters": {"type": "object"}}

    exchange = await send_exchange(
        make_adapter(metadata=_claude_metadata()),
        model_id="claude-sonnet-4.6",
        tools=[search],
        tool_choice=tool_choice,
    )

    assert exchange.payload == {
        "model": "claude-sonnet-4.6",
        "messages": _HELLO,
        "tools": [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}],
        **expected_choice,
        "max_tokens": 4096,
    }


@pytest.mark.parametrize(
    ("defaults", "request_kwargs", "expected_max_tokens"),
    [
        pytest.param(None, {}, DEFAULT_MAX_OUTPUT_TOKENS, id="endpoint-default"),
        pytest.param(
            {"max_tokens": 4096},
            {"max_output_tokens": 2048},
            2048,
            id="max-output-beats-max-tokens",
        ),
        pytest.param(
            {"max_tokens": 4096}, {"max_completion_tokens": 1024}, 1024, id="max-completion-alias"
        ),
        # Provider config defaults join after the output clamp, so the wire sanitizes them.
        pytest.param({"max_output_tokens": "2048"}, {}, 2048, id="decimal-string-config-default"),
        pytest.param(
            {"max_completion_tokens": 0},
            {},
            DEFAULT_MAX_OUTPUT_TOKENS,
            id="non-positive-config-default-ignored",
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_resolves_messages_max_tokens(
    defaults: dict[str, Any] | None, request_kwargs: dict[str, Any], expected_max_tokens: int
) -> None:
    exchange = await send_exchange(
        make_adapter(replace(COPILOT_CONFIG, defaults=defaults), metadata=_claude_metadata()),
        model_id="claude-sonnet-4.6",
        **request_kwargs,
    )

    assert exchange.payload == {
        "model": "claude-sonnet-4.6",
        "messages": _HELLO,
        "max_tokens": expected_max_tokens,
    }


# ---------------------------------------------------------------------------
# Thinking and output controls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "adapter_kwargs", "request_kwargs", "expected_controls"),
    [
        pytest.param(
            "claude-opus-4.7",
            {"metadata": _claude_metadata("claude-opus-4.7")},
            {
                "thinking_effort": "xhigh",
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "xhigh", "unknown": True},
                "thinking_budget": 64000,
            },
            # Raw thinking fields never reach the wire; the effort is planned.
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "xhigh"},
                "max_tokens": 4096,
            },
            id="effort-wins-over-raw-thinking-fields",
        ),
        pytest.param(
            "claude-opus-4.7",
            {"metadata": _claude_metadata("claude-opus-4.7", reasoning_efforts=["high"])},
            {"thinking_effort": "low", "output_config": {"effort": "low", "unknown": True}},
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "high"},
                "max_tokens": 4096,
            },
            id="effort-maps-to-nearest-allowed",
        ),
        pytest.param(
            "claude-budget",
            {"config": NO_DEFAULTS_CONFIG, "metadata": _BUDGET_ONLY},
            {"thinking_effort": "high", "temperature": 0.25},
            # high -> 0.75 * 32000 = 24000, kept strictly under the 8192 default.
            {
                "thinking": {"type": "enabled", "budget_tokens": DEFAULT_MAX_OUTPUT_TOKENS - 1},
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                "temperature": 0.25,
            },
            id="budget-only-effort-clamped-under-max-tokens",
        ),
        pytest.param(
            "claude-budget",
            {"metadata": _BUDGET_ONLY, "context_window": 200_000},
            {"thinking_effort": "medium", "max_tokens": 64000},
            # medium -> 0.50 * 32000 = 16000, under the 64000 output allowance.
            {"thinking": {"type": "enabled", "budget_tokens": 16000}, "max_tokens": 64000},
            id="budget-only-effort-scales-with-headroom",
        ),
        pytest.param(
            "claude-plain",
            {
                "metadata": _claude_metadata(
                    "claude-plain",
                    reasoning_efforts=[],
                    min_thinking_budget=None,
                    max_thinking_budget=None,
                    adaptive_thinking=False,
                    tool_calls=False,
                )
            },
            {
                "thinking_effort": "high",
                "tools": [
                    {
                        "name": "search",
                        "description": "Search",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                "tool_choice": "auto",
                "output_config": {"effort": "high"},
                "thinking": {"type": "adaptive"},
                "temperature": 0.25,
            },
            {"max_tokens": 4096, "temperature": 0.25},
            id="unsupported-thinking-and-tools-are-omitted",
        ),
        pytest.param(
            "claude-haiku-4.5",
            {"metadata": _claude_metadata("claude-haiku-4.5", reasoning_efforts=[])},
            {
                "thinking_effort": "high",
                "thinking_budget": 2048,
                "thinking": {"type": "enabled", "budget_tokens": 2048},
                "output_config": {"effort": "high"},
                "temperature": 0.25,
            },
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "max_tokens": 4096,
                "temperature": 0.25,
            },
            id="haiku-override-drops-budget-controls",
        ),
        pytest.param(
            "claude-sonnet-4.6",
            {},
            {
                "thinking_effort": "high",
                "response_format": {"type": "json_object"},
                "temperature": 0.25,
                "top_p": 0.9,
                "top_k": 10,
                "stop_sequences": ["END"],
            },
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "high"},
                "max_tokens": 32000,
                "top_p": 0.9,
                "top_k": 10,
                "stop_sequences": ["END"],
            },
            id="sonnet-catalog-omits-temperature-while-thinking",
        ),
        pytest.param(
            "claude-haiku-4.5",
            {},
            {
                "thinking_effort": "high",
                "response_format": {"type": "json_object"},
                "temperature": 0.25,
                "top_p": 0.9,
            },
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "max_tokens": 4096,
                "temperature": 0.25,
                "top_p": 0.9,
            },
            id="haiku-keeps-temperature-while-thinking",
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_applies_the_model_thinking_policy(
    model_id: str,
    adapter_kwargs: dict[str, Any],
    request_kwargs: dict[str, Any],
    expected_controls: dict[str, Any],
) -> None:
    exchange = await send_exchange(
        make_adapter(**adapter_kwargs), model_id=model_id, **request_kwargs
    )

    assert exchange.endpoint == MESSAGES_ENDPOINT
    assert exchange.payload == {"model": model_id, "messages": _HELLO, **expected_controls}


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(
            {
                "content": [
                    {"type": "thinking", "thinking": "I should inspect.", "signature": "sig-1"},
                    {"type": "redacted_thinking", "data": "opaque-redacted"},
                    {"type": "text", "text": "Use this."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read",
                        "input": {"path": "README.md"},
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
            {
                "role": "assistant",
                "content": "Use this.",
                "reasoning": "I should inspect.",
                "reasoning_meta": {
                    "content_blocks": [
                        {"type": "thinking", "thinking": "I should inspect.", "signature": "sig-1"},
                        {"type": "redacted_thinking", "data": "opaque-redacted"},
                    ]
                },
                "tool_calls": [
                    {"id": "toolu_1", "name": "read", "arguments": {"path": "README.md"}}
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
            id="text-thinking-tools-usage",
        ),
        pytest.param(
            {
                "content": [
                    {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                    {"type": "text", "text": "Done."},
                ]
            },
            {
                "role": "assistant",
                "content": "Done.",
                "reasoning": "Need to inspect first.",
                "reasoning_meta": {
                    "content_blocks": [
                        {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"}
                    ]
                },
                "tool_calls": None,
            },
            id="visible-thinking-text-variant",
        ),
        pytest.param(
            {
                "content": [{"type": "text", "text": "Use this."}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 500,
                    "cache_creation_input_tokens": 50,
                    "output_tokens_details": {"thinking_tokens": 12},
                },
            },
            {
                "role": "assistant",
                "content": "Use this.",
                "reasoning": None,
                "reasoning_meta": None,
                "tool_calls": None,
                "usage": {
                    "input_tokens": 560,
                    "output_tokens": 20,
                    "cache_read_tokens": 500,
                    "cache_write_tokens": 50,
                    "reasoning_tokens": 12,
                },
            },
            id="cache-tokens-fold-into-input",
        ),
    ],
)
def test_normalize_response_maps_messages_content_blocks(
    response: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert make_adapter().normalize_response(response) == expected


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

_END_TURN = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
_TOOL_USE_START = {
    "type": "content_block_start",
    "index": 0,
    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"},
}


@pytest.mark.parametrize(
    ("model_id", "events", "expected"),
    [
        pytest.param(
            "claude-sonnet-4.6",
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "thinking_delta", "thinking": "Plan"},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "signature_delta", "signature": "sig-stream"},
                },
                {"type": "content_block_stop", "index": 1},
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {"type": "tool_use", "id": "toolu_2", "name": "write"},
                },
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path"'},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 11},
                },
            ],
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "reasoning_delta", "text": "Plan"},
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "Plan", "signature": "sig-stream"}
                        ]
                    },
                },
                {
                    "type": "tool_call_delta",
                    "id": "toolu_2",
                    "name_delta": "write",
                    "arguments_delta": "",
                },
                {
                    "type": "tool_call_delta",
                    "id": "toolu_2",
                    "name_delta": "",
                    "arguments_delta": '{"path"',
                },
                {"type": "finish", "reason": TERMINAL_OUTCOME_TOOL_CALLS},
                {"type": "usage", "input_tokens": 7, "output_tokens": 11},
            ],
            id="text-thinking-tool-usage",
        ),
        pytest.param(
            "claude-haiku-4.5",
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Need docs lookup."},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "sig-stream"},
                },
                {"type": "content_block_stop", "index": 0},
                {**_END_TURN, "usage": {"output_tokens": 2}},
            ],
            [
                {"type": "reasoning_delta", "text": "Need docs lookup."},
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "text": "Need docs lookup.",
                                "signature": "sig-stream",
                            }
                        ]
                    },
                },
                # No ``message_start`` input count: no usage is invented.
                {"type": "finish", "reason": TERMINAL_OUTCOME_STOP},
            ],
            id="visible-thinking-text-delta-without-input-usage",
        ),
        pytest.param(
            "claude-sonnet-4.6",
            [_TOOL_USE_START, {"type": "message_delta", "delta": {"stop_reason": "copilot_stop"}}],
            [
                {
                    "type": "tool_call_delta",
                    "id": "toolu_1",
                    "name_delta": "search",
                    "arguments_delta": "",
                },
                {"type": "finish", "reason": TERMINAL_OUTCOME_UNKNOWN},
            ],
            id="unknown-stop-reason-stays-unknown",
        ),
        pytest.param(
            "claude-sonnet-4.6",
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "redacted_thinking",
                        "data": "opaque",
                        "provider_private": "must-not-leak",
                    },
                },
                {"type": "content_block_stop", "index": 0},
                _END_TURN,
            ],
            [
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "content_blocks": [{"type": "redacted_thinking", "data": "opaque"}]
                    },
                },
                {"type": "finish", "reason": TERMINAL_OUTCOME_STOP},
            ],
            id="redacted-thinking-keeps-only-safe-fields",
        ),
        pytest.param(
            "claude-sonnet-4.6",
            [
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 7,
                            "cache_read_input_tokens": 300,
                            "cache_creation_input_tokens": 40,
                        }
                    },
                },
                {
                    **_END_TURN,
                    "usage": {"output_tokens": 11, "output_tokens_details": {"thinking_tokens": 7}},
                },
            ],
            [
                {"type": "finish", "reason": TERMINAL_OUTCOME_STOP},
                {
                    "type": "usage",
                    "input_tokens": 347,
                    "output_tokens": 11,
                    "cache_read_tokens": 300,
                    "cache_write_tokens": 40,
                    "reasoning_tokens": 7,
                },
            ],
            id="message-start-cache-usage-folds-into-input",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stream_normalizes_messages_events(
    model_id: str, events: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    deltas = await stream_deltas(make_adapter(), sse_events(*events), model_id=model_id)

    assert deltas == expected


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        pytest.param(
            {"type": "overloaded_error", "message": "Copilot is busy"}, True, id="transient"
        ),
        pytest.param({"type": "invalid_request_error", "message": "Bad input"}, False, id="fatal"),
    ],
)
@pytest.mark.asyncio
async def test_stream_error_event_is_classified_and_keeps_the_provider_message(
    error: dict[str, str], retryable: bool
) -> None:
    body = sse_events({"type": "error", "error": error})

    with pytest.raises(ProviderError) as caught:
        await stream_deltas(make_adapter(), body, model_id="claude-sonnet-4.6")

    assert type(caught.value) is ProviderError
    assert caught.value.retryable is retryable
    assert error["message"] in str(caught.value)
