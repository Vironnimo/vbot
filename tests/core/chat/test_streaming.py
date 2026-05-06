"""Tests for provider-agnostic streaming helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.chat.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
)
from core.chat.streaming import (
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    iter_with_chunk_timeout,
)

JsonObject = dict[str, Any]

pytestmark = pytest.mark.asyncio


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
                "arguments_delta": '{"path"',
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


async def test_suppresses_parsed_tool_arguments_until_finalization() -> None:
    accumulator = StreamingAccumulator()

    first_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "read_file",
            "arguments_delta": '{"path":"',
        }
    )[0]
    second_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": 'notes.md"}',
        }
    )[0]

    fields = accumulator.finalize_assistant_fields()
    assert "arguments" not in first_delta.payload
    assert "arguments" not in second_delta.payload
    assert fields.tool_calls == [
        {"id": "call_abc", "name": "read_file", "arguments": {"path": "notes.md"}}
    ]


async def test_malformed_tool_arguments_degrade_to_empty_object() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "read_file",
            "arguments_delta": '{"path":',
        }
    )

    fields = accumulator.finalize_assistant_fields()
    assert fields.tool_calls == [{"id": "call_abc", "name": "read_file", "arguments": {}}]


async def test_finish_delta_records_reason_without_visible_event() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta({"type": "finish", "reason": "stop"})

    fields = accumulator.finalize_assistant_fields()
    assert visible == []
    assert fields.finish_reason == "stop"


async def test_iter_with_chunk_timeout_resets_after_each_delta() -> None:
    async def source() -> AsyncIteratorForTest:
        yield {"type": "content_delta", "text": "first"}
        await asyncio.sleep(0.01)
        yield {"type": "content_delta", "text": "second"}

    chunks = [chunk async for chunk in iter_with_chunk_timeout(source(), timeout_seconds=0.05)]

    assert chunks == [
        {"type": "content_delta", "text": "first"},
        {"type": "content_delta", "text": "second"},
    ]


async def test_iter_with_chunk_timeout_fails_on_stalled_delta() -> None:
    closed = False

    async def source() -> AsyncIteratorForTest:
        nonlocal closed
        try:
            yield {"type": "content_delta", "text": "first"}
            await asyncio.sleep(1)
            yield {"type": "content_delta", "text": "late"}
        finally:
            closed = True

    iterator = iter_with_chunk_timeout(source(), timeout_seconds=0.01)

    assert await anext(iterator) == {"type": "content_delta", "text": "first"}
    with pytest.raises(StreamingChunkTimeoutError, match="stalled"):
        await anext(iterator)
    assert closed is True


AsyncIteratorForTest = Any
