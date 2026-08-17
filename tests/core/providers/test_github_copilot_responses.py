"""Tests for GitHub Copilot Responses protocol helpers."""

from __future__ import annotations

import json

import pytest

from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.github_copilot_policy import RESPONSES_ENDPOINT, copilot_model_policy
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    build_responses_payload,
    iter_responses_sse_deltas_with_state,
    normalize_responses_response,
)
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS


def _iter_deltas(lines):
    """Parse Responses SSE lines with a fresh stream state (test convenience)."""
    return iter_responses_sse_deltas_with_state(lines, ResponsesStreamState())


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


def test_build_payload_maps_reasoning_and_gates_tools_and_structured_output() -> None:
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

    assert payload["reasoning"] == {"effort": "low", "summary": "auto"}
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "search",
            "description": "Search",
            "parameters": {"type": "object"},
            "strict": False,
        }
    ]
    assert payload["tool_choice"] == "auto"
    assert "text" not in payload


def test_build_payload_maps_history_tool_without_special_case() -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Verify prior context"}],
        model_id="gpt-5.4",
        policy=responses_policy(),
        tools=[
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "parameters": HISTORY_TOOL_PARAMETERS,
            }
        ],
    )

    assert payload["tools"] == [
        {
            "type": "function",
            "name": HISTORY_TOOL_NAME,
            "description": HISTORY_TOOL_DESCRIPTION,
            "parameters": HISTORY_TOOL_PARAMETERS,
            "strict": False,
        }
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_build_payload_prefers_nested_function_tool_definition_when_top_level_name_is_blank(
    model_id: str,
) -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Search docs"}],
        model_id=model_id,
        policy=responses_policy(model_id),
        tools=[
            {
                "type": "function",
                "name": "",
                "function": {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            }
        ],
    )

    assert payload["tools"] == [
        {
            "type": "function",
            "name": "search",
            "description": "Search docs",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            "strict": False,
        }
    ]


def test_build_payload_includes_allowed_reasoning_encrypted_content_request() -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Think"}],
        model_id="gpt-5.4",
        policy=responses_policy(),
        thinking_effort="xhigh",
    )

    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_build_payload_requests_encrypted_content_even_without_effort_object() -> None:
    """Always-on / default-effort Models still need encrypted continuity bytes."""

    payload = build_responses_payload(
        [{"role": "user", "content": "Continue"}],
        model_id="gpt-5.4",
        policy=responses_policy(),
    )

    assert "reasoning" not in payload
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

    # Caller-supplied include is ignored; reasoning-capable Models always get the
    # encrypted continuity request, never arbitrary include values.
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "cache_control" not in payload
    assert "prompt_cache_key" not in payload
    assert "prompt_cache_retention" not in payload
    assert "unknown_extra" not in payload
    assert "temperature" not in payload
    assert payload["top_p"] == 0.9
    assert payload["max_output_tokens"] == 512
    assert payload["parallel_tool_calls"] is True


def test_build_payload_omits_temperature_for_partial_openai_like_metadata() -> None:
    partial_policy = responses_policy(reasoning_efforts=[])

    payload = build_responses_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="gpt-5.4",
        policy=partial_policy,
        temperature=0.2,
        top_p=0.9,
        max_tokens=512,
    )

    assert "temperature" not in payload
    assert payload["top_p"] == 0.9
    assert payload["max_output_tokens"] == 512


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


def test_build_payload_renders_image_inside_function_call_output() -> None:
    payload = build_responses_payload(
        [
            {
                "role": "tool",
                "tool_call_id": "call_image",
                "content": '{"ok":true}',
                TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
                    {
                        "type": "media",
                        "base64": "aW1hZ2U=",
                        "media_type": "image/png",
                    },
                    {"type": "text", "text": "[Image path: C:/diagram.png]"},
                ],
            }
        ],
        model_id="gpt-5.4",
        policy=responses_policy(),
    )

    assert payload["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_image",
            "output": [
                {"type": "input_text", "text": '{"ok":true}'},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,aW1hZ2U=",
                },
                {"type": "input_text", "text": "[Image path: C:/diagram.png]"},
            ],
        }
    ]


