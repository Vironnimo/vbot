"""Github copilot responses: normalization behavior."""

from __future__ import annotations

import json

import pytest

from core.providers.errors import (
    ProviderError,
)
from core.providers.github_copilot_responses import (
    estimate_responses_input_tokens,
    normalize_responses_response,
)
from tests.core.providers.github_copilot_responses_helpers import (
    _iter_deltas,
)


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
            "response_output": response["output"],
            "reasoning_items": [reasoning_item],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def test_normalize_response_preserves_reported_token_details() -> None:
    response = {
        "id": "resp_1",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Done."}]}],
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "input_tokens_details": {
                "cached_tokens": 8,
                "cache_write_tokens": 2,
            },
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }

    normalized = normalize_responses_response(response)

    assert normalized["usage"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 8,
        "cache_write_tokens": 2,
        "reasoning_tokens": 5,
    }


def test_normalize_response_ignores_non_int_cached_tokens() -> None:
    response = {
        "id": "resp_1",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Done."}]}],
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "input_tokens_details": {"cached_tokens": None},
        },
    }

    normalized = normalize_responses_response(response)

    assert normalized["usage"] == {"input_tokens": 11, "output_tokens": 7}


def test_normalize_response_preserves_malformed_function_arguments_as_rejected_call() -> None:
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

    assert normalized["tool_calls"] is not None
    tool_call = normalized["tool_calls"][0]
    assert tool_call["id"] == "call_1"
    assert tool_call["name"] == "search"
    assert tool_call["arguments"] == {}
    assert tool_call["rejection"]["code"] == "malformed_tool_arguments"


def test_normalize_response_recovers_consecutive_function_argument_objects() -> None:
    normalized = normalize_responses_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_batch",
                    "name": "bash",
                    "arguments": '{"command":"echo one"}{"command":"echo two"}',
                }
            ]
        }
    )

    assert normalized["tool_calls"] is not None
    assert [call["arguments"]["command"] for call in normalized["tool_calls"]] == [
        "echo one",
        "echo two",
    ]
    assert [call["argument_sequence_index"] for call in normalized["tool_calls"]] == [0, 1]


def test_normalize_response_accepts_collapsed_single_output_item() -> None:
    normalized = normalize_responses_response(
        {
            "output": {
                "type": "function_call",
                "call_id": "call_one",
                "name": "search",
                "arguments": '{"q":"docs"}',
            }
        }
    )

    assert normalized["tool_calls"] == [
        {"id": "call_one", "name": "search", "arguments": {"q": "docs"}}
    ]


def test_normalize_response_keeps_valid_sibling_when_one_function_arguments_json_is_malformed() -> (
    None
):
    normalized = normalize_responses_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_bad",
                    "name": "search",
                    "arguments": "{not json",
                },
                {
                    "type": "function_call",
                    "call_id": "call_ok",
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                },
            ]
        }
    )

    assert normalized["tool_calls"] is not None
    assert [call["id"] for call in normalized["tool_calls"]] == ["call_bad", "call_ok"]
    assert normalized["tool_calls"][0]["arguments"] == {}
    assert normalized["tool_calls"][0]["rejection"]["code"] == "malformed_tool_arguments"
    assert normalized["tool_calls"][1] == {
        "id": "call_ok",
        "name": "read_file",
        "arguments": {"path": "README.md"},
    }


