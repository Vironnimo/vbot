"""Github copilot responses: payload behavior."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import (
    ProviderError,
)
from core.providers.github_copilot_responses import (
    build_responses_payload,
    estimate_responses_input_tokens,
)
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS
from tests.core.providers.github_copilot_responses_helpers import (
    responses_policy,
)


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


def test_estimate_responses_input_tokens_counts_wire_items_and_instructions() -> None:
    messages = [
        {"role": "system", "content": "Use concise answers."},
        {"role": "user", "content": "Hello"},
    ]
    payload = build_responses_payload(
        messages,
        model_id="gpt-5.4",
        policy=responses_policy(),
    )

    estimated = estimate_responses_input_tokens(messages)

    assert estimated > 0
    # The system instructions ride on the wire and must be budgeted.
    assert estimated > estimate_responses_input_tokens([{"role": "user", "content": "Hello"}])
    # The estimator counts the same items the payload carries on the wire.
    assert estimated > len(json.dumps(payload["input"])) // 4


def test_estimate_responses_input_tokens_counts_encrypted_reasoning_blobs() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "opaque-continuity-bytes",
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "I will call a tool.",
            "reasoning_meta": {"reasoning_items": [reasoning_item]},
            "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "result"},
    ]

    estimated = estimate_responses_input_tokens(messages)

    # The encrypted continuity blob rides on the wire and must be budgeted.
    assert estimated > estimate_responses_input_tokens(
        [
            {
                "role": "assistant",
                "content": "I will call a tool.",
                "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "result"},
        ]
    )


def test_estimate_responses_input_tokens_counts_rendered_tools() -> None:
    messages = [{"role": "user", "content": "Search docs"}]
    tools = [
        {
            "name": "search",
            "description": "Search the documentation",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]

    with_tools = estimate_responses_input_tokens(messages, tools=tools)
    without_tools = estimate_responses_input_tokens(messages)

    assert with_tools > without_tools


def test_estimate_responses_input_tokens_normalizes_native_media() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": "a" * 10_000,
                }
            ],
        }
    ]

    estimated = estimate_responses_input_tokens(messages)

    # The base64 transport payload must not masquerade as prose tokens.
    assert estimated < 10_000


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


def test_build_payload_omits_unspecified_top_p() -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Hello"}],
        model_id="gpt-5.4",
        policy=responses_policy(),
        top_p=None,
    )

    assert "top_p" not in payload


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