def test_build_payload_replays_complete_response_output_without_reconstruction() -> None:
    response_output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "I will inspect this."}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
    ]

    payload = build_responses_payload(
        [
            {
                "role": "assistant",
                "content": "A reconstructed copy that must not be used.",
                "phase": "final_answer",
                "reasoning_meta": {"response_output": response_output},
                "tool_calls": [{"id": "duplicate", "name": "search", "arguments": {}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "result"},
        ],
        model_id="gpt-5.6-sol",
        policy=responses_policy("gpt-5.6-sol"),
    )

    assert payload["input"] == [
        *response_output,
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


def test_build_payload_replaces_rejected_raw_function_call_with_safe_canonical_item() -> None:
    response_output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": "fc_bad",
            "call_id": "call_bad",
            "name": "write",
            "arguments": '{"path":"README.md"',
        },
    ]
    failure = '{"ok":false,"error":{"code":"malformed_tool_arguments"}}'

    payload = build_responses_payload(
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_meta": {"response_output": response_output},
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "name": "write",
                        "arguments": {},
                        "rejection": {
                            "code": "malformed_tool_arguments",
                            "message": "Arguments were malformed.",
                            "fingerprint": "sha256",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_bad",
                "name": "write",
                "content": failure,
            },
        ],
        model_id="gpt-5.6-sol",
        policy=responses_policy("gpt-5.6-sol"),
    )

    assert payload["input"] == [
        response_output[0],
        {
            "type": "function_call",
            "call_id": "call_bad",
            "name": "write",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call_bad", "output": failure},
    ]


def test_build_payload_expands_recovered_argument_sequence_for_replay() -> None:
    response_output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": "fc_batch",
            "call_id": "call_batch",
            "name": "bash",
            "arguments": '{"command":"echo one"}{"command":"echo two"}',
        },
    ]
    payload = build_responses_payload(
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_meta": {"response_output": response_output},
                "tool_calls": [
                    {
                        "id": "call_batch",
                        "name": "bash",
                        "arguments": {"command": "echo one"},
                        "argument_sequence_index": 0,
                        "argument_sequence_length": 2,
                    },
                    {
                        "id": "tool_call_recovered_1234",
                        "name": "bash",
                        "arguments": {"command": "echo two"},
                        "argument_sequence_index": 1,
                        "argument_sequence_length": 2,
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_batch",
                "name": "bash",
                "content": "first",
            },
            {
                "role": "tool",
                "tool_call_id": "tool_call_recovered_1234",
                "name": "bash",
                "content": "second",
            },
        ],
        model_id="gpt-5.6-sol",
        policy=responses_policy("gpt-5.6-sol"),
    )

    assert payload["input"] == [
        response_output[0],
        {
            "type": "function_call",
            "call_id": "call_batch",
            "name": "bash",
            "arguments": '{"command":"echo one"}',
        },
        {
            "type": "function_call",
            "call_id": "tool_call_recovered_1234",
            "name": "bash",
            "arguments": '{"command":"echo two"}',
        },
        {"type": "function_call_output", "call_id": "call_batch", "output": "first"},
        {
            "type": "function_call_output",
            "call_id": "tool_call_recovered_1234",
            "output": "second",
        },
    ]


def test_build_payload_replays_phase_from_canonical_fallback_message() -> None:
    payload = build_responses_payload(
        [
            {
                "role": "assistant",
                "content": "Intermediate update.",
                "phase": "commentary",
            }
        ],
        model_id="gpt-5.5",
        policy=responses_policy("gpt-5.5"),
    )

    assert payload["input"] == [
        {
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Intermediate update."}],
        }
    ]


def test_build_payload_maps_allowed_pdf_document_to_input_file() -> None:
    payload = build_responses_payload(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "filename": "report.pdf",
                        "media_type": "application/pdf",
                        "base64": "cGRm",
                    }
                ],
            }
        ],
        model_id="gpt-5.6-sol",
        policy=responses_policy("gpt-5.6-sol"),
        document_media_types=frozenset({"application/pdf"}),
    )

    assert payload["input"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "filename": "report.pdf",
                    "file_data": "data:application/pdf;base64,cGRm",
                }
            ],
        }
    ]


