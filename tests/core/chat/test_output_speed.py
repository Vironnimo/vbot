"""Output speed of a completed step: which tokens over which window, and when there is none."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat._output_speed import complete_output_speed
from core.chat.messages import ChatMessage

JsonObject = dict[str, Any]

ANSWER = "word " * 100


def _arrivals(
    first_ms: int,
    last_ms: int,
    *,
    chars: int,
    first_chars: int,
    arrivals: int = 40,
    max_arrival_chars: int = 30,
) -> JsonObject:
    return {
        "first_ms": first_ms,
        "last_ms": last_ms,
        "chars": chars,
        "first_chars": first_chars,
        "arrivals": arrivals,
        "max_arrival_chars": max_arrival_chars,
    }


# Output from 2.0 s (Reasoning first, when there is some), the answer from
# 4.0 s, the last output at 6.0 s; the first read carries a fifth of each.
MARKS = {
    "output": _arrivals(2000, 6000, chars=1000, first_chars=200),
    "answer": _arrivals(4000, 6000, chars=500, first_chars=100),
}


def _speed(
    usage: JsonObject, *, marks: JsonObject = MARKS, reasoning: str | None = None
) -> JsonObject | None:
    completed = complete_output_speed(
        ChatMessage.assistant(
            content=ANSWER,
            reasoning=reasoning,
            model="openai/gpt-5",
            usage={"input_tokens": 10, **usage, "stream_timing": marks},
        )
    )
    assert completed.usage is not None
    assert "stream_timing" not in completed.usage
    assert "reported_output_tokens_per_second" not in completed.usage
    return completed.usage.get("speed")


@pytest.mark.parametrize(
    ("usage", "reasoning", "marks", "rate"),
    [
        pytest.param(
            {"output_tokens": 300},
            "thought " * 200,
            MARKS,
            {"output_tokens_per_second": 60.0, "tokens": 240, "window_ms": 4000, "basis": "output"},
            id="streamed-reasoning-counts-after-the-first-read",
        ),
        pytest.param(
            {"output_tokens": 2100, "reasoning_tokens": 2000},
            None,
            MARKS,
            {"output_tokens_per_second": 40.0, "tokens": 80, "window_ms": 2000, "basis": "answer"},
            id="hidden-reasoning-leaves-the-answer-over-its-window",
        ),
        pytest.param(
            {"output_tokens": 120},
            None,
            {
                "output": _arrivals(400, 550, chars=1000, first_chars=200),
                "answer": _arrivals(400, 550, chars=1000, first_chars=200),
            },
            {"output_tokens_per_second": 640.0, "tokens": 96, "window_ms": 150, "basis": "output"},
            id="a-fast-model-within-a-short-window",
        ),
        pytest.param(
            {"output_tokens": 100, "reported_output_tokens_per_second": 61.47},
            None,
            MARKS,
            {
                "output_tokens_per_second": 61.5,
                "tokens": 100,
                "window_ms": 1627,
                "basis": "provider",
            },
            id="provider-measured-speed-wins",
        ),
    ],
)
def test_speed_divides_the_window_tokens_by_their_generation_window(
    usage: JsonObject, reasoning: str | None, marks: JsonObject, rate: JsonObject
) -> None:
    speed = _speed(usage, marks=marks, reasoning=reasoning)

    assert speed == {
        "first_token_ms": marks["output"]["first_ms"],
        "first_answer_ms": marks["answer"]["first_ms"],
        "duration_ms": marks["output"]["last_ms"],
        **rate,
    }


def _output_only(**changes: Any) -> JsonObject:
    output = {**MARKS["output"], **changes}
    return {"output": output, "answer": {**output}}


@pytest.mark.parametrize(
    ("output_tokens", "marks"),
    [
        pytest.param(12, MARKS, id="too-few-tokens"),
        pytest.param(100, _output_only(arrivals=1, max_arrival_chars=0), id="one-burst"),
        pytest.param(100, _output_only(arrivals=3), id="too-few-arrivals"),
        pytest.param(100, _output_only(max_arrival_chars=500), id="most-output-in-one-read"),
        pytest.param(100, _output_only(first_ms=5950), id="window-too-short"),
    ],
)
def test_an_unreliable_window_keeps_only_the_latency(output_tokens: int, marks: JsonObject) -> None:
    speed = _speed({"output_tokens": output_tokens}, marks=marks)

    assert speed is not None
    assert "output_tokens_per_second" not in speed
    assert speed["first_token_ms"] == marks["output"]["first_ms"]


def test_reasoning_counted_without_a_split_leaves_the_estimated_answer() -> None:
    speed = _speed({"output_tokens": 2100}, reasoning="A short summary.")

    # Only a summary streamed: the answer's local estimate over its own window.
    assert speed is not None
    assert speed["basis"] == "answer"
    assert speed["estimated"] is True
    assert speed["window_ms"] == 2000
    assert 60 <= speed["tokens"] <= 110
