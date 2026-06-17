"""Tests for GitHub Copilot ``/v1/messages`` helpers."""

from typing import Any

import pytest

from core.providers.errors import ProviderError
from core.providers.github_copilot_messages import (
    CopilotMessagesStreamState,
    build_copilot_messages_payload,
    normalize_copilot_messages_response,
    normalize_copilot_messages_stream_event,
)
from core.providers.github_copilot_policy import (
    CHAT_COMPLETIONS_ENDPOINT,
    MESSAGES_ENDPOINT,
    copilot_model_policy,
)


def _messages_policy(
    model_id: str = "claude-sonnet-4.6",
    **overrides,
):
    metadata = {
        "github_copilot": {
            "vendor": "Anthropic",
            "family": model_id,
            "version": model_id,
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT],
            "reasoning_efforts": ["low", "medium", "high", "xhigh"],
            "min_thinking_budget": 1024,
            "max_thinking_budget": 32000,
            "adaptive_thinking": True,
            "tool_calls": True,
            "parallel_tool_calls": False,
            "streaming": True,
            "structured_outputs": False,
        }
    }
    metadata["github_copilot"].update(overrides)
    return copilot_model_policy(model_id, metadata)


def test_build_payload_extracts_system_and_translates_messages_tools_and_results() -> None:
    policy = _messages_policy()
    payload = build_copilot_messages_payload(
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "Search."},
            {
                "role": "assistant",
                "reasoning_meta": {
                    "content_blocks": [
                        {
                            "type": "thinking",
                            "thinking": "Need a lookup.",
                            "signature": "sig-1",
                        }
                    ]
                },
                "content": "I will search.",
                "tool_calls": [{"id": "toolu_1", "name": "search", "arguments": {"query": "vBot"}}],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "Found result."},
        ],
        model_id="claude-sonnet-4.6",
        policy=policy,
        tools=[
            {
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        tool_choice={"type": "tool", "name": "search"},
        parallel_tool_calls=True,
        max_tokens=512,
        response_format={"type": "json_object"},
        cache_control={"type": "ephemeral"},
    )

    assert payload == {
        "model": "claude-sonnet-4.6",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Search."}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Need a lookup.",
                        "signature": "sig-1",
                    },
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
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "Found result.",
                    }
                ],
            },
        ],
        "system": "Be precise.",
        "tools": [
            {
                "name": "search",
                "description": "Search docs",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": {"type": "tool", "name": "search"},
        "max_tokens": 512,
    }
    assert "parallel_tool_calls" not in payload
    assert "response_format" not in payload
    assert "cache_control" not in payload


def test_build_payload_uses_default_max_tokens_when_caller_omits_it() -> None:
    policy = _messages_policy()

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="claude-sonnet-4.6",
        policy=policy,
    )

    assert payload == {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 8192,
    }


def test_build_payload_keeps_only_endpoint_safe_fields() -> None:
    policy = _messages_policy()

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="claude-sonnet-4.6",
        policy=policy,
        max_tokens=2048,
        metadata={"ignored": True},
        text={"format": {"type": "json_schema"}},
    )

    assert payload == {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 2048,
    }
    assert "max_output_tokens" not in payload
    assert "max_completion_tokens" not in payload
    assert "metadata" not in payload
    assert "text" not in payload


def test_build_payload_prefers_messages_output_token_aliases_over_provider_default() -> None:
    policy = _messages_policy()

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="claude-sonnet-4.6",
        policy=policy,
        max_tokens=4096,
        max_output_tokens=2048,
    )

    assert payload == {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 2048,
    }


def test_build_payload_accepts_max_completion_tokens_alias() -> None:
    policy = _messages_policy()

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="claude-sonnet-4.6",
        policy=policy,
        max_completion_tokens="1024",
    )

    assert payload == {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 1024,
    }


def test_build_payload_gates_thinking_by_exact_policy() -> None:
    policy = _messages_policy()

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id="claude-opus-4.7",
        policy=policy,
        thinking_effort="xhigh",
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "xhigh", "unknown": True},
        thinking_budget=64000,
    )

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "xhigh"}


def test_build_payload_maps_output_config_to_nearest_policy_effort() -> None:
    policy = _messages_policy(reasoning_efforts=["high"])

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id="claude-sonnet-4.6",
        policy=policy,
        thinking_effort="low",
        output_config={"effort": "low", "unknown": True},
    )

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "high"}


def test_build_payload_omits_unsupported_tools_and_reasoning_controls() -> None:
    model_id = "claude-haiku-4.5-runtime-metadata"
    policy = _messages_policy(
        model_id,
        reasoning_efforts=[],
        min_thinking_budget=None,
        max_thinking_budget=None,
        adaptive_thinking=False,
        tool_calls=False,
    )

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Hello"}],
        model_id=model_id,
        policy=policy,
        thinking_effort="high",
        tools=[{"name": "search", "description": "Search", "parameters": {}}],
        tool_choice="auto",
        output_config={"effort": "high"},
        thinking={"type": "adaptive"},
    )

    assert payload == {
        "model": model_id,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 8192,
    }