def test_build_payload_replays_nested_function_tool_call_name_shape() -> None:
    payload = build_responses_payload(
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
            }
        ],
        model_id="gpt-5.4",
        policy=responses_policy(),
    )

    assert payload["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        }
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_build_payload_replays_nested_function_arguments_when_top_level_arguments_are_blank(
    model_id: str,
) -> None:
    payload = build_responses_payload(
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
            }
        ],
        model_id=model_id,
        policy=responses_policy(model_id),
    )

    assert payload["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        }
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_build_payload_preserves_nested_function_tool_call_name_shape_for_gpt_5_4_family(
    model_id: str,
) -> None:
    payload = build_responses_payload(
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
            }
        ],
        model_id=model_id,
        policy=responses_policy(model_id),
    )

    assert payload["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        }
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
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 3,
                        "input_tokens_details": {
                            "cached_tokens": 2,
                            "cache_write_tokens": 1,
                        },
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                }
            },
        ),
        "data: [DONE]\n\n",
    ]

    assert list(_iter_deltas(lines)) == [
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
                "response_output": [
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                ],
                "reasoning_items": [
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                ],
                "encrypted_content": ["opaque"],
            },
        },
        {
            "type": "usage",
            "input_tokens": 5,
            "output_tokens": 3,
            "cache_read_tokens": 2,
            "cache_write_tokens": 1,
            "reasoning_tokens": 2,
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


def test_stream_accepts_openrouter_reasoning_delta_event() -> None:
    lines = [_sse("response.reasoning.delta", {"delta": "Thinking"})]

    assert list(_iter_deltas(lines)) == [{"type": "reasoning_delta", "text": "Thinking"}]


def test_stream_emits_tool_name_from_nested_function_call_item() -> None:
    lines = [
        _sse(
            "response.output_item.added",
            {
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "function": {"name": "search"},
                }
            },
        )
    ]

    assert list(_iter_deltas(lines)) == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "search",
            "arguments_delta": "",
        }
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_stream_deduplicates_replayed_argument_delta_when_item_id_differs(model_id: str) -> None:
    lines = [
        _sse(
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "",
                    "arguments": "",
                    "function": {
                        "name": "search",
                        "arguments": '{"q":"docs"}',
                    },
                },
            },
        ),
        _sse(
            "response.function_call_arguments.delta",
            {
                "output_index": 0,
                "item_id": "fc_1",
                "call_id": "call_1",
                "delta": '{"q":"docs"}',
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "search",
            "arguments_delta": '{"q":"docs"}',
        },
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_stream_item_id_only_delta_resolves_to_call_id_canonical_slot(model_id: str) -> None:
    lines = [
        _sse(
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "",
                    "arguments": "",
                    "function": {
                        "name": "search",
                    },
                },
            },
        ),
        _sse(
            "response.function_call_arguments.delta",
            {
                "item_id": "fc_1",
                "delta": '{"q":"docs"}',
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
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
            "arguments_delta": '{"q":"docs"}',
        },
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_stream_combined_placeholder_name_and_item_id_only_delta_emits_single_canonical_tool_call(
    model_id: str,
) -> None:
    lines = [
        _sse(
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "tool",
                    "arguments": "",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"pwd"}',
                    },
                },
            },
        ),
        _sse(
            "response.function_call_arguments.delta",
            {
                "item_id": "fc_1",
                "delta": '{"command":"pwd"}',
            },
        ),
        _sse(
            "response.completed",
            {
                "response": {
                    "id": f"resp_{model_id}",
                    "status": "completed",
                    "output": [],
                }
            },
        ),
    ]

    deltas = list(_iter_deltas(lines))
    tool_call_deltas = [delta for delta in deltas if delta.get("type") == "tool_call_delta"]

    assert tool_call_deltas == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "bash",
            "arguments_delta": '{"command":"pwd"}',
        }
    ]
    assert deltas[-1] == {"type": "finish", "reason": "tool_calls"}


def test_stream_backfills_only_missing_argument_suffix_after_added_item() -> None:
    lines = [
        _sse(
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"q"',
                    },
                },
            },
        ),
        _sse(
            "response.function_call_arguments.delta",
            {
                "output_index": 0,
                "call_id": "call_1",
                "delta": '{"q":"docs"}',
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "search",
            "arguments_delta": '{"q"',
        },
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": ':"docs"}',
        },
    ]


def test_stream_preserves_repeated_tool_argument_boundary_text() -> None:
    lines = [
        _sse(
            "response.function_call_arguments.delta",
            {
                "call_id": "call_1",
                "delta": '{"value":"ab',
            },
        ),
        _sse(
            "response.function_call_arguments.delta",
            {
                "call_id": "call_1",
                "delta": 'ab"}',
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": '{"value":"ab',
        },
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": 'ab"}',
        },
    ]


def test_stream_preserves_tool_argument_delta_that_appears_elsewhere_in_payload() -> None:
    lines = [
        _sse(
            "response.function_call_arguments.delta",
            {
                "call_id": "call_1",
                "delta": '{"pattern":"abc","value":"',
            },
        ),
        _sse(
            "response.function_call_arguments.delta",
            {
                "call_id": "call_1",
                "delta": "abc",
            },
        ),
        _sse(
            "response.function_call_arguments.delta",
            {
                "call_id": "call_1",
                "delta": '"}',
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": '{"pattern":"abc","value":"',
        },
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": "abc",
        },
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "",
            "arguments_delta": '"}',
        },
    ]


