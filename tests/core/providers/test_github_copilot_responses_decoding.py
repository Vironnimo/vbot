"""Stateless Responses decoding: non-stream normalization, SSE deltas, usage, and errors."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat.streaming import StreamingAccumulator
from core.providers.adapter import (
    TERMINAL_OUTCOME_CONTENT_FILTERED,
    TERMINAL_OUTCOME_ERROR,
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
)
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    normalize_responses_response,
)
from tests.core.providers.github_copilot_test_support import decode_responses_sse, sse_event

_FINISH_STOP = {"type": "finish", "reason": TERMINAL_OUTCOME_STOP}
_FINISH_TOOL_CALLS = {"type": "finish", "reason": TERMINAL_OUTCOME_TOOL_CALLS}
_SUMMARY_ITEM = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
    "encrypted_content": "opaque",
}


def _tool_delta(call_id: str, name: str = "", arguments: str = "") -> dict[str, str]:
    return {
        "type": "tool_call_delta",
        "id": call_id,
        "name_delta": name,
        "arguments_delta": arguments,
    }


def _summary_delta(text: str) -> dict[str, Any]:
    return {"type": "reasoning_delta", "text": text, "summary_index": 0, "summary_text": text}


def _completion_meta(response_id: str, output: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``reasoning_meta`` delta a completion with reasoning items emits."""

    return {
        "type": "reasoning_meta",
        "reasoning_meta": {
            "response_id": response_id,
            "response_output": output,
            "reasoning_items": [item for item in output if item["type"] == "reasoning"],
            "encrypted_content": ["opaque"],
        },
    }


def _completed(response: dict[str, Any]) -> str:
    return sse_event("response.completed", {"response": {"status": "completed", **response}})


def _function_call(call_id: str, name: str, arguments: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        **fields,
    }


# ---------------------------------------------------------------------------
# Non-stream normalization
# ---------------------------------------------------------------------------