def test_build_payload_supports_enabled_budget_when_policy_allows_budget() -> None:
    policy = _messages_policy()

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think with budget."}],
        model_id="claude-sonnet-4.6",
        policy=policy,
        thinking_budget=2048,
    )

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_build_payload_omits_temperature_for_sonnet_when_adaptive_thinking_is_active() -> None:
    policy = _messages_policy(model_id="claude-sonnet-4.6")

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id="claude-sonnet-4.6",
        policy=policy,
        thinking_effort="high",
        temperature=0.25,
        top_p=0.9,
    )

    assert payload == {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Think."}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "high"},
        "max_tokens": 8192,
        "top_p": 0.9,
    }
    assert "temperature" not in payload


def test_build_payload_keeps_temperature_for_haiku_when_adaptive_thinking_is_active() -> None:
    policy = _messages_policy(model_id="claude-haiku-4.5")

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id="claude-haiku-4.5",
        policy=policy,
        thinking_effort="high",
        temperature=0.25,
        top_p=0.9,
    )

    assert payload == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Think."}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 8192,
        "temperature": 0.25,
        "top_p": 0.9,
    }


def test_build_payload_haiku_requests_visible_thinking_without_reasoning_effort_support() -> None:
    policy = _messages_policy(
        model_id="claude-haiku-4.5",
        reasoning_efforts=[],
        adaptive_thinking=True,
        min_thinking_budget=None,
        max_thinking_budget=None,
    )

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id="claude-haiku-4.5",
        policy=policy,
        thinking_effort="high",
        temperature=0.25,
        top_p=0.9,
    )

    assert payload == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Think."}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 8192,
        "temperature": 0.25,
        "top_p": 0.9,
    }
    assert "output_config" not in payload


def test_build_payload_haiku_ignores_budget_controls_from_bundled_metadata_shape() -> None:
    policy = _messages_policy(
        model_id="claude-haiku-4.5",
        reasoning_efforts=[],
        adaptive_thinking=True,
        min_thinking_budget=1024,
        max_thinking_budget=32000,
    )

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id="claude-haiku-4.5",
        policy=policy,
        thinking_budget=2048,
        thinking={"type": "enabled", "budget_tokens": 2048},
        thinking_effort="high",
        output_config={"effort": "high"},
        temperature=0.25,
    )

    assert payload == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Think."}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 8192,
        "temperature": 0.25,
    }
    assert "output_config" not in payload


def test_build_payload_budget_only_model_sends_native_thinking_budget() -> None:
    """A budget-capable model with no adaptive thinking derives a native budget."""
    model_id = "claude-haiku-4.5-runtime-metadata"
    policy = _messages_policy(
        model_id=model_id,
        reasoning_efforts=[],
        adaptive_thinking=False,
    )

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id=model_id,
        policy=policy,
        thinking_effort="high",
        temperature=0.25,
    )

    # high → 0.75 * 32000 = 24000, clamped strictly under the default 8192 max.
    assert payload == {
        "model": model_id,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Think."}]}],
        "thinking": {"type": "enabled", "budget_tokens": 8191},
        "max_tokens": 8192,
        "temperature": 0.25,
    }
    assert "output_config" not in payload


def test_build_payload_budget_only_model_scales_budget_under_explicit_max_tokens() -> None:
    """With headroom, the budget scales with the effort and stays under max_tokens."""
    model_id = "claude-haiku-4.5-runtime-metadata"
    policy = _messages_policy(
        model_id=model_id,
        reasoning_efforts=[],
        adaptive_thinking=False,
    )

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id=model_id,
        policy=policy,
        thinking_effort="medium",
        max_tokens=64000,
    )

    # medium → 0.50 * 32000 = 16000, under the 64000 output allowance.
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16000}


def test_build_payload_without_budget_or_adaptive_thinking_omits_visible_thinking() -> None:
    model_id = "claude-haiku-4.5-runtime-metadata"
    policy = _messages_policy(
        model_id=model_id,
        reasoning_efforts=[],
        adaptive_thinking=False,
        min_thinking_budget=None,
        max_thinking_budget=None,
    )

    payload = build_copilot_messages_payload(
        [{"role": "user", "content": "Think."}],
        model_id=model_id,
        policy=policy,
        thinking_effort="high",
        temperature=0.25,
    )

    assert payload == {
        "model": model_id,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Think."}]}],
        "max_tokens": 8192,
        "temperature": 0.25,
    }


