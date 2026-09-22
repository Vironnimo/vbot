"""Visible summary structure survives streaming without exposing replay state."""

from core.chat.events import _visible_message_payload
from core.chat.streaming import StreamingAccumulator, StreamingDeltaBatcher
from core.chat.wire_shaping import _assistant_continuation_dict, _assistant_message_from_response
from core.providers._responses_output import normalize_responses_response
from core.providers._responses_stream import ResponsesStreamState, normalize_responses_stream_event


def test_summary_sections_stream_backfill_and_replay_are_separate() -> None:
    state = ResponsesStreamState()
    accumulator = StreamingAccumulator()
    visible = []

    def accept(kind, **data):
        for delta in normalize_responses_stream_event(kind, data, state):
            visible.extend(accumulator.add_delta(delta))

    sections = [
        "**Inspecting files**\n\nRead the source.",
        "**Comparing options**\n\nChoose a fix.",
    ]
    for index, section in enumerate(sections):
        accept(
            "response.reasoning_summary_part.added",
            output_index=0,
            summary_index=index,
            part={"type": "summary_text", "text": ""},
        )
        for fragment in [section[:8], section[8:]]:
            accept(
                "response.reasoning_summary_text.delta",
                output_index=0,
                summary_index=index,
                delta=fragment,
            )
        accept(
            "response.reasoning_summary_text.done",
            output_index=0,
            summary_index=index,
            text=section,
        )
    item = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text} for text in sections],
        "encrypted_content": "opaque-sentinel",
    }
    accept("response.output_item.done", output_index=0, item=item)
    accept(
        "response.completed",
        response={
            "status": "completed",
            "output": [
                item,
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Result"}],
                },
            ],
        },
    )
    fields = accumulator.finalize_assistant_fields()
    assert fields.reasoning_summary == sections
    assert fields.reasoning == "\n\n".join(sections)
    assert not accumulator.ends_with_reasoning
    assert state.normalized_response()["reasoning_summary"] == sections
    message = _assistant_message_from_response("test/model", fields.to_response_dict())
    public = _visible_message_payload(message)
    assert public["reasoning_summary"] == sections
    assert "opaque-sentinel" not in str(public)
    replay = _assistant_continuation_dict(message)
    assert "reasoning_summary" not in replay
    assert replay["reasoning_meta"]["response_output"][0] == item
    assert normalize_responses_response({"output": [item]})["reasoning_summary"] == sections

    batcher = StreamingDeltaBatcher(interval_seconds=100)
    batches = []
    batcher.add(visible[-1])  # Start the cadence before collecting the summary fragments.
    for delta in visible:
        batches.extend(batcher.add(delta))
    batches.extend(batcher.flush())
    summary_events = [event for event in batches if "summary_index" in event.payload]
    assert [event.payload["summary_index"] for event in summary_events] == [0, 1]
    assert [event.payload["summary_text"] for event in summary_events] == sections


def test_summary_only_in_terminal_and_multiple_items_keeps_boundaries() -> None:
    state = ResponsesStreamState()
    accumulator = StreamingAccumulator()
    output = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": text}]}
        for text in ["One", "Two"]
    ]
    for delta in normalize_responses_stream_event(
        "response.completed",
        {
            "response": {
                "status": "completed",
                "output": output,
            }
        },
        state,
    ):
        accumulator.add_delta(delta)
    assert accumulator.finalize_partial_fields().reasoning_summary == ["One", "Two"]
    assert accumulator.partial_reasoning == "One\n\nTwo"