def test_stream_emits_visible_reasoning_from_completed_response_output() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
        "encrypted_content": "opaque",
    }
    lines = [
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [reasoning_item],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                }
            },
        )
    ]

    assert list(_iter_deltas(lines)) == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp_1",
                "response_output": [reasoning_item],
                "reasoning_items": [reasoning_item],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "usage", "input_tokens": 5, "output_tokens": 3},
        {"type": "finish", "reason": "stop"},
    ]


def test_stream_emits_visible_reasoning_from_reasoning_output_item() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
        "encrypted_content": "opaque",
    }
    lines = [
        _sse("response.output_item.done", {"item": reasoning_item}),
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [reasoning_item],
                }
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {"reasoning_items": [reasoning_item]},
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp_1",
                "response_output": [reasoning_item],
                "reasoning_items": [reasoning_item],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


def test_stream_replaces_same_item_without_output_index_before_empty_completion() -> None:
    partial_item = {"type": "message", "id": "msg_1", "role": "assistant", "content": []}
    completed_item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "Done"}],
    }
    lines = [
        _sse("response.output_item.added", {"item": partial_item}),
        _sse("response.output_item.done", {"item": completed_item}),
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [],
                }
            },
        ),
    ]

    deltas = list(_iter_deltas(lines))

    assert deltas[-2] == {
        "type": "reasoning_meta",
        "reasoning_meta": {
            "response_id": "resp_1",
            "response_output": [completed_item],
        },
    }
    assert deltas[-1] == {"type": "finish", "reason": "stop"}


def test_stream_completed_event_prefers_tool_calls_finish_over_completed_status() -> None:
    lines = [
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_tool",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "function": {"name": "search", "arguments": '{"q":"docs"}'},
                        }
                    ],
                }
            },
        )
    ]

    assert list(_iter_deltas(lines)) == [
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp_tool",
                "response_output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "function": {"name": "search", "arguments": '{"q":"docs"}'},
                    }
                ],
            },
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
def test_responses_policy_variants_cover_same_nested_tool_name_and_visible_reasoning_paths(
    model_id: str,
) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
        "encrypted_content": "opaque",
    }

    normalized = normalize_responses_response(
        {
            "id": "resp_1",
            "output": [
                reasoning_item,
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
    )
    streamed = list(
        _iter_deltas(
            [
                _sse(
                    "response.output_item.added",
                    {
                        "item": {
                            "type": "function_call",
                            "call_id": "call_1",
                            "function": {"name": "search"},
                        }
                    },
                ),
                _sse(
                    "response.completed",
                    {
                        "response": {
                            "id": f"resp_{model_id}",
                            "status": "completed",
                            "output": [reasoning_item],
                        }
                    },
                ),
            ]
        )
    )

    assert responses_policy(model_id).endpoint_path == RESPONSES_ENDPOINT
    assert normalized["tool_calls"] == [
        {"id": "call_1", "name": "search", "arguments": {"q": "docs"}}
    ]
    assert normalized["reasoning"] == "Need docs lookup."
    assert streamed == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": f"resp_{model_id}",
                "response_output": [reasoning_item],
                "reasoning_items": [reasoning_item],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


def test_stream_does_not_duplicate_reasoning_when_completed_repeats_streamed_text() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
        "encrypted_content": "opaque",
    }
    lines = [
        _sse("response.reasoning_summary_text.delta", {"delta": "Need docs lookup."}),
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [reasoning_item],
                }
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp_1",
                "response_output": [reasoning_item],
                "reasoning_items": [reasoning_item],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


def test_stream_backfills_only_missing_reasoning_suffix_from_completed_response() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Need docs"}],
        "content": [{"type": "reasoning_text", "text": " lookup."}],
        "encrypted_content": "opaque",
    }
    lines = [
        _sse("response.reasoning_summary_text.delta", {"delta": "Need docs"}),
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [reasoning_item],
                }
            },
        ),
    ]

    assert list(_iter_deltas(lines)) == [
        {"type": "reasoning_delta", "text": "Need docs"},
        {"type": "reasoning_delta", "text": " lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp_1",
                "response_output": [reasoning_item],
                "reasoning_items": [reasoning_item],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


def test_stream_tolerates_unknown_events() -> None:
    lines = [_sse("response.unrecognized", {"type": "response.unrecognized", "value": 1})]

    assert list(_iter_deltas(lines)) == []


def test_stream_raises_provider_error_for_error_events() -> None:
    lines = [_sse("response.failed", {"error": {"message": "bad request"}})]

    with pytest.raises(ProviderError, match="bad request"):
        list(_iter_deltas(lines))


@pytest.mark.parametrize(
    ("event_name", "event_data", "expected_type", "retryable"),
    [
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "server_error", "message": "OpenAI failed."},
                },
            },
            ProviderError,
            True,
            id="openai-platform-transient-server",
        ),
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "invalid_prompt", "message": "Invalid prompt."},
                },
            },
            ProviderError,
            False,
            id="openai-platform-fatal-prompt",
        ),
        pytest.param(
            "error",
            {
                "type": "error",
                "code": "rate_limit_exceeded",
                "message": "Codex rate limit.",
            },
            ProviderRateLimitError,
            True,
            id="openai-codex-transient-rate-limit",
        ),
        pytest.param(
            "error",
            {
                "type": "error",
                "code": "invalid_api_key",
                "message": "Codex authentication failed.",
            },
            ProviderAuthError,
            False,
            id="openai-codex-fatal-auth",
        ),
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "server_error", "message": "Provider overloaded."},
                    "error_type": "provider_overloaded",
                },
            },
            ProviderError,
            True,
            id="openrouter-transient-overload",
        ),
        pytest.param(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"code": "server_error", "message": "Authentication failed."},
                    "error_type": "authentication",
                },
            },
            ProviderAuthError,
            False,
            id="openrouter-fatal-auth-overrides-native-code",
        ),
        pytest.param(
            "response.error",
            {
                "type": "response.error",
                "error": {"code": "timeout", "message": "Copilot timed out."},
            },
            ProviderTimeoutError,
            True,
            id="github-copilot-transient-timeout",
        ),
        pytest.param(
            "response.error",
            {
                "type": "response.error",
                "error": {"code": "invalid_request", "message": "Invalid request."},
            },
            ProviderError,
            False,
            id="github-copilot-fatal-request",
        ),
    ],
)
def test_stream_classifies_route_specific_error_frames(
    event_name,
    event_data,
    expected_type,
    retryable,
) -> None:
    with pytest.raises(ProviderError) as exc_info:
        list(_iter_deltas([_sse(event_name, event_data)]))

    assert type(exc_info.value) is expected_type
    assert exc_info.value.retryable is retryable


