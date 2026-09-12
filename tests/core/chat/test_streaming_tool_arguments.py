"""Tests for streaming tool arguments."""

from __future__ import annotations

import pytest

from core.chat.streaming import (
    StreamingAccumulator,
)

pytestmark = pytest.mark.asyncio


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


async def test_cumulative_tool_argument_fragments_emit_only_missing_suffix_and_finalize() -> None:
    accumulator = StreamingAccumulator()

    first_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "write",
            "arguments_delta": '{"path":"',
        }
    )[0]
    second_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": '{"path":"notes.md"}',
        }
    )[0]
    duplicate_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": '{"path":"notes.md"}',
        }
    )

    fields = accumulator.finalize_assistant_fields()
    assert first_delta.payload == {
        "tool_call_id": "call_abc",
        "name_delta": "write",
        "arguments_delta": '{"path":"',
    }
    assert second_delta.payload == {
        "tool_call_id": "call_abc",
        "arguments_delta": 'notes.md"}',
    }
    assert duplicate_delta == []
    assert fields.tool_calls == [
        {"id": "call_abc", "name": "write", "arguments": {"path": "notes.md"}}
    ]


async def test_tool_argument_merge_keeps_non_tail_repeated_text() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "write",
            "arguments_delta": '{"pattern":"abc","value":"',
        }
    )
    repeated_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": "abc",
        }
    )[0]
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": '"}',
        }
    )

    fields = accumulator.finalize_assistant_fields()
    assert repeated_delta.payload == {
        "tool_call_id": "call_abc",
        "arguments_delta": "abc",
    }
    assert fields.tool_calls == [
        {
            "id": "call_abc",
            "name": "write",
            "arguments": {"pattern": "abc", "value": "abc"},
        }
    ]


async def test_tool_argument_merge_preserves_repeated_boundary_text() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "write",
            "arguments_delta": '{"value":"ab',
        }
    )
    repeated_boundary_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": 'ab"}',
        }
    )[0]

    fields = accumulator.finalize_assistant_fields()
    assert repeated_boundary_delta.payload == {
        "tool_call_id": "call_abc",
        "arguments_delta": 'ab"}',
    }
    assert fields.tool_calls == [
        {
            "id": "call_abc",
            "name": "write",
            "arguments": {"value": "abab"},
        }
    ]


async def test_tool_argument_merge_preserves_closing_quote_after_escaped_inner_quote() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "bash",
            "arguments_delta": '{"command":"echo \\"',
        }
    )
    second_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": '"}',
        }
    )[0]

    fields = accumulator.finalize_assistant_fields()
    assert second_delta.payload == {
        "tool_call_id": "call_abc",
        "arguments_delta": '"}',
    }
    assert fields.tool_calls == [
        {
            "id": "call_abc",
            "name": "bash",
            "arguments": {"command": 'echo "'},
        }
    ]


async def test_tool_argument_merge_preserves_backslash_escape_pair_at_chunk_boundary() -> None:
    accumulator = StreamingAccumulator()

    first_fragment = '{"path":"C:' + "\\"
    second_fragment = '\\Users\\\\notes.txt"}'
    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "write",
            "arguments_delta": first_fragment,
        }
    )
    second_delta = accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "arguments_delta": second_fragment,
        }
    )[0]

    fields = accumulator.finalize_assistant_fields()
    assert second_delta.payload == {
        "tool_call_id": "call_abc",
        "arguments_delta": second_fragment,
    }
    assert fields.tool_calls == [
        {
            "id": "call_abc",
            "name": "write",
            "arguments": {"path": r"C:\Users\notes.txt"},
        }
    ]


async def test_malformed_tool_arguments_become_rejected_tool_call() -> None:
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

    assert fields.tool_calls is not None
    tool_call = fields.tool_calls[0]
    assert tool_call["arguments"] == {}
    assert tool_call["rejection"]["code"] == "malformed_tool_arguments"
    assert "malformed or incomplete JSON" in tool_call["rejection"]["message"]


async def test_consecutive_streamed_argument_objects_become_sequential_calls() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_batch",
            "name_delta": "bash",
            "arguments_delta": (
                '{"mode":"foreground","command":"echo one"}'
                '{"mode":"foreground","command":"echo two"}'
            ),
        }
    )

    fields = accumulator.finalize_assistant_fields()

    assert fields.tool_calls is not None
    assert [call["arguments"]["command"] for call in fields.tool_calls] == [
        "echo one",
        "echo two",
    ]
    assert [call["argument_sequence_index"] for call in fields.tool_calls] == [0, 1]
    assert all(call["argument_sequence_length"] == 2 for call in fields.tool_calls)
    assert fields.tool_calls[0]["id"] == "call_batch"
    assert fields.tool_calls[1]["id"].startswith("tool_call_recovered_")


async def test_malformed_tool_arguments_rejection_abbreviates_large_fragments() -> None:
    accumulator = StreamingAccumulator()
    huge_fragment = '{"path":"todo.html","content":"' + ("x" * 5000)

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "write",
            "arguments_delta": huge_fragment,
        }
    )

    fields = accumulator.finalize_assistant_fields()

    assert fields.tool_calls is not None
    error_message = fields.tool_calls[0]["rejection"]["message"]
    assert f"{len(huge_fragment)} chars" in error_message
    assert "chars omitted" in error_message
    assert huge_fragment not in error_message