def test_normalize_response_maps_text_reasoning_tool_calls_and_usage() -> None:
    reasoning_item = {**_SUMMARY_ITEM, "summary": [{"type": "summary_text", "text": "Considered."}]}
    response = {
        "id": "resp_1",
        "output": [
            reasoning_item,
            {"type": "message", "content": [{"type": "output_text", "text": "Done."}]},
            _function_call("call_1", "search", '{"q":"docs"}'),
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }

    assert normalize_responses_response(response) == {
        "terminal_outcome": TERMINAL_OUTCOME_UNKNOWN,
        "role": "assistant",
        "content": "Done.",
        "reasoning": "Considered.",
        "reasoning_summary": ["Considered."],
        "reasoning_meta": {
            "response_id": "resp_1",
            "response_output": response["output"],
            "reasoning_items": [reasoning_item],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def test_normalize_response_reads_reasoning_text_from_content_blocks() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "content": [{"type": "reasoning_text", "text": "Need docs lookup."}],
        "encrypted_content": "opaque",
    }

    normalized = normalize_responses_response({"id": "resp_1", "output": [reasoning_item]})

    assert normalized["reasoning"] == "Need docs lookup."
    assert normalized["reasoning_meta"] == {
        "response_id": "resp_1",
        "response_output": [reasoning_item],
        "reasoning_items": [reasoning_item],
        "encrypted_content": ["opaque"],
    }


def _call_facts(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Tool call fields without the generated recovery ids and rejection prose."""

    facts: dict[str, Any] = {
        key: tool_call[key] for key in ("name", "arguments") if key in tool_call
    }
    if not tool_call["id"].startswith("tool_call_recovered_"):
        facts["id"] = tool_call["id"]
    if "rejection" in tool_call:
        facts["rejection"] = tool_call["rejection"]["code"]
    if "argument_sequence_index" in tool_call:
        facts["sequence"] = tool_call["argument_sequence_index"]
    return facts


_SEARCH_CALL = {"id": "call_1", "name": "search", "arguments": {"q": "docs"}}


@pytest.mark.parametrize(
    ("output", "expected_calls"),
    [
        pytest.param(
            _function_call("call_1", "search", '{"q":"docs"}'),
            [_SEARCH_CALL],
            id="collapsed-single-output-item",
        ),
        pytest.param(
            [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "function": {"name": "search", "arguments": '{"q":"docs"}'},
                }
            ],
            [_SEARCH_CALL],
            id="nested-function-only",
        ),
        pytest.param(
            [
                _function_call(
                    "call_1",
                    "",
                    "",
                    id="fc_1",
                    function={"name": "search", "arguments": '{"q":"docs"}'},
                )
            ],
            [_SEARCH_CALL],
            id="blank-top-level-defers-to-nested-function",
        ),
        pytest.param(
            [
                _function_call(
                    "call_1",
                    "tool",
                    "",
                    id="fc_1",
                    function={"name": "bash", "arguments": '{"command":"pwd"}'},
                )
            ],
            [{"id": "call_1", "name": "bash", "arguments": {"command": "pwd"}}],
            id="placeholder-name-defers-to-nested-function",
        ),
        pytest.param(
            [
                _function_call("call_bad", "search", "{not json"),
                _function_call("call_ok", "read_file", '{"path":"README.md"}'),
            ],
            [
                {
                    "id": "call_bad",
                    "name": "search",
                    "arguments": {},
                    "rejection": "malformed_tool_arguments",
                },
                {"id": "call_ok", "name": "read_file", "arguments": {"path": "README.md"}},
            ],
            id="malformed-arguments-rejected-and-valid-sibling-kept",
        ),
        pytest.param(
            [_function_call("call_batch", "bash", '{"command":"echo one"}{"command":"echo two"}')],
            [
                {
                    "id": "call_batch",
                    "name": "bash",
                    "arguments": {"command": "echo one"},
                    "sequence": 0,
                },
                {"name": "bash", "arguments": {"command": "echo two"}, "sequence": 1},
            ],
            id="consecutive-argument-objects-recovered",
        ),
    ],
)
def test_normalize_response_extracts_tool_calls(
    output: Any, expected_calls: list[dict[str, Any]]
) -> None:
    tool_calls = normalize_responses_response({"output": output})["tool_calls"]

    assert [_call_facts(call) for call in tool_calls] == expected_calls


@pytest.mark.parametrize(
    ("status", "reason", "outcome"),
    [
        ("incomplete", "max_output_tokens", TERMINAL_OUTCOME_OUTPUT_TRUNCATED),
        ("incomplete", "content_filter", TERMINAL_OUTCOME_CONTENT_FILTERED),
        ("incomplete", "other", TERMINAL_OUTCOME_UNKNOWN),
        ("failed", "", TERMINAL_OUTCOME_ERROR),
        ("completed", "", TERMINAL_OUTCOME_TOOL_CALLS),
        (None, "", TERMINAL_OUTCOME_UNKNOWN),
    ],
)
def test_normalize_response_terminal_outcome_guards_incomplete_calls(
    status: str | None, reason: str, outcome: str
) -> None:
    response = normalize_responses_response(
        {
            "status": status,
            "incomplete_details": {"reason": reason},
            "output": [_function_call("call", "write", "{}")],
        }
    )

    assert response["terminal_outcome"] == outcome


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param(
            {
                "input_tokens": 11,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 5},
            },
            {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 8,
                "cache_write_tokens": 2,
                "reasoning_tokens": 5,
            },
            id="token-details",
        ),
        pytest.param(
            {
                "input_tokens": 11,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": None},
            },
            {"input_tokens": 11, "output_tokens": 7},
            id="non-int-cached-tokens-ignored",
        ),
        pytest.param({"input_tokens": 0}, {"input_tokens": 0}, id="real-zero-kept"),
        pytest.param({"prompt_tokens": 12}, {"input_tokens": 12}, id="prompt-tokens-alias"),
        pytest.param({"completion_tokens": 7}, {"output_tokens": 7}, id="completion-tokens-alias"),
        pytest.param(
            {"input_tokens_details": {"cached_tokens": 2}}, None, id="details-without-counters"
        ),
    ],
)
def test_normalize_response_usage(usage: dict[str, Any], expected: dict[str, int] | None) -> None:
    assert normalize_responses_response({"usage": usage}).get("usage") == expected


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param(
            {"input_tokens": 17, "output_tokens": "12"},
            {"input_tokens": 17},
            id="non-int-output",
        ),
        pytest.param(
            {"input_tokens": True, "output_tokens": 17},
            {"output_tokens": 17},
            id="bool-input",
        ),
        pytest.param(
            {"input_tokens": 17, "output_tokens": -1},
            {"input_tokens": 17},
            id="negative-output",
        ),
    ],
)
def test_usage_keeps_only_valid_counters_in_responses_and_streams(
    usage: dict[str, Any], expected: dict[str, int]
) -> None:
    response = {
        "status": "completed",
        "output": [],
        "usage": {**usage, "input_tokens_details": {"cached_tokens": 4}},
    }
    expected = {**expected, "cache_read_tokens": 4}

    assert normalize_responses_response(response)["usage"] == expected
    accumulator = StreamingAccumulator()
    for delta in decode_responses_sse([_completed(response)]):
        accumulator.add_delta(delta)
    assert accumulator.finalize_assistant_fields().usage == expected


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_stream_decodes_text_reasoning_tool_calls_usage_and_finish() -> None:
    reasoning_item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
    lines = [
        sse_event("response.output_text.delta", {"delta": "Hel"}),
        sse_event("response.reasoning_summary_text.delta", {"delta": "Thinking"}),
        sse_event(
            "response.output_item.added",
            {"item": {"type": "function_call", "call_id": "call_1", "name": "search"}},
        ),
        sse_event("response.function_call_arguments.delta", {"item_id": "call_1", "delta": '{"q"'}),
        sse_event(
            "response.function_call_arguments.delta", {"item_id": "call_1", "delta": ':"docs"}'}
        ),
        sse_event("response.output_item.done", {"item": reasoning_item}),
        _completed(
            {
                "id": "resp_1",
                "output": [reasoning_item],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 3,
                    "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 1},
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            }
        ),
        "data: [DONE]\n\n",
    ]

    assert decode_responses_sse(lines) == [
        {"type": "content_delta", "text": "Hel"},
        _summary_delta("Thinking"),
        _tool_delta("call_1", name="search"),
        _tool_delta("call_1", arguments='{"q"'),
        _tool_delta("call_1", arguments=':"docs"}'),
        {"type": "reasoning_meta", "reasoning_meta": {"reasoning_items": [reasoning_item]}},
        _completion_meta("resp_1", [reasoning_item]),
        {
            "type": "usage",
            "input_tokens": 5,
            "output_tokens": 3,
            "cache_read_tokens": 2,
            "cache_write_tokens": 1,
            "reasoning_tokens": 2,
        },
        _FINISH_TOOL_CALLS,
    ]


_NESTED_SEARCH_ITEM = _function_call(
    "call_1", "", "", id="fc_1", function={"name": "search", "arguments": '{"q":"docs"}'}
)
_PLACEHOLDER_BASH_ITEM = _function_call(
    "call_1", "tool", "", id="fc_1", function={"name": "bash", "arguments": '{"command":"pwd"}'}
)
_PARTIAL_MESSAGE = {"type": "message", "id": "msg_1", "role": "assistant", "content": []}
_FINAL_MESSAGE = {
    **_PARTIAL_MESSAGE,
    "phase": "final_answer",
    "content": [{"type": "output_text", "text": "Done"}],
}
_READ_ITEM = _function_call("call", "read", "", id="item")
_INCOMPLETE_WRITE = _function_call("call_incomplete", "write", '{"path":"partial')


def _added(item: dict[str, Any], **fields: Any) -> str:
    return sse_event("response.output_item.added", {**fields, "item": item})


def _arguments(delta: str, **fields: Any) -> str:
    return sse_event("response.function_call_arguments.delta", {**fields, "delta": delta})


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        pytest.param(
            [sse_event("response.reasoning.delta", {"delta": "Thinking"})],
            [{"type": "reasoning_delta", "text": "Thinking"}],
            id="openrouter-reasoning-delta",
        ),
        pytest.param(
            [sse_event("response.unrecognized", {"type": "response.unrecognized", "value": 1})],
            [],
            id="unknown-event-ignored",
        ),
        pytest.param(
            [
                _added(
                    {"type": "function_call", "call_id": "call_stable", "name": "search"},
                    output_index=0,
                ),
                _arguments('{"q"', output_index=0),
                _arguments(':"docs"}', output_index=0),
                _completed({"output": [{"type": "function_call", "call_id": "call_stable"}]}),
            ],
            [
                _tool_delta("call_stable", name="search"),
                _tool_delta("call_stable", arguments='{"q"'),
                _tool_delta("call_stable", arguments=':"docs"}'),
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "response_output": [{"type": "function_call", "call_id": "call_stable"}]
                    },
                },
                _FINISH_TOOL_CALLS,
            ],
            id="output-index-only-deltas-keep-the-call-id",
        ),
        pytest.param(
            [
                _added(
                    {"type": "function_call", "call_id": "call_1", "function": {"name": "search"}}
                ),
                sse_event("response.reasoning_summary_text.delta", {"delta": "Need docs lookup."}),
                _completed(
                    {
                        "id": "resp_1",
                        "output": [_SUMMARY_ITEM],
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    }
                ),
            ],
            [
                _tool_delta("call_1", name="search"),
                _summary_delta("Need docs lookup."),
                _completion_meta("resp_1", [_SUMMARY_ITEM]),
                {"type": "usage", "input_tokens": 1, "output_tokens": 2},
                _FINISH_TOOL_CALLS,
            ],
            id="nested-item-name-and-streamed-call-finish-with-tool-calls",
        ),
        pytest.param(
            [
                _added(_NESTED_SEARCH_ITEM, output_index=0),
                _arguments('{"q":"docs"}', output_index=0, item_id="fc_1", call_id="call_1"),
                _completed({}),
            ],
            [
                _tool_delta("call_1", name="search", arguments='{"q":"docs"}'),
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "response_output": [{**_NESTED_SEARCH_ITEM, "arguments": '{"q":"docs"}'}]
                    },
                },
                _FINISH_TOOL_CALLS,
            ],
            id="replayed-argument-delta-deduplicated",
        ),
        pytest.param(
            [
                _added(
                    _function_call("call_1", "", "", id="fc_1", function={"name": "search"}),
                    output_index=0,
                ),
                _arguments('{"q":"docs"}', item_id="fc_1"),
            ],
            [
                _tool_delta("call_1", name="search"),
                _tool_delta("call_1", arguments='{"q":"docs"}'),
            ],
            id="item-id-only-delta-resolves-the-call-id",
        ),
        pytest.param(
            [
                _added(_PLACEHOLDER_BASH_ITEM, output_index=0),
                _arguments('{"command":"pwd"}', item_id="fc_1"),
                _completed({"id": "resp_1", "output": []}),
            ],
            [
                _tool_delta("call_1", name="bash", arguments='{"command":"pwd"}'),
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "response_id": "resp_1",
                        "response_output": [
                            {**_PLACEHOLDER_BASH_ITEM, "arguments": '{"command":"pwd"}'}
                        ],
                    },
                },
                _FINISH_TOOL_CALLS,
            ],
            id="placeholder-name-and-replayed-arguments-emit-one-call",
        ),
        pytest.param(
            [
                _added(
                    {
                        "type": "function_call",
                        "call_id": "call_stable",
                        "function": {"name": "search", "arguments": '{"q"'},
                    },
                    output_index=0,
                ),
                _arguments('{"q":"docs"}', output_index=0, call_id="call_stable"),
                _completed({}),
            ],
            [
                _tool_delta("call_stable", name="search", arguments='{"q"'),
                _tool_delta("call_stable", arguments=':"docs"}'),
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "response_output": [
                            {
                                "type": "function_call",
                                "call_id": "call_stable",
                                "arguments": '{"q":"docs"}',
                                "function": {"name": "search", "arguments": '{"q":"docs"}'},
                            }
                        ]
                    },
                },
                _FINISH_TOOL_CALLS,
            ],
            id="only-the-missing-argument-suffix-is-backfilled",
        ),
        pytest.param(
            [_arguments('{"value":"ab', call_id="call_1"), _arguments('ab"}', call_id="call_1")],
            [
                _tool_delta("call_1", arguments='{"value":"ab'),
                _tool_delta("call_1", arguments='ab"}'),
            ],
            id="repeated-boundary-text-kept",
        ),
        pytest.param(
            [
                _arguments('{"pattern":"abc","value":"', call_id="call_1"),
                _arguments("abc", call_id="call_1"),
                _arguments('"}', call_id="call_1"),
            ],
            [
                _tool_delta("call_1", arguments='{"pattern":"abc","value":"'),
                _tool_delta("call_1", arguments="abc"),
                _tool_delta("call_1", arguments='"}'),
            ],
            id="delta-seen-elsewhere-in-arguments-kept",
        ),
        pytest.param(
            [
                _added(_READ_ITEM, output_index=0),
                _arguments('{"path":', item_id="item"),
                _arguments('"x"}', item_id="item"),
                _completed({"id": "resp", "output": []}),
            ],
            [
                _tool_delta("call", name="read"),
                _tool_delta("call", arguments='{"path":'),
                _tool_delta("call", arguments='"x"}'),
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "response_id": "resp",
                        "response_output": [{**_READ_ITEM, "arguments": '{"path":"x"}'}],
                    },
                },
                _FINISH_TOOL_CALLS,
            ],
            id="empty-completion-replays-accumulated-arguments",
        ),
        pytest.param(
            [
                _completed(
                    {
                        "id": "resp_tool",
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_1",
                                "function": {"name": "search", "arguments": '{"q":"docs"}'},
                            }
                        ],
                    }
                )
            ],
            [
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
                _tool_delta("call_1", name="search", arguments='{"q":"docs"}'),
                _FINISH_TOOL_CALLS,
            ],
            id="terminal-only-call-emits-tool-deltas-and-tool-calls-finish",
        ),
        pytest.param(
            [
                sse_event(
                    "response.incomplete",
                    {
                        "type": "response.incomplete",
                        "response": {
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                            "output": [_INCOMPLETE_WRITE],
                        },
                    },
                )
            ],
            [
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {"response_output": [_INCOMPLETE_WRITE]},
                },
                _tool_delta("call_incomplete", name="write", arguments='{"path":"partial'),
                {"type": "finish", "reason": TERMINAL_OUTCOME_OUTPUT_TRUNCATED},
            ],
            id="incomplete-response-finishes-as-truncated",
        ),
        pytest.param(
            [
                _added(_PARTIAL_MESSAGE),
                sse_event("response.output_item.done", {"item": _FINAL_MESSAGE}),
                _completed({"id": "resp_1", "output": []}),
            ],
            [
                {"type": "content_delta", "text": "Done"},
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "response_id": "resp_1",
                        "response_output": [_FINAL_MESSAGE],
                    },
                },
                _FINISH_STOP,
            ],
            id="same-item-without-output-index-is-replaced",
        ),
    ],
)
def test_stream_decodes_tool_call_and_item_events(
    lines: list[str], expected: list[dict[str, Any]]
) -> None:
    assert decode_responses_sse(lines) == expected


_SPLIT_REASONING_ITEM = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [{"type": "summary_text", "text": "Need docs"}],
    "content": [{"type": "reasoning_text", "text": " lookup."}],
    "encrypted_content": "opaque",
}


@pytest.mark.parametrize(
    ("streamed_events", "output_item", "expected_reasoning"),
    [
        pytest.param(
            [],
            _SUMMARY_ITEM,
            [_summary_delta("Need docs lookup.")],
            id="completed-output-only",
        ),
        pytest.param(
            [sse_event("response.reasoning_summary_text.delta", {"delta": "Need docs lookup."})],
            _SUMMARY_ITEM,
            [_summary_delta("Need docs lookup.")],
            id="streamed-text-not-duplicated",
        ),
        pytest.param(
            [sse_event("response.reasoning_summary_text.delta", {"delta": "Need docs"})],
            _SPLIT_REASONING_ITEM,
            [_summary_delta("Need docs"), {"type": "reasoning_delta", "text": " lookup."}],
            id="only-the-missing-suffix-is-backfilled",
        ),
        pytest.param(
            [sse_event("response.output_item.done", {"item": _SUMMARY_ITEM})],
            _SUMMARY_ITEM,
            [
                _summary_delta("Need docs lookup."),
                {"type": "reasoning_meta", "reasoning_meta": {"reasoning_items": [_SUMMARY_ITEM]}},
            ],
            id="reasoning-output-item-done",
        ),
    ],
)
def test_stream_backfills_visible_reasoning_from_completed_items(
    streamed_events: list[str],
    output_item: dict[str, Any],
    expected_reasoning: list[dict[str, Any]],
) -> None:
    lines = [*streamed_events, _completed({"id": "resp_1", "output": [output_item]})]

    assert decode_responses_sse(lines) == [
        *expected_reasoning,
        _completion_meta("resp_1", [output_item]),
        _FINISH_STOP,
    ]


def test_stream_tool_call_accumulates_into_a_valid_chat_tool_call() -> None:
    lines = [
        _added(
            _function_call("call_1", "tool", "", id="fc_1", function={"name": "bash"}),
            output_index=0,
        ),
        _arguments('{"command":"pwd"}', item_id="fc_1"),
        _completed({}),
    ]
    accumulator = StreamingAccumulator()

    for delta in decode_responses_sse(lines):
        accumulator.add_delta(delta)

    assert accumulator.finalize_assistant_fields().tool_calls == [
        {"id": "call_1", "name": "bash", "arguments": {"command": "pwd"}}
    ]


@pytest.mark.parametrize(
    "fragments",
    [
        pytest.param(['{"value":', '{"value":', "1}}"], id="repeated-fragment"),
        pytest.param(['{"value":', '{"value":1}}'], id="prefix-overlapping-fragment"),
    ],
)
@pytest.mark.parametrize("terminal_snapshot", [False, True], ids=["no-snapshot", "snapshot"])
def test_argument_deltas_preserve_repeated_or_prefix_overlapping_fragments(
    fragments: list[str], terminal_snapshot: bool
) -> None:
    item = _function_call("call_1", "write", "", id="fc_1")
    arguments = "".join(fragments)
    lines = [
        _added(item, output_index=0),
        *(_arguments(part, item_id="fc_1") for part in fragments),
        _completed({"output": [{**item, "arguments": arguments}] if terminal_snapshot else []}),
    ]
    state = ResponsesStreamState()

    deltas = decode_responses_sse(lines, state)

    assert (
        "".join(delta["arguments_delta"] for delta in deltas if delta["type"] == "tool_call_delta")
        == arguments
    )
    result = state.normalized_response()
    assert result["tool_calls"] == [
        {"id": "call_1", "name": "write", "arguments": {"value": {"value": 1}}}
    ]
    assert result["reasoning_meta"]["response_output"][0]["arguments"] == arguments
    # Chat appends the true deltas verbatim, so the streamed Call matches the
    # completed one even after a terminal snapshot.
    accumulator = StreamingAccumulator()
    for delta in deltas:
        accumulator.add_delta(delta)
    assert accumulator.finalize_assistant_fields().tool_calls == result["tool_calls"]


# ---------------------------------------------------------------------------
# Stream errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_name", "event_data", "expected_type", "retryable"),
    [
        pytest.param(
            "response.failed",
            {"error": {"message": "Bad request."}},
            ProviderError,
            False,
            id="error-without-code",
        ),
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
            {"type": "error", "code": "rate_limit_exceeded", "message": "Codex rate limit."},
            ProviderRateLimitError,
            True,
            id="openai-codex-transient-rate-limit",
        ),
        pytest.param(
            "error",
            {"type": "error", "code": "invalid_api_key", "message": "Codex authentication failed."},
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
    ],
)
def test_stream_error_events_are_classified_and_keep_the_provider_message(
    event_name: str,
    event_data: dict[str, Any],
    expected_type: type[ProviderError],
    retryable: bool,
) -> None:
    with pytest.raises(ProviderError) as caught:
        decode_responses_sse([sse_event(event_name, event_data)])

    error = event_data.get("response", event_data).get("error", event_data)
    assert type(caught.value) is expected_type
    assert caught.value.retryable is retryable
    assert error["message"] in str(caught.value)


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param('{"test_frame":', id="malformed"),
        pytest.param('["test_frame"]', id="non-object"),
    ],
)
def test_stream_bad_frames_keep_evidence_without_naming_a_provider(frame: str) -> None:
    with pytest.raises(ProviderError) as caught:
        decode_responses_sse([f"data: {frame}"])

    assert caught.value.retryable is False
    assert frame in str(caught.value)
    # The shared decoder serves several Providers, so it names none of them.
    assert "GitHub Copilot" not in str(caught.value)