def test_normalize_response_extracts_nested_function_call_name_and_visible_reasoning() -> None:
    response = {
        "id": "resp_1",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                "encrypted_content": "opaque",
            },
            {"type": "message", "content": [{"type": "output_text", "text": "Calling tool."}]},
            {
                "type": "function_call",
                "call_id": "call_1",
                "function": {
                    "name": "search",
                    "arguments": '{"q":"docs"}',
                },
            },
        ],
    }

    normalized = normalize_responses_response(response)

    assert normalized == {
        "role": "assistant",
        "content": "Calling tool.",
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {
            "response_id": "resp_1",
            "response_output": response["output"],
            "reasoning_items": [response["output"][0]],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
    }


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_normalize_response_prefers_nested_function_arguments_when_top_level_values_are_blank(
    model_id: str,
) -> None:
    normalized = normalize_responses_response(
        {
            "id": f"resp_{model_id}",
            "output": [
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
            ],
        }
    )

    assert normalized["tool_calls"] == [
        {"id": "call_1", "name": "search", "arguments": {"q": "docs"}}
    ]


def test_normalize_response_prefers_nested_function_name_over_top_level_placeholder() -> None:
    normalized = normalize_responses_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "tool",
                    "arguments": "",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"pwd"}',
                    },
                }
            ]
        }
    )

    assert normalized["tool_calls"] == [
        {"id": "call_1", "name": "bash", "arguments": {"command": "pwd"}}
    ]


def test_normalize_response_extracts_reasoning_text_from_reasoning_content_blocks() -> None:
    response = {
        "id": "resp_1",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "content": [{"type": "reasoning_text", "text": "Need docs lookup."}],
                "encrypted_content": "opaque",
            }
        ],
    }

    normalized = normalize_responses_response(response)

    assert normalized["reasoning"] == "Need docs lookup."
    assert normalized["reasoning_meta"] == {
        "response_id": "resp_1",
        "response_output": response["output"],
        "reasoning_items": [response["output"][0]],
        "encrypted_content": ["opaque"],
    }


@pytest.mark.parametrize(
    "primary,other", [("input_tokens", "output_tokens"), ("output_tokens", "input_tokens")]
)
@pytest.mark.parametrize("invalid", [None, "12", 1.5, True, False, -1])
def test_responses_usage_preserves_only_valid_primary_fields(primary, other, invalid) -> None:
    from core.chat.streaming import StreamingAccumulator

    usage = {primary: 17, other: invalid, "input_tokens_details": {"cached_tokens": 4}}
    response = {"status": "completed", "output": [], "usage": usage}
    expected = {primary: 17, "cache_read_tokens": 4}
    assert normalize_responses_response(response)["usage"] == expected
    accumulator = StreamingAccumulator()
    for delta in _iter_deltas(
        [f"data: {json.dumps({'type': 'response.completed', 'response': response})}"]
    ):
        accumulator.add_delta(delta)
    assert accumulator.finalize_assistant_fields().usage == expected


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"input_tokens": 0}, {"input_tokens": 0}),
        ({"output_tokens": 0}, {"output_tokens": 0}),
        ({"prompt_tokens": 12}, {"input_tokens": 12}),
        ({"completion_tokens": 7}, {"output_tokens": 7}),
        ({"total_tokens": 12}, None),
        ({"input_tokens": True, "output_tokens": -1}, None),
        ({"input_tokens_details": {"cached_tokens": 2}}, None),
    ],
)
def test_responses_usage_missing_counters_and_real_zero(usage, expected) -> None:
    assert normalize_responses_response({"usage": usage}).get("usage") == expected


@pytest.mark.parametrize("frame", ['{"test_frame":', '["test_frame"]'])
def test_responses_bad_frames_preserve_evidence_without_foreign_provider(frame) -> None:
    with pytest.raises(ProviderError) as caught:
        list(_iter_deltas([f"data: {frame}"]))
    assert caught.value.retryable is False
    assert frame in str(caught.value)
    assert "GitHub Copilot" not in str(caught.value)


def test_responses_image_estimate_receives_active_model():
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": (
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
                        "QVR42mP8/x8AAwMCAO+aXfcAAAAASUVORK5CYII="
                    ),
                }
            ],
        }
    ]
    known = estimate_responses_input_tokens(messages, model_id="gpt-4o")
    fallback = estimate_responses_input_tokens(messages, model_id="unknown")
    assert fallback - known == 4096 - 255
