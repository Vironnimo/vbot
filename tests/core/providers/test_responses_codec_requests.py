"""Stateless Responses request codec: payload fields, conversation replay, and estimates."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import ProviderError
from core.providers.github_copilot_responses import (
    REASONING_ENCRYPTED_CONTENT_INCLUDE,
    build_responses_payload,
    estimate_responses_input_tokens,
)
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS
from core.utils.tokens import NATIVE_MEDIA_TOKEN_RESERVE
from tests.core.providers.responses_test_support import responses_policy, responses_reasoning

_ENCRYPTED_INCLUDE = [REASONING_ENCRYPTED_CONTENT_INCLUDE]
_HELLO = [{"role": "user", "content": "Hello"}]
_HELLO_INPUT = [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}]
_SEARCH_PARAMETERS = {"type": "object", "properties": {"q": {"type": "string"}}}


def test_payload_carries_system_instructions_and_always_requests_encrypted_reasoning() -> None:
    payload = build_responses_payload(
        [{"role": "system", "content": "Use concise answers."}, *_HELLO],
        model_id="gpt-5.4",
        policy=responses_policy(),
        reasoning_renderer=responses_reasoning(),
    )

    # Reasoning-capable Models need the continuity bytes even without an effort.
    assert payload == {
        "model": "gpt-5.4",
        "instructions": "Use concise answers.",
        "input": _HELLO_INPUT,
        "include": _ENCRYPTED_INCLUDE,
    }


def test_payload_maps_effort_and_tool_definitions_and_gates_structured_output() -> None:
    payload = build_responses_payload(
        [{"role": "user", "content": "Return JSON"}],
        model_id="gpt-5.4",
        policy=responses_policy(structured_outputs=False),
        reasoning_renderer=responses_reasoning("xhigh", levels=("low",)),
        tools=[
            {"name": "search", "description": "Search", "parameters": {"type": "object"}},
            # A blank top-level name defers to the nested function definition.
            {
                "type": "function",
                "name": "",
                "function": {
                    "name": "lookup",
                    "description": "Look up docs",
                    "parameters": _SEARCH_PARAMETERS,
                },
            },
            # The history Tool maps like any other Tool, without a special case.
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "parameters": HISTORY_TOOL_PARAMETERS,
            },
        ],
        tool_choice="auto",
        response_format={"type": "json_object"},
    )

    assert payload == {
        "model": "gpt-5.4",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Return JSON"}]}],
        "reasoning": {"effort": "low", "summary": "auto"},
        "include": _ENCRYPTED_INCLUDE,
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search",
                "parameters": {"type": "object"},
                "strict": False,
            },
            {
                "type": "function",
                "name": "lookup",
                "description": "Look up docs",
                "parameters": _SEARCH_PARAMETERS,
                "strict": False,
            },
            {
                "type": "function",
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "parameters": HISTORY_TOOL_PARAMETERS,
                "strict": False,
            },
        ],
        "tool_choice": "auto",
    }


@pytest.mark.parametrize(
    ("policy_overrides", "request_kwargs", "expected_fields"),
    [
        pytest.param(
            {},
            {
                "include": ["unsupported.trace", REASONING_ENCRYPTED_CONTENT_INCLUDE],
                "cache_control": {"type": "ephemeral"},
                "prompt_cache_key": "cache-key",
                "prompt_cache_retention": "24h",
                "unknown_extra": "do-not-forward",
                "temperature": 0.2,
                "top_p": 0.9,
                "max_tokens": 512,
                "parallel_tool_calls": True,
            },
            # Caller-supplied include is ignored; only the continuity request remains.
            {
                "include": _ENCRYPTED_INCLUDE,
                "top_p": 0.9,
                "max_output_tokens": 512,
                "parallel_tool_calls": True,
            },
            id="unsafe-fields-dropped",
        ),
        pytest.param(
            {"reasoning_supported": False},
            {"temperature": 0.2, "top_p": 0.9, "max_tokens": 512},
            # A Model without reasoning gets no encrypted continuity request.
            {"top_p": 0.9, "max_output_tokens": 512},
            id="partial-metadata-omits-temperature-and-include",
        ),
        pytest.param(
            {},
            {"max_tokens": 512, "max_output_tokens": 1024, "top_p": None},
            {"include": _ENCRYPTED_INCLUDE, "max_output_tokens": 1024},
            id="explicit-max-output-tokens-wins-and-unset-top-p-omitted",
        ),
        pytest.param(
            {"tool_calls": False},
            {
                "tools": [{"name": "search", "description": "Search", "parameters": {}}],
                "tool_choice": "auto",
            },
            {"include": _ENCRYPTED_INCLUDE},
            id="tools-omitted-when-disallowed",
        ),
    ],
)
def test_payload_forwards_only_fields_the_policy_allows(
    policy_overrides: dict[str, Any],
    request_kwargs: dict[str, Any],
    expected_fields: dict[str, Any],
) -> None:
    overrides = dict(policy_overrides)
    reasoning = responses_reasoning(supported=overrides.pop("reasoning_supported", True))
    payload = build_responses_payload(
        _HELLO,
        model_id="gpt-5.4",
        policy=responses_policy(**overrides),
        reasoning_renderer=reasoning,
        **request_kwargs,
    )

    assert payload == {"model": "gpt-5.4", "input": _HELLO_INPUT, **expected_fields}


# ---------------------------------------------------------------------------
# Conversation replay
# ---------------------------------------------------------------------------

_REASONING_ITEM = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
_SEARCH_RESULT = {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "result"}
_SEARCH_OUTPUT = {"type": "function_call_output", "call_id": "call_1", "output": "result"}
_SEARCH_CALL = {
    "type": "function_call",
    "call_id": "call_1",
    "name": "search",
    "arguments": '{"q":"docs"}',
}
_COMPLETE_OUTPUT = [
    _REASONING_ITEM,
    {
        "type": "message",
        "role": "assistant",
        "phase": "commentary",
        "content": [{"type": "output_text", "text": "I will inspect this."}],
    },
    {**_SEARCH_CALL, "id": "fc_1"},
]
_REJECTED_OUTPUT = [
    _REASONING_ITEM,
    {
        "type": "function_call",
        "id": "fc_bad",
        "call_id": "call_bad",
        "name": "write",
        "arguments": '{"path":"README.md"',
    },
]
_REJECTION_FAILURE = '{"ok":false,"error":{"code":"malformed_tool_arguments"}}'
_BATCH_OUTPUT = [
    _REASONING_ITEM,
    {
        "type": "function_call",
        "id": "fc_batch",
        "call_id": "call_batch",
        "name": "bash",
        "arguments": '{"command":"echo one"}{"command":"echo two"}',
    },
]


def _bash_call(call_id: str, command: str, index: int) -> dict[str, Any]:
    return {
        "id": call_id,
        "name": "bash",
        "arguments": {"command": command},
        "argument_sequence_index": index,
        "argument_sequence_length": 2,
    }


def _image_input(data: str) -> dict[str, str]:
    return {"type": "input_image", "image_url": f"data:image/png;base64,{data}"}


@pytest.mark.parametrize(
    ("messages", "expected_input"),
    [
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": "I will call a tool.",
                    "reasoning_meta": {"reasoning_items": [_REASONING_ITEM]},
                    "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
                },
                _SEARCH_RESULT,
            ],
            [
                _REASONING_ITEM,
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I will call a tool."}],
                },
                _SEARCH_CALL,
                _SEARCH_OUTPUT,
            ],
            id="reasoning-items-tool-call-and-result",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": "A reconstructed copy that must not be used.",
                    "phase": "final_answer",
                    "reasoning_meta": {"response_output": _COMPLETE_OUTPUT},
                    "tool_calls": [{"id": "duplicate", "name": "search", "arguments": {}}],
                },
                _SEARCH_RESULT,
            ],
            [*_COMPLETE_OUTPUT, _SEARCH_OUTPUT],
            id="complete-response-output-verbatim",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_meta": {"response_output": _REJECTED_OUTPUT},
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
                    "content": _REJECTION_FAILURE,
                },
            ],
            [
                _REASONING_ITEM,
                {
                    "type": "function_call",
                    "call_id": "call_bad",
                    "name": "write",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_bad",
                    "output": _REJECTION_FAILURE,
                },
            ],
            id="rejected-raw-call-replaced-by-canonical-call",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_meta": {"response_output": _BATCH_OUTPUT},
                    "tool_calls": [
                        _bash_call("call_batch", "echo one", 0),
                        _bash_call("tool_call_recovered_1234", "echo two", 1),
                    ],
                },
                {"role": "tool", "tool_call_id": "call_batch", "name": "bash", "content": "first"},
                {
                    "role": "tool",
                    "tool_call_id": "tool_call_recovered_1234",
                    "name": "bash",
                    "content": "second",
                },
            ],
            [
                _REASONING_ITEM,
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
            ],
            id="recovered-argument-sequence-expanded",
        ),
        pytest.param(
            [{"role": "assistant", "content": "Intermediate update.", "phase": "commentary"}],
            [
                {
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Intermediate update."}],
                }
            ],
            id="phase-on-canonical-message",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "search", "arguments": '{"q":"docs"}'},
                        }
                    ],
                }
            ],
            [_SEARCH_CALL],
            id="nested-function-call-shape",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "",
                            "arguments": "",
                            "function": {"name": "search", "arguments": '{"q":"docs"}'},
                        }
                    ],
                }
            ],
            [_SEARCH_CALL],
            id="blank-top-level-call-defers-to-nested-function",
        ),
        pytest.param(
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
                    "tool_call_id": "call_image",
                    "content": '{"ok":true}',
                    TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
                        {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
                        {"type": "text", "text": "[Image path: C:/diagram.png]"},
                    ],
                },
            ],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is this?"},
                        _image_input("aW1n"),
                    ],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_image",
                    "output": [
                        {"type": "input_text", "text": '{"ok":true}'},
                        _image_input("aW1hZ2U="),
                        {"type": "input_text", "text": "[Image path: C:/diagram.png]"},
                    ],
                },
            ],
            id="user-and-tool-result-images",
        ),
    ],
)
def test_payload_replays_conversation_as_responses_items(
    messages: list[dict[str, Any]], expected_input: list[dict[str, Any]]
) -> None:
    payload = build_responses_payload(
        messages,
        model_id="gpt-5.4",
        policy=responses_policy(),
        reasoning_renderer=responses_reasoning(),
    )

    assert payload["input"] == expected_input


def test_payload_maps_an_allowed_document_to_input_file() -> None:
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
        model_id="gpt-5.4",
        policy=responses_policy(),
        reasoning_renderer=responses_reasoning(),
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


@pytest.mark.parametrize(
    "media_block",
    [
        pytest.param(
            {"type": "media", "media_type": "audio/wav", "base64": "YXVkaW8="}, id="audio"
        ),
        pytest.param({"type": "media", "media_type": "image/png"}, id="missing-data"),
    ],
)
def test_payload_rejects_media_the_endpoint_cannot_carry(media_block: dict[str, Any]) -> None:
    with pytest.raises(ProviderError) as caught:
        build_responses_payload(
            [{"role": "user", "content": [media_block]}],
            model_id="gpt-5.4",
            policy=responses_policy(),
            reasoning_renderer=responses_reasoning(),
        )

    assert caught.value.retryable is False


# ---------------------------------------------------------------------------
# Input estimate
# ---------------------------------------------------------------------------

_TOOL_TURN: list[dict[str, Any]] = [
    {
        "role": "assistant",
        "content": "I will call a tool.",
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
    },
    _SEARCH_RESULT,
]


@pytest.mark.parametrize(
    ("messages", "with_addition"),
    [
        pytest.param(
            _HELLO,
            {"messages": [{"role": "system", "content": "Use concise answers."}, *_HELLO]},
            id="system-instructions",
        ),
        pytest.param(
            _TOOL_TURN,
            {
                "messages": [
                    {
                        **_TOOL_TURN[0],
                        "reasoning_meta": {
                            "reasoning_items": [
                                {
                                    "type": "reasoning",
                                    "id": "rs_1",
                                    "encrypted_content": "opaque-continuity-bytes",
                                }
                            ]
                        },
                    },
                    _TOOL_TURN[1],
                ]
            },
            id="encrypted-reasoning",
        ),
        pytest.param(
            _HELLO,
            {
                "messages": _HELLO,
                "tools": [
                    {
                        "name": "search",
                        "description": "Search docs",
                        "parameters": _SEARCH_PARAMETERS,
                    }
                ],
            },
            id="tool-definitions",
        ),
    ],
)
def test_estimate_budgets_everything_that_rides_on_the_wire(
    messages: list[dict[str, Any]], with_addition: dict[str, Any]
) -> None:
    estimated = estimate_responses_input_tokens(
        with_addition["messages"], tools=with_addition.get("tools")
    )
    payload = build_responses_payload(
        with_addition["messages"],
        model_id="gpt-5.4",
        policy=responses_policy(),
        reasoning_renderer=responses_reasoning(),
    )

    assert estimated > estimate_responses_input_tokens(messages)
    assert estimated > len(json.dumps(payload["input"])) // 4


def test_estimate_prices_native_media_as_an_image_for_the_active_model() -> None:
    def image_message(base64_data: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [{"type": "media", "media_type": "image/png", "base64": base64_data}],
            }
        ]

    one_pixel_png = image_message(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
        "QVR42mP8/x8AAwMCAO+aXfcAAAAASUVORK5CYII="
    )

    # The base64 transport payload must not masquerade as prose tokens.
    assert estimate_responses_input_tokens(image_message("a" * 10_000)) < 10_000
    known = estimate_responses_input_tokens(one_pixel_png, model_id="gpt-4o")
    fallback = estimate_responses_input_tokens(one_pixel_png, model_id="unknown")
    # gpt-4o prices a one-tile image at 85 base plus 170 tile tokens.
    assert fallback - known == NATIVE_MEDIA_TOKEN_RESERVE - 255
