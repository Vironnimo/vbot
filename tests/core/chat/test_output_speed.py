"""Output speed of a completed step: which tokens over which window, and when there is none."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat._output_speed import complete_output_speed
from core.chat.messages import ChatMessage

JsonObject = dict[str, Any]

ANSWER = "word " * 100
# 2.0 s from the request to the first output; Reasoning streams first when
# there is some, the answer from 4.0 s, the last output at 6.0 s.
MARKS = {
    "first_token_ms": 2000,
    "first_answer_ms": 4000,
    "last_token_ms": 6000,
    "deltas": 80,
    "answer_deltas": 40,
}


def _speed(usage: JsonObject, *, reasoning: str | None = None) -> JsonObject | None:
    message = ChatMessage.assistant(
        content=ANSWER, reasoning=reasoning, model="openai/gpt-5", usage=usage
    )
    completed = complete_output_speed(message)
    assert completed.usage is not None
    assert "stream_timing" not in completed.usage
    assert "reported_output_tokens_per_second" not in completed.usage
    return completed.usage.get("speed")


@pytest.mark.parametrize(
    ("usage", "reasoning", "rate"),
    [
        pytest.param(
            {"output_tokens": 300},
            "thought " * 200,
            {"output_tokens_per_second": 75.0, "tokens": 300, "window_ms": 4000, "basis": "output"},
            id="streamed-reasoning-counts-from-the-first-token",
        ),
        pytest.param(
            {"output_tokens": 2100, "reasoning_tokens": 2000},
            None,
            {"output_tokens_per_second": 50.0, "tokens": 100, "window_ms": 2000, "basis": "answer"},
            id="hidden-reasoning-leaves-the-answer-over-its-window",
        ),
        pytest.param(
            {"output_tokens": 100, "reported_output_tokens_per_second": 61.47},
            None,
            {"output_tokens_per_second": 61.5, "basis": "provider"},
            id="provider-measured-speed-wins",
        ),
    ],
)
def test_speed_divides_the_streamed_tokens_by_their_generation_window(
    usage: JsonObject, reasoning: str | None, rate: JsonObject
) -> None:
    speed = _speed({"input_tokens": 10, **usage, "stream_timing": MARKS}, reasoning=reasoning)

    assert speed == {"first_token_ms": 2000, "first_answer_ms": 4000, "duration_ms": 6000, **rate}


@pytest.mark.parametrize(
    ("output_tokens", "marks"),
    [
        pytest.param(15, MARKS, id="too-few-tokens"),
        pytest.param(100, {**MARKS, "first_token_ms": 5800, "first_answer_ms": 5800}, id="burst"),
        pytest.param(100, {**MARKS, "deltas": 2, "answer_deltas": 2}, id="too-few-deltas"),
    ],
)
def test_an_unreliable_window_keeps_only_the_latency(output_tokens: int, marks: JsonObject) -> None:
    speed = _speed({"input_tokens": 10, "output_tokens": output_tokens, "stream_timing": marks})

    assert speed is not None
    assert "output_tokens_per_second" not in speed
    assert speed["first_token_ms"] == marks["first_token_ms"]


def test_reasoning_counted_without_a_split_leaves_the_estimated_answer() -> None:
    speed = _speed(
        {"input_tokens": 10, "output_tokens": 2100, "stream_timing": MARKS},
        reasoning="A short summary.",
    )

    # Only a summary streamed: the answer's local estimate over its own window.
    assert speed is not None
    assert speed["basis"] == "answer"
    assert speed["estimated"] is True
    assert speed["window_ms"] == 2000
    assert 80 <= speed["tokens"] <= 130
    assert speed["output_tokens_per_second"] == round(speed["tokens"] / 2, 1)
