"""Tests for GitHub Copilot Responses protocol helpers."""

from __future__ import annotations

import json

import pytest

from core.providers.errors import ProviderError
from core.providers.github_copilot_policy import RESPONSES_ENDPOINT, copilot_model_policy
from core.providers.github_copilot_responses import (
    build_responses_payload,
    iter_responses_sse_deltas,
    normalize_responses_response,
)


def responses_policy(model_id: str = "gpt-5.4", **overrides):
    metadata = {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": model_id,
            "version": model_id,
            "supported_endpoints": [RESPONSES_ENDPOINT],
            "reasoning_efforts": ["low", "medium", "high", "xhigh"],
            "tool_calls": True,
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
        }
    }
    metadata["github_copilot"].update(overrides)
    return copilot_model_policy(model_id, metadata)


def test_build_payload_extracts_system_instructions_and_user_input() -> None:
    payload = build_responses_payload(
        [
            {"role": "system", "content": "Use concise answers."},
            {"role": "user", "content": "Hello"},
        ],
        model_id="gpt-5.4",
        policy=responses_policy(),
    )

    assert payload["model"] == "gpt-5.4"
    assert payload["instructions"] == "Use concise answers."
    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}
    ]


def test_build_payload_gates_reasoning_tools_and_structured_output() -> None:
    policy = responses_policy(reasoning_efforts=["low"], structured_outputs=False)

    payload = build_responses_payload(
        [{"role": "user", "content": "Return JSON"}],
        model_id="gpt-5.4",
        policy=policy,
        thinking_effort="xhigh",
        tools=[{"name": "search", "description": "Search", "parameters": {"type": "object"}}],
        tool_choice="auto",
        response_format={"type": "json_object"},
    )

    assert "reasoning" not in payload
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "search",
            "description": "Search",
            "parameters": {"type": "object"},
        }
    ]
    assert payload["tool_choice"] == "auto"
    assert "text" not in payload


def test_build_payload_includes_allowed_reasoning_encrypted_content_request() -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Think"}],
        model_id="gpt-5.4",
        policy=responses_policy(),
        thinking_effort="xhigh",
    )

    assert payload["reasoning"] == {"effort": "xhigh"}
    assert payload["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5-mini"])
def test_build_payload_omits_temperature_for_gpt5_responses_models(model_id: str) -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Hello"}],
        model_id=model_id,
        policy=responses_policy(model_id),
        include=["unsupported.trace", "reasoning.encrypted_content"],
        cache_control={"type": "ephemeral"},
        prompt_cache_key="cache-key",
        prompt_cache_retention="24h",
        unknown_extra="do-not-forward",
        temperature=0.2,
        top_p=0.9,
        max_tokens=512,
        parallel_tool_calls=True,
    )

    assert "include" not in payload
    assert "cache_control" not in payload
    assert "prompt_cache_key" not in payload
    assert "prompt_cache_retention" not in payload
    assert "unknown_extra" not in payload
    assert "temperature" not in payload
    assert payload["top_p"] == 0.9
    assert payload["max_output_tokens"] == 512
    assert payload["parallel_tool_calls"] is True


def test_build_payload_prefers_explicit_max_output_tokens_over_max_tokens() -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="gpt-5.4",
        policy=responses_policy(),
        max_tokens=512,
        max_output_tokens=1024,
    )

    assert payload["max_output_tokens"] == 1024


def test_build_payload_omits_tools_when_policy_disallows_tools() -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="gpt-5.5",
        policy=responses_policy(tool_calls=False),
        tools=[{"name": "search", "description": "Search", "parameters": {}}],
        tool_choice="auto",
    )

    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_build_payload_replays_tool_calls_tool_results_and_reasoning_meta() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "opaque",
    }

    payload = build_responses_payload(
        [
            {
                "role": "assistant",
                "content": "I will call a tool.",
                "reasoning_meta": {"reasoning_items": [reasoning_item]},
                "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "result"},
        ],
        model_id="gpt-5.4",
        policy=responses_policy(),
    )

    assert payload["input"] == [
        reasoning_item,
        {"role": "assistant", "content": [{"type": "output_text", "text": "I will call a tool."}]},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


def test_normalize_response_extracts_text_tool_calls_usage_and_reasoning_meta() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Considered evidence."}],
        "encrypted_content": "opaque",
    }
    response = {
        "id": "resp_1",
        "output": [
            reasoning_item,
            {"type": "message", "content": [{"type": "output_text", "text": "Done."}]},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"q":"docs"}',
            },
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }

    normalized = normalize_responses_response(response)

    assert normalized == {
        "role": "assistant",
        "content": "Done.",
        "reasoning": "Considered evidence.",
        "reasoning_meta": {
            "response_id": "resp_1",
            "reasoning_items": [reasoning_item],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def test_normalize_response_uses_empty_arguments_for_malformed_function_json() -> None:
    normalized = normalize_responses_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search",
                    "arguments": "{not json",
                }
            ]
        }
    )

    assert normalized["tool_calls"] == [{"id": "call_1", "name": "search", "arguments": {}}]


def test_stream_normalizes_text_reasoning_tool_usage_and_finish() -> None:
    lines = [
        _sse("response.output_text.delta", {"delta": "Hel"}),
        _sse("response.reasoning_summary_text.delta", {"delta": "Thinking"}),
        _sse(
            "response.output_item.added",
            {"item": {"type": "function_call", "call_id": "call_1", "name": "search"}},
        ),
        _sse("response.function_call_arguments.delta", {"item_id": "call_1", "delta": '{"q"'}),
        _sse("response.function_call_arguments.delta", {"item_id": "call_1", "delta": ':"docs"}'}),
        _sse(
            "response.output_item.done",
            {"item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}},
        ),
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                }
            },
        ),
        "data: [DONE]\n\n",
    ]

    assert list(iter_responses_sse_deltas(lines)) == [
        {"type": "content_delta", "text": "Hel"},
        {"type": "reasoning_delta", "text": "Thinking"},
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": '{"q"',
        },
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": ':"docs"}',
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "reasoning_items": [
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                ]
            },
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp_1",
                "reasoning_items": [
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                ],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "usage", "input_tokens": 5, "output_tokens": 3},
        {"type": "finish", "reason": "stop"},
    ]


def test_stream_tolerates_unknown_events() -> None:
    lines = [_sse("response.unrecognized", {"type": "response.unrecognized", "value": 1})]

    assert list(iter_responses_sse_deltas(lines)) == []


def test_stream_raises_provider_error_for_error_events() -> None:
    lines = [_sse("response.failed", {"error": {"message": "bad request"}})]

    with pytest.raises(ProviderError, match="bad request"):
        list(iter_responses_sse_deltas(lines))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
