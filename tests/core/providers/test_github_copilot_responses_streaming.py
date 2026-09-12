"""Github copilot responses: streaming behavior."""

from __future__ import annotations

import pytest

from core.providers.github_copilot_policy import RESPONSES_ENDPOINT
from core.providers.github_copilot_responses import (
    normalize_responses_response,
)
from tests.core.providers.github_copilot_responses_helpers import (
    _iter_deltas,
    _sse,
    responses_policy,
)


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