@pytest.mark.parametrize(
    ("reason", "expected_outcome"),
    [
        ("max_output_tokens", "output_truncated"),
        ("content_filter", "content_filtered"),
        ("provider_added_reason", "unknown"),
    ],
)
def test_stream_maps_incomplete_responses_to_safe_terminal_outcomes(
    reason,
    expected_outcome,
) -> None:
    lines = [
        _sse(
            "response.incomplete",
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": reason},
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_incomplete",
                            "name": "write",
                            "arguments": '{"path":"partial',
                        }
                    ],
                },
            },
        )
    ]

    deltas = list(_iter_deltas(lines))

    assert deltas[-1] == {"type": "finish", "reason": expected_outcome}


def test_stream_raises_provider_error_for_malformed_json() -> None:
    lines = ["event: response.output_text.delta\ndata: not-json\n\n"]

    with pytest.raises(ProviderError):
        list(_iter_deltas(lines))


def test_stream_raises_provider_error_for_non_object_json() -> None:
    lines = ["event: response.output_text.delta\ndata: []\n\n"]

    with pytest.raises(ProviderError):
        list(_iter_deltas(lines))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def test_build_payload_translates_user_image_media_block() -> None:
    payload = build_responses_payload(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "media", "media_type": "image/png", "base64": "aW1n"},
                ],
            }
        ],
        model_id="gpt-5.4",
        policy=responses_policy(),
    )

    assert payload["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is this?"},
                {"type": "input_image", "image_url": "data:image/png;base64,aW1n"},
            ],
        }
    ]


def test_build_payload_rejects_non_image_media_block() -> None:
    with pytest.raises(ProviderError):
        build_responses_payload(
            [
                {
                    "role": "user",
                    "content": [{"type": "media", "media_type": "audio/wav", "base64": "YXVkaW8="}],
                }
            ],
            model_id="gpt-5.4",
            policy=responses_policy(),
        )
