"""Tests for provider-agnostic streaming helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.chat.streaming import (
    StreamingAccumulator,
    StreamingAssistantFields,
    StreamingChunkTimeoutError,
    StreamingDeltaError,
    StreamRecoveryAction,
    decide_stream_recovery,
    is_local_provider_base_url,
    iter_with_chunk_timeout,
)
from core.providers.errors import (
    NetworkError,
    ProviderStreamingUnsupportedError,
    ProviderTimeoutError,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
)
from core.utils.errors import ProviderError

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


async def test_malformed_tool_arguments_raise_visible_streaming_error() -> None:
    accumulator = StreamingAccumulator()

    accumulator.add_delta(
        {
            "type": "tool_call_delta",
            "id": "call_abc",
            "name_delta": "read_file",
            "arguments_delta": '{"path":',
        }
    )

    with pytest.raises(StreamingDeltaError, match="malformed or incomplete arguments"):
        accumulator.finalize_assistant_fields()


async def test_malformed_tool_arguments_error_abbreviates_large_fragments() -> None:
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

    with pytest.raises(StreamingDeltaError) as exc_info:
        accumulator.finalize_assistant_fields()

    error_message = str(exc_info.value)
    assert f"{len(huge_fragment)} chars" in error_message
    assert "chars omitted" in error_message
    assert huge_fragment not in error_message


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


async def test_accumulator_keeps_cache_token_fields_from_usage_delta() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta(
        {
            "type": "usage",
            "input_tokens": 250,
            "output_tokens": 80,
            "cache_read_tokens": 200,
            "cache_write_tokens": 30,
        }
    )

    assert visible == []
    fields = accumulator.finalize_assistant_fields()
    assert fields.usage == {
        "input_tokens": 250,
        "output_tokens": 80,
        "cache_read_tokens": 200,
        "cache_write_tokens": 30,
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
        }
    )

    fields = accumulator.finalize_assistant_fields()
    assert fields.usage == {"input_tokens": 250, "output_tokens": 80}


async def test_usage_delta_rejects_non_integer_tokens() -> None:
    accumulator = StreamingAccumulator()

    with pytest.raises(StreamingDeltaError, match="integer"):
        accumulator.add_delta({"type": "usage", "input_tokens": "bad", "output_tokens": 10})

    with pytest.raises(StreamingDeltaError, match="integer"):
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


async def test_decide_recovery_streaming_unsupported_before_visible_falls_back() -> None:
    action = decide_stream_recovery(
        ProviderStreamingUnsupportedError("no streaming"),
        emitted_visible_delta=False,
        can_restart=True,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.FALLBACK


async def test_decide_recovery_restartable_transient_before_visible_restarts() -> None:
    for error in (
        NetworkError("dropped"),
        ProviderTimeoutError("slow"),
        StreamingChunkTimeoutError("stalled"),
        ProviderError("overloaded", retryable=True),
    ):
        action = decide_stream_recovery(
            error,
            emitted_visible_delta=False,
            can_restart=True,
            has_partial_content=False,
        )

        assert action is StreamRecoveryAction.RESTART, type(error).__name__


async def test_decide_recovery_restartable_before_visible_fails_when_budget_exhausted() -> None:
    action = decide_stream_recovery(
        NetworkError("dropped"),
        emitted_visible_delta=False,
        can_restart=False,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.FAIL


async def test_decide_recovery_non_restartable_before_visible_fails() -> None:
    action = decide_stream_recovery(
        ProviderError("fatal", retryable=False),
        emitted_visible_delta=False,
        can_restart=True,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.FAIL


async def test_decide_recovery_visible_with_content_preserves_partial() -> None:
    for error in (
        NetworkError("dropped"),
        StreamingChunkTimeoutError("stalled"),
        ProviderError("overloaded", retryable=True),
        ProviderStreamingUnsupportedError("no streaming"),
    ):
        action = decide_stream_recovery(
            error,
            emitted_visible_delta=True,
            can_restart=True,
            has_partial_content=True,
        )

        assert action is StreamRecoveryAction.PRESERVE_PARTIAL, type(error).__name__


async def test_decide_recovery_visible_reasoning_only_discards_with_note() -> None:
    action = decide_stream_recovery(
        NetworkError("dropped"),
        emitted_visible_delta=True,
        can_restart=True,
        has_partial_content=False,
    )

    assert action is StreamRecoveryAction.DISCARD_WITH_NOTE


async def test_local_provider_base_url_detects_loopback_and_local_names() -> None:
    for url in (
        "http://localhost:11434",
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "http://ollama.local:11434",
        "http://box.localhost/v1",
    ):
        assert is_local_provider_base_url(url) is True, url


async def test_local_provider_base_url_detects_private_and_link_local_ips() -> None:
    for url in (
        "http://10.0.0.5:1234",
        "http://172.16.4.2:1234",
        "http://192.168.1.50:11434",
        "http://169.254.10.10:1234",
    ):
        assert is_local_provider_base_url(url) is True, url


async def test_local_provider_base_url_rejects_public_hosts() -> None:
    for url in (
        "https://api.openai.com/v1",
        "https://api.anthropic.com",
        "http://8.8.8.8:443",
    ):
        assert is_local_provider_base_url(url) is False, url


async def test_local_provider_base_url_rejects_missing_or_unparseable() -> None:
    assert is_local_provider_base_url(None) is False
    assert is_local_provider_base_url("") is False
    assert is_local_provider_base_url("not a url") is False


async def test_iter_with_chunk_timeout_disabled_never_aborts_on_silence() -> None:
    async def source() -> AsyncIteratorForTest:
        yield {"type": "content_delta", "text": "first"}
        await asyncio.sleep(0.02)
        yield {"type": "content_delta", "text": "second"}

    chunks = [chunk async for chunk in iter_with_chunk_timeout(source(), timeout_seconds=None)]

    assert chunks == [
        {"type": "content_delta", "text": "first"},
        {"type": "content_delta", "text": "second"},
    ]


AsyncIteratorForTest = Any