def test_normalize_response_extracts_text_thinking_meta_tool_calls_and_usage() -> None:
    normalized = normalize_copilot_messages_response(
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
        }
    )

    assert normalized == {
        "role": "assistant",
        "content": "Use this.",
        "reasoning": "I should inspect.",
        "reasoning_meta": {
            "content_blocks": [
                {"type": "thinking", "thinking": "I should inspect.", "signature": "sig-1"},
                {"type": "redacted_thinking", "data": "opaque-redacted"},
            ]
        },
        "tool_calls": [{"id": "toolu_1", "name": "read", "arguments": {"path": "README.md"}}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def test_normalize_response_folds_cache_tokens_into_input_tokens() -> None:
    normalized = normalize_copilot_messages_response(
        {
            "content": [{"type": "text", "text": "Use this."}],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 50,
            },
        }
    )

    assert normalized["usage"] == {
        "input_tokens": 560,
        "output_tokens": 20,
        "cache_read_tokens": 500,
        "cache_write_tokens": 50,
    }


def test_stream_usage_delta_folds_cache_tokens_from_message_start() -> None:
    state = CopilotMessagesStreamState()

    events: list[dict[str, Any]] = [
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
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 11},
        },
    ]

    deltas = []
    for event in events:
        deltas.extend(normalize_copilot_messages_stream_event(event, state))

    assert deltas == [
        {"type": "finish", "reason": "stop"},
        {
            "type": "usage",
            "input_tokens": 347,
            "output_tokens": 11,
            "cache_read_tokens": 300,
            "cache_write_tokens": 40,
        },
    ]


def test_normalize_response_extracts_visible_thinking_text_block() -> None:
    normalized = normalize_copilot_messages_response(
        {
            "content": [
                {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                {"type": "text", "text": "Done."},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )

    assert normalized == {
        "role": "assistant",
        "content": "Done.",
        "reasoning": "Need to inspect first.",
        "reasoning_meta": {
            "content_blocks": [
                {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"}
            ]
        },
        "tool_calls": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def test_stream_normalizes_text_thinking_signature_tool_usage_and_finish() -> None:
    state = CopilotMessagesStreamState()

    events: list[dict[str, Any]] = [
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
    ]

    deltas = []
    for event in events:
        deltas.extend(normalize_copilot_messages_stream_event(event, state))

    assert deltas == [
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
        {"type": "finish", "reason": "tool_calls"},
        {"type": "usage", "input_tokens": 7, "output_tokens": 11},
    ]


def test_stream_normalizes_visible_thinking_text_delta_variant() -> None:
    state = CopilotMessagesStreamState()

    events: list[dict[str, Any]] = [
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
    ]

    deltas = []
    for event in events:
        deltas.extend(normalize_copilot_messages_stream_event(event, state))

    assert deltas == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "content_blocks": [
                    {"type": "thinking", "text": "Need docs lookup.", "signature": "sig-stream"}
                ]
            },
        },
    ]


def test_stream_normalizes_tool_use_stop_reason_to_tool_calls_finish() -> None:
    state = CopilotMessagesStreamState()

    events: list[dict[str, Any]] = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
        },
    ]

    deltas = []
    for event in events:
        deltas.extend(normalize_copilot_messages_stream_event(event, state))

    assert deltas == [
        {
            "type": "tool_call_delta",
            "id": "toolu_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


def test_stream_falls_back_to_tool_calls_finish_when_tool_use_block_is_present() -> None:
    state = CopilotMessagesStreamState()

    events: list[dict[str, Any]] = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "copilot_tool_stop"},
        },
    ]

    deltas = []
    for event in events:
        deltas.extend(normalize_copilot_messages_stream_event(event, state))

    assert deltas == [
        {
            "type": "tool_call_delta",
            "id": "toolu_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


def test_stream_preserves_redacted_thinking_block() -> None:
    state = CopilotMessagesStreamState()

    deltas = normalize_copilot_messages_stream_event(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "redacted_thinking", "data": "opaque"},
        },
        state,
    )
    deltas.extend(
        normalize_copilot_messages_stream_event(
            {"type": "content_block_stop", "index": 0},
            state,
        )
    )

    assert deltas == [
        {
            "type": "reasoning_meta",
            "reasoning_meta": {"content_blocks": [{"type": "redacted_thinking", "data": "opaque"}]},
        }
    ]


def test_stream_error_event_raises_provider_error() -> None:
    with pytest.raises(ProviderError, match="overloaded"):
        normalize_copilot_messages_stream_event(
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "overloaded"},
            },
            CopilotMessagesStreamState(),
        )


def test_build_payload_translates_user_image_media_block() -> None:
    policy = _messages_policy()
    payload = build_copilot_messages_payload(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "media", "media_type": "image/png", "base64": "aW1n"},
                ],
            }
        ],
        model_id="claude-sonnet-4.6",
        policy=policy,
    )

    assert payload["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "aW1n",
                    },
                },
            ],
        }
    ]


def test_build_payload_rejects_non_image_media_block() -> None:
    policy = _messages_policy()
    with pytest.raises(ProviderError, match="only image media blocks"):
        build_copilot_messages_payload(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "media", "media_type": "audio/wav", "base64": "YXVkaW8="}
                    ],
                }
            ],
            model_id="claude-sonnet-4.6",
            policy=policy,
        )


def test_build_payload_rejects_media_block_missing_fields() -> None:
    policy = _messages_policy()
    with pytest.raises(ProviderError, match="requires string base64 and media_type"):
        build_copilot_messages_payload(
            [{"role": "user", "content": [{"type": "media", "media_type": "image/png"}]}],
            model_id="claude-sonnet-4.6",
            policy=policy,
        )
