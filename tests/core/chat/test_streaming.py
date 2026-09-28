"""Stream accumulation: visible deltas, final Assistant fields, Tool Call fragments and usage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import streaming as streaming_module
from core.chat.streaming import (
    StreamingAccumulator,
    StreamingDeltaBatcher,
    StreamingDeltaError,
    StreamingVisibleDelta,
)
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
)
from core.tools import model_names

JsonObject = dict[str, Any]


def _tool_delta(arguments: str, *, name: str = "", **identity: Any) -> JsonObject:
    return {"type": "tool_call_delta", "name_delta": name, "arguments_delta": arguments, **identity}


def _accumulate(*deltas: JsonObject) -> StreamingAccumulator:
    accumulator = StreamingAccumulator()
    for delta in deltas:
        accumulator.add_delta(delta)
    return accumulator


def _output(text: str) -> StreamingVisibleDelta:
    return StreamingVisibleDelta(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": text})


def test_accumulates_visible_deltas_in_provider_order() -> None:
    accumulator = StreamingAccumulator()

    emitted = [
        visible
        for delta in (
            {"type": "reasoning_delta", "text": "Think"},
            {"type": "content_delta", "text": "Hello"},
            _tool_delta('{"path":"notes.md"}', name="read", id="call_abc"),
            {"type": "content_delta", "text": " world"},
        )
        for visible in accumulator.add_delta(delta)
    ]
    fields = accumulator.finalize_assistant_fields()

    order = [
        REASONING_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        TOOL_CALL_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
    ]
    assert [delta.event_type for delta in emitted] == order
    assert (fields.content, fields.reasoning) == ("Hello world", "Think")


def test_partial_text_and_final_readable_phase_follow_each_channel() -> None:
    accumulator = StreamingAccumulator()
    assert (accumulator.partial_content, accumulator.partial_reasoning) == (None, None)

    accumulator.add_delta({"type": "reasoning_delta", "text": "Plan"})
    assert accumulator.partial_content is None
    assert accumulator.ends_with_reasoning is True

    accumulator.add_delta({"type": "content_delta", "text": "Hello"})
    accumulator.add_delta({"type": "content_delta", "text": " world"})
    assert accumulator.ends_with_reasoning is False

    accumulator.add_delta({"type": "reasoning_delta", "text": " misrouted tail"})
    assert accumulator.ends_with_reasoning is True
    assert accumulator.partial_content == "Hello world"
    assert accumulator.partial_reasoning == "Plan misrouted tail"


def test_batches_adjacent_visible_deltas_at_bounded_cadence() -> None:
    batcher = StreamingDeltaBatcher(interval_seconds=0.04)

    first = batcher.add(_output("Hel"), now=10.0)
    second = batcher.add(_output("lo"), now=10.01)
    due = batcher.add(_output("!"), now=10.04)

    assert [delta.payload for delta in first] == [{"content_delta": "Hel"}]
    assert second == []
    assert [delta.payload for delta in due] == [{"content_delta": "lo!"}]
    assert batcher.flush() == []


def test_batcher_preserves_delta_order_and_tool_call_identity() -> None:
    batcher = StreamingDeltaBatcher(interval_seconds=1.0)
    for now, event_type, payload in (
        (1.0, REASONING_DELTA_EVENT, {"reasoning_delta": "First"}),
        (1.1, TOOL_CALL_DELTA_EVENT, {"tool_call_id": "one", "name_delta": "re"}),
        (1.2, TOOL_CALL_DELTA_EVENT, {"tool_call_id": "one", "name_delta": "ad"}),
        (1.3, TOOL_CALL_DELTA_EVENT, {"tool_call_id": "two", "arguments_delta": "{}"}),
    ):
        batcher.add(StreamingVisibleDelta(event_type, payload), now=now)

    assert [(delta.event_type, delta.payload) for delta in batcher.flush()] == [
        (TOOL_CALL_DELTA_EVENT, {"tool_call_id": "one", "name_delta": "read"}),
        (TOOL_CALL_DELTA_EVENT, {"tool_call_id": "two", "arguments_delta": "{}"}),
    ]


def test_tool_only_response_finalizes_without_text_and_keeps_its_finish_reason() -> None:
    accumulator = StreamingAccumulator()
    accumulator.add_delta(_tool_delta('{"city":"Berlin"}', name="get_weather", id="call_abc"))

    finish_visible = accumulator.add_delta({"type": "finish", "reason": "tool_calls"})
    fields = accumulator.finalize_assistant_fields()

    assert finish_visible == []
    assert (fields.content, fields.reasoning, fields.finish_reason) == (None, None, "tool_calls")
    assert fields.tool_calls == [
        {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]


def test_finalized_streamed_tool_calls_keep_stable_indexes_in_arrival_order() -> None:
    fields = _accumulate(
        _tool_delta('{"path":"one.md"}', name="read", id="call_first"),
        _tool_delta('{"path":"two.md"}', name="read", id="call_second"),
        _tool_delta("", id="call_first"),
    ).finalize_assistant_fields()

    assert fields.tool_calls == [
        {"id": "call_first", "name": "read", "arguments": {"path": "one.md"}},
        {"id": "call_second", "name": "read", "arguments": {"path": "two.md"}},
    ]


def test_late_provider_id_replaces_the_synthesized_identity_of_its_stream_slot() -> None:
    accumulator = StreamingAccumulator()

    [first_visible] = accumulator.add_delta(_tool_delta('{"query":"', name="search", slot=0))
    [second_visible] = accumulator.add_delta(
        _tool_delta('Berlin"}', slot=0, id="call_provider_late")
    )

    assert first_visible.payload["tool_call_id"] == "tool_call_0"
    assert second_visible.payload["tool_call_id"] == "call_provider_late"
    assert accumulator.finalize_assistant_fields().tool_calls == [
        {"id": "call_provider_late", "name": "search", "arguments": {"query": "Berlin"}}
    ]


def test_interleaved_stream_slots_merge_independently() -> None:
    fields = _accumulate(
        _tool_delta('{"path":"', name="write", slot=0),
        _tool_delta('{"path":"README', name="read", slot=3),
        _tool_delta('notes.md"}', slot=0, id="call_write"),
        _tool_delta('.md"}', slot=3),
    ).finalize_assistant_fields()

    # A slot whose provider never supplied an id gets a stable synthesized one.
    assert fields.tool_calls == [
        {"id": "call_write", "name": "write", "arguments": {"path": "notes.md"}},
        {"id": "tool_call_3", "name": "read", "arguments": {"path": "README.md"}},
    ]


def test_tool_call_name_deltas_carry_registry_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_names, "_REGISTRY_NAMES", {"host_probe": "probe"})

    emitted = StreamingAccumulator().add_delta(_tool_delta("", name="host_probe", id="call_1"))

    assert [delta.payload["name_delta"] for delta in emitted] == ["probe"]


@pytest.mark.parametrize(
    ("fragments", "arguments"),
    [
        (['{"path":"', 'notes.md"}'], {"path": "notes.md"}),
        # Adapters reconcile wire snapshots; a delta repeating everything so far is new content.
        (['{"value":', '{"value":', "1}}"], {"value": {"value": 1}}),
        (['{"pattern":"abc","value":"', "abc", '"}'], {"pattern": "abc", "value": "abc"}),
        (['{"value":"ab', 'ab"}'], {"value": "abab"}),
        (['{"command":"echo \\"', '"}'], {"command": 'echo "'}),
        (['{"path":"C:\\', '\\Users\\\\notes.txt"}'], {"path": r"C:\Users\notes.txt"}),
    ],
    ids=["split", "prefix-repeat", "inner-repeat", "boundary-repeat", "escaped-quote", "backslash"],
)
def test_tool_argument_fragments_are_appended_verbatim_and_parsed_at_finalization(
    fragments: list[str], arguments: JsonObject
) -> None:
    accumulator = StreamingAccumulator()

    visible = [
        accumulator.add_delta(
            _tool_delta(fragment, name="write" if index == 0 else "", id="call_abc")
        )[0]
        for index, fragment in enumerate(fragments)
    ]

    assert [delta.payload["arguments_delta"] for delta in visible] == fragments
    assert all("arguments" not in delta.payload for delta in visible)
    assert accumulator.finalize_assistant_fields().tool_calls == [
        {"id": "call_abc", "name": "write", "arguments": arguments}
    ]


@pytest.mark.parametrize(
    "fragments",
    [
        ['{"command":"echo one"}', '{"command":"echo two"}'],
        ['{"command":"echo one"}{"command":"echo two"}'],
    ],
    ids=["identical-second-fragment", "one-fragment"],
)
def test_consecutive_argument_objects_become_sequential_sibling_calls(
    fragments: list[str],
) -> None:
    accumulator = StreamingAccumulator()
    for index, fragment in enumerate(fragments):
        accumulator.add_delta(
            _tool_delta(fragment, name="bash" if index == 0 else "", id="call_batch")
        )

    tool_calls = accumulator.finalize_assistant_fields().tool_calls

    assert tool_calls is not None
    assert [call["arguments"]["command"] for call in tool_calls] == ["echo one", "echo two"]
    assert [call["argument_sequence_index"] for call in tool_calls] == [0, 1]
    assert all(call["argument_sequence_length"] == 2 for call in tool_calls)
    assert tool_calls[0]["id"] == "call_batch"
    assert tool_calls[1]["id"].startswith("tool_call_recovered_")
    assert all("rejection" not in call for call in tool_calls)


@pytest.mark.parametrize("size", [10, 5000], ids=["short", "abbreviated"])
def test_malformed_tool_arguments_become_a_rejected_call(size: int) -> None:
    fragment = '{"path":"todo.html","content":"' + "x" * size

    [tool_call] = (
        _accumulate(_tool_delta(fragment, name="write", id="call_abc"))
        .finalize_assistant_fields()
        .tool_calls
        or []
    )

    message = tool_call["rejection"]["message"]
    assert tool_call["arguments"] == {}
    assert tool_call["rejection"]["code"] == "malformed_tool_arguments"
    assert "malformed or incomplete JSON" in message
    assert (fragment in message) is (size == 10)
    if size == 5000:
        assert f"{len(fragment)} chars" in message
        assert "chars omitted" in message


def test_partial_fields_drop_an_in_flight_tool_call_without_raising() -> None:
    fields = _accumulate(
        {"type": "reasoning_delta", "text": "Working"},
        {"type": "content_delta", "text": "Here is the"},
        _tool_delta('{"path":', name="write", id="call_abc"),
    ).finalize_partial_fields()

    assert (fields.content, fields.reasoning) == ("Here is the", "Working")
    assert (fields.tool_calls, fields.finish_reason) == (None, None)
    assert fields.reasoning_timing is not None


def test_reasoning_meta_merges_without_a_public_delta() -> None:
    accumulator = StreamingAccumulator()

    visible = accumulator.add_delta(
        {"type": "reasoning_meta", "reasoning_meta": {"signature": "opaque"}}
    )
    accumulator.add_delta(
        {"type": "reasoning_meta", "reasoning_meta": {"encrypted_content": "opaque-too"}}
    )

    assert visible == []
    assert accumulator.finalize_assistant_fields().reasoning_meta == {
        "signature": "opaque",
        "encrypted_content": "opaque-too",
    }


def _reasoning_item(item_id: str, **fields: Any) -> JsonObject:
    return {"type": "reasoning", "id": item_id, **fields}


def _summary(text: str) -> list[JsonObject]:
    return [{"type": "summary_text", "text": text}]


def test_reasoning_meta_item_lists_accumulate_and_keep_encrypted_content() -> None:
    fields = _accumulate(
        {
            "type": "reasoning_meta",
            "reasoning_meta": {"reasoning_items": [_reasoning_item("rs_1", summary=_summary("1"))]},
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {"reasoning_items": [_reasoning_item("rs_2", summary=_summary("2"))]},
        },
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "reasoning_items": [_reasoning_item("rs_1", encrypted_content="secret-1")],
                "response_output": [
                    _reasoning_item("rs_1", encrypted_content="secret-1", summary=_summary("1")),
                    _reasoning_item("rs_2", encrypted_content="secret-2", summary=_summary("2")),
                ],
            },
        },
    ).finalize_assistant_fields()

    assert fields.reasoning_meta is not None
    assert fields.reasoning_meta["reasoning_items"] == [
        _reasoning_item("rs_1", summary=_summary("1"), encrypted_content="secret-1"),
        _reasoning_item("rs_2", summary=_summary("2")),
    ]
    output = fields.reasoning_meta["response_output"]
    assert [item["encrypted_content"] for item in output] == ["secret-1", "secret-2"]


def test_reasoning_timing_spans_first_to_last_non_empty_reasoning_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(streaming_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    accumulator = StreamingAccumulator()
    accumulator.add_delta({"type": "content_delta", "text": "Answer"})
    assert accumulator.reasoning_timing is None

    accumulator.add_delta({"type": "reasoning_delta", "text": "Think"})
    clock[0] += 0.25
    accumulator.add_delta({"type": "reasoning_delta", "text": " more"})
    clock[0] += 5
    # Empty reasoning fragments carry no text and must not move the window.
    accumulator.add_delta({"type": "reasoning_delta", "text": ""})

    timing = accumulator.reasoning_timing
    assert timing is not None
    assert set(timing) == {"started_at", "completed_at", "duration_ms"}
    assert timing["duration_ms"] == 250
    assert timing["started_at"] <= timing["completed_at"]
    assert accumulator.finalize_assistant_fields().reasoning_timing == timing


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 250, "output_tokens": 80},
        {"output_tokens": 2572},
        {
            "input_tokens": 250,
            "output_tokens": 80,
            "cache_read_tokens": 200,
            "cache_write_tokens": 30,
            "reasoning_tokens": 50,
        },
    ],
    ids=["counts", "partial", "token-details"],
)
def test_usage_delta_reaches_the_final_response_without_a_visible_delta(usage: JsonObject) -> None:
    accumulator = StreamingAccumulator()
    accumulator.add_delta({"type": "content_delta", "text": "Hello"})

    visible = accumulator.add_delta({"type": "usage", **usage})
    accumulator.add_delta({"type": "finish", "reason": "stop"})
    fields = accumulator.finalize_assistant_fields()

    assert visible == []
    assert fields.usage == usage
    response = fields.to_response_dict()
    assert response["usage"] == usage
    assert response["terminal_outcome"] == "stop"


def test_usage_drops_invalid_optional_token_details() -> None:
    fields = _accumulate(
        {
            "type": "usage",
            "input_tokens": 250,
            "output_tokens": 80,
            "cache_read_tokens": None,
            "cache_write_tokens": "bad",
            "reasoning_tokens": -1,
        }
    ).finalize_assistant_fields()

    assert fields.usage == {"input_tokens": 250, "output_tokens": 80}


@pytest.mark.parametrize(
    "usage",
    [{"input_tokens": "bad", "output_tokens": 10}, {"input_tokens": 10, "output_tokens": "bad"}],
    ids=["input", "output"],
)
def test_usage_delta_rejects_non_integer_token_counts(usage: JsonObject) -> None:
    with pytest.raises(StreamingDeltaError):
        StreamingAccumulator().add_delta({"type": "usage", **usage})


def test_response_without_usage_delta_carries_no_usage() -> None:
    fields = _accumulate(
        {"type": "content_delta", "text": "Hi"}, {"type": "finish", "reason": "stop"}
    ).finalize_assistant_fields()

    assert fields.usage is None
    assert "usage" not in fields.to_response_dict()
