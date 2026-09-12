"""Tests for streaming."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.chat.streaming import (
    StreamingAccumulator,
    StreamingAssistantFields,
    StreamingDeltaBatcher,
    StreamingDeltaError,
    StreamingVisibleDelta,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
)

pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]


async def test_accumulates_visible_deltas_in_provider_order() -> None:
    accumulator = StreamingAccumulator()

    emitted = []
    emitted.extend(accumulator.add_delta({"type": "reasoning_delta", "text": "Think"}))
    emitted.extend(accumulator.add_delta({"type": "content_delta", "text": "Hello"}))
    emitted.extend(
        accumulator.add_delta(
            {
                "type": "tool_call_delta",
                "id": "call_abc",
                "name_delta": "read",
                "arguments_delta": '{"path":"notes.md"}',
            }
        )
    )
    emitted.extend(accumulator.add_delta({"type": "content_delta", "text": " world"}))

    fields = accumulator.finalize_assistant_fields()

    assert [delta.event_type for delta in emitted] == [
        REASONING_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        TOOL_CALL_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
    ]
    assert [delta.event_type for delta in accumulator.visible_deltas] == [
        REASONING_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        TOOL_CALL_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
    ]
    assert fields.content == "Hello world"
    assert fields.reasoning == "Think"


async def test_batches_adjacent_visible_deltas_at_bounded_cadence() -> None:
    batcher = StreamingDeltaBatcher(interval_seconds=0.04)

    first = batcher.add(
        StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": "Hel"},
        ),
        now=10.0,
    )
    second = batcher.add(
        StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": "lo"},
        ),
        now=10.01,
    )
    due = batcher.add(
        StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": "!"},
        ),
        now=10.04,
    )

    assert [delta.payload for delta in first] == [{"content_delta": "Hel"}]
    assert second == []
    assert [delta.payload for delta in due] == [{"content_delta": "lo!"}]
    assert batcher.flush() == []


async def test_batcher_preserves_delta_order_and_tool_call_identity() -> None:
    batcher = StreamingDeltaBatcher(interval_seconds=1.0)

    batcher.add(
        StreamingVisibleDelta(
            event_type=REASONING_DELTA_EVENT,
            payload={"reasoning_delta": "First"},
        ),
        now=1.0,
    )
    batcher.add(
        StreamingVisibleDelta(
            event_type=TOOL_CALL_DELTA_EVENT,
            payload={"tool_call_id": "one", "name_delta": "re"},
        ),
        now=1.1,
    )
    batcher.add(
        StreamingVisibleDelta(
            event_type=TOOL_CALL_DELTA_EVENT,
            payload={"tool_call_id": "one", "name_delta": "ad"},
        ),
        now=1.2,
    )
    batcher.add(
        StreamingVisibleDelta(
            event_type=TOOL_CALL_DELTA_EVENT,
            payload={"tool_call_id": "two", "arguments_delta": "{}"},
        ),
        now=1.3,
    )

    pending = batcher.flush()

    assert [(delta.event_type, delta.payload) for delta in pending] == [
        (TOOL_CALL_DELTA_EVENT, {"tool_call_id": "one", "name_delta": "read"}),
        (TOOL_CALL_DELTA_EVENT, {"tool_call_id": "two", "arguments_delta": "{}"}),
    ]


async def test_partial_reasoning_is_none_without_reasoning_deltas() -> None:
    accumulator = StreamingAccumulator()

    assert accumulator.partial_reasoning is None


async def test_partial_reasoning_returns_joined_reasoning_deltas() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta({"type": "reasoning_delta", "text": "Think"})
    accumulator.add_delta({"type": "reasoning_delta", "text": " harder"})

    assert accumulator.partial_reasoning == "Think harder"


async def test_finalizes_empty_content_tool_only_response() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "get_weather",
            "arguments_delta": '{"city":"Berlin"}',
        }
    )
    accumulator.add_delta({"type": "finish", "reason": "tool_calls"})

    fields = accumulator.finalize_assistant_fields()

    assert fields.content is None
    assert fields.reasoning is None
    assert fields.tool_calls == [
        {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]
    assert fields.finish_reason == "tool_calls"


async def test_finalized_streamed_tool_calls_keep_stable_indexes_in_arrival_order() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_first",
            "name_delta": "read",
            "arguments_delta": '{"path":"one.md"}',
        }
    )
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_second",
            "name_delta": "read",
            "arguments_delta": '{"path":"two.md"}',
        }
    )
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_first",
            "arguments_delta": "",
        }
    )

    fields = accumulator.finalize_assistant_fields()

    assert fields.tool_calls == [
        {"id": "call_first", "name": "read", "arguments": {"path": "one.md"}},
        {"id": "call_second", "name": "read", "arguments": {"path": "two.md"}},
    ]


async def test_late_provider_id_replaces_unknown_identity_for_same_stream_slot() -> None:
    accumulator = StreamingAccumulator()

    first_visible = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "slot": 0,
            "name_delta": "search",
            "arguments_delta": '{"query":"',
        }
    )[0]
    second_visible = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "slot": 0,
            "id": "call_provider_late",
            "arguments_delta": 'Berlin"}',
        }
    )[0]

    fields = accumulator.finalize_assistant_fields()

    assert first_visible.payload["tool_call_id"] == "tool_call_0"
    assert second_visible.payload["tool_call_id"] == "call_provider_late"
    assert fields.tool_calls == [
        {
            "id": "call_provider_late",
            "name": "search",
            "arguments": {"query": "Berlin"},
        }
    ]


async def test_never_supplied_provider_id_is_synthesized_only_at_finalization() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "slot": 3,
            "name_delta": "read",
            "arguments_delta": '{"path":"README.md"}',
        }
    )

    fields = accumulator.finalize_assistant_fields()

    assert fields.tool_calls == [
        {"id": "tool_call_3", "name": "read", "arguments": {"path": "README.md"}}
    ]


async def test_interleaved_stream_slots_merge_independently_with_late_ids() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "slot": 0,
            "name_delta": "write",
            "arguments_delta": '{"path":"',
        }
    )
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "slot": 1,
            "id": "call_read",
            "name_delta": "read",
            "arguments_delta": '{"path":"README',
        }
    )
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "slot": 0,
            "id": "call_write",
            "arguments_delta": 'notes.md"}',
        }
    )
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "slot": 1,
            "arguments_delta": '.md"}',
        }
    )

    fields = accumulator.finalize_assistant_fields()

    assert fields.tool_calls == [
        {"id": "call_write", "name": "write", "arguments": {"path": "notes.md"}},
        {"id": "call_read", "name": "read", "arguments": {"path": "README.md"}},
    ]


async def test_preserves_reasoning_meta_without_public_delta() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta(
        {"type": "reasoning_meta", "reasoning_meta": {"signature": "opaque"}}
    )
    accumulator.add_delta(
        {"type": "reasoning_meta", "reasoning_meta": {"encrypted_content": "opaque-too"}}
    )

    fields = accumulator.finalize_assistant_fields()
    assert visible == []
    assert accumulator.visible_deltas == []
    assert fields.reasoning_meta == {
        "signature": "opaque",
        "encrypted_content": "opaque-too",
    }


async def test_reasoning_meta_item_lists_accumulate_and_keep_encrypted_content() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "first"}],
                    }
                ]
            },
        }
    )
    accumulator.add_delta(
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_2",
                        "summary": [{"type": "summary_text", "text": "second"}],
                    }
                ]
            },
        }
    )
    accumulator.add_delta(
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "secret-1",
                    }
                ],
                "response_output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "secret-1",
                        "summary": [{"type": "summary_text", "text": "first"}],
                    },
                    {
                        "type": "reasoning",
                        "id": "rs_2",
                        "encrypted_content": "secret-2",
                        "summary": [{"type": "summary_text", "text": "second"}],
                    },
                ],
            },
        }
    )

    fields = accumulator.finalize_assistant_fields()
    assert fields.reasoning_meta is not None
    assert fields.reasoning_meta["reasoning_items"] == [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "first"}],
            "encrypted_content": "secret-1",
        },
        {
            "type": "reasoning",
            "id": "rs_2",
            "summary": [{"type": "summary_text", "text": "second"}],
        },
    ]
    assert fields.reasoning_meta["response_output"][0]["encrypted_content"] == "secret-1"
    assert fields.reasoning_meta["response_output"][1]["encrypted_content"] == "secret-2"


async def test_assistant_fields_includes_usage_in_response_dict_when_set() -> None:
    fields = StreamingAssistantFields(
        content="hello",
        reasoning=None,
        reasoning_meta=None,
        tool_calls=None,
        finish_reason="stop",
        usage={"input_tokens": 100, "output_tokens": 50},
    )

    result = fields.to_response_dict()

    assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert result["terminal_outcome"] == "stop"


async def test_assistant_fields_omits_usage_from_response_dict_when_none() -> None:
    fields = StreamingAssistantFields(
        content="hello",
        reasoning=None,
        reasoning_meta=None,
        tool_calls=None,
        finish_reason="stop",
        usage=None,
    )

    result = fields.to_response_dict()

    assert "usage" not in result


async def test_accumulator_accumulates_usage_delta() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta({"type": "usage", "input_tokens": 250, "output_tokens": 80})

    assert visible == []
    fields = accumulator.finalize_assistant_fields()
    assert fields.usage == {"input_tokens": 250, "output_tokens": 80}


async def test_accumulator_preserves_partial_usage_delta() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta({"type": "usage", "output_tokens": 2572})

    assert visible == []
    fields = accumulator.finalize_assistant_fields()
    assert fields.usage == {"output_tokens": 2572}


async def test_finalize_assistant_fields_includes_usage_when_received_via_delta() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta({"type": "content_delta", "text": "Hello"})
    accumulator.add_delta({"type": "usage", "input_tokens": 500, "output_tokens": 200})
    accumulator.add_delta({"type": "finish", "reason": "stop"})

    fields = accumulator.finalize_assistant_fields()

    assert fields.content == "Hello"
    assert fields.finish_reason == "stop"
    assert fields.usage == {"input_tokens": 500, "output_tokens": 200}

    response_dict = fields.to_response_dict()
    assert response_dict["usage"] == {"input_tokens": 500, "output_tokens": 200}


async def test_accumulator_keeps_optional_token_details_from_usage_delta() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta(
        {
            "type": "usage",
            "input_tokens": 250,
            "output_tokens": 80,
            "cache_read_tokens": 200,
            "cache_write_tokens": 30,
            "reasoning_tokens": 50,
        }
    )

    assert visible == []
    fields = accumulator.finalize_assistant_fields()
    assert fields.usage == {
        "input_tokens": 250,
        "output_tokens": 80,
        "cache_read_tokens": 200,
        "cache_write_tokens": 30,
        "reasoning_tokens": 50,
    }


async def test_accumulator_drops_non_integer_cache_token_fields() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "usage",
            "input_tokens": 250,
            "output_tokens": 80,
            "cache_read_tokens": None,
            "cache_write_tokens": "bad",
            "reasoning_tokens": -1,
        }
    )

    fields = accumulator.finalize_assistant_fields()
    assert fields.usage == {"input_tokens": 250, "output_tokens": 80}


async def test_usage_delta_rejects_non_integer_tokens() -> None:
    accumulator = StreamingAccumulator()

    with pytest.raises(StreamingDeltaError):
        accumulator.add_delta({"type": "usage", "input_tokens": "bad", "output_tokens": 10})

    with pytest.raises(StreamingDeltaError):
        accumulator.add_delta({"type": "usage", "input_tokens": 10, "output_tokens": "bad"})


async def test_accumulator_usage_is_none_when_no_usage_delta() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta({"type": "content_delta", "text": "Hi"})
    accumulator.add_delta({"type": "finish", "reason": "stop"})

    fields = accumulator.finalize_assistant_fields()

    assert fields.usage is None
    assert "usage" not in fields.to_response_dict()


async def test_partial_content_is_none_without_content_deltas() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta({"type": "reasoning_delta", "text": "Think"})

    assert accumulator.partial_content is None


async def test_partial_content_returns_joined_content_deltas() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta({"type": "content_delta", "text": "Hello"})
    accumulator.add_delta({"type": "content_delta", "text": " world"})

    assert accumulator.partial_content == "Hello world"


async def test_finalize_partial_fields_drops_in_flight_tool_call_without_raising() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta({"type": "reasoning_delta", "text": "Working"})
    accumulator.add_delta({"type": "content_delta", "text": "Here is the"})
    # Malformed/incomplete tool-call fragment that finalize_assistant_fields would reject.
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "write",
            "arguments_delta": '{"path":',
        }
    )

    fields = accumulator.finalize_partial_fields()

    assert fields.content == "Here is the"
    assert fields.reasoning == "Working"
    assert fields.tool_calls is None
    assert fields.finish_reason is None


async def test_reasoning_timing_absent_without_reasoning_deltas() -> None:
    accumulator = StreamingAccumulator()
    accumulator.add_delta({"type": "content_delta", "text": "Answer"})
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "read",
            "arguments_delta": "{}",
        }
    )

    fields = accumulator.finalize_assistant_fields()

    assert fields.reasoning_timing is None


async def test_reasoning_timing_spans_first_to_last_reasoning_delta() -> None:
    accumulator = StreamingAccumulator()
    accumulator.add_delta({"type": "reasoning_delta", "text": "Think"})
    await asyncio.sleep(0.02)
    # Empty reasoning fragments carry no text and must not move the window.
    accumulator.add_delta({"type": "reasoning_delta", "text": ""})
    accumulator.add_delta({"type": "reasoning_delta", "text": " more"})

    timing = accumulator.reasoning_timing

    assert timing is not None
    assert set(timing) == {"started_at", "completed_at", "duration_ms"}
    assert timing["started_at"] < timing["completed_at"]
    assert timing["duration_ms"] >= 10
    fields = accumulator.finalize_assistant_fields()
    assert fields.reasoning_timing == timing


async def test_reasoning_timing_present_on_finalized_partial_fields() -> None:
    accumulator = StreamingAccumulator()
    accumulator.add_delta({"type": "reasoning_delta", "text": "Partial thought"})

    fields = accumulator.finalize_partial_fields()

    assert fields.reasoning is not None
    assert fields.reasoning_timing is not None
    assert fields.reasoning_timing["duration_ms"] >= 0
    assert fields.reasoning_timing["started_at"] <= fields.reasoning_timing["completed_at"]


async def test_accumulator_tracks_the_final_readable_text_phase() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta({"type": "reasoning_delta", "text": "Plan"})
    assert accumulator.ends_with_reasoning is True

    accumulator.add_delta({"type": "content_delta", "text": "Answer"})
    assert accumulator.ends_with_reasoning is False

    accumulator.add_delta({"type": "reasoning_delta", "text": " misrouted tail"})
    assert accumulator.ends_with_reasoning is True
