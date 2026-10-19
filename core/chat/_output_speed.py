"""Output speed of one streamed Model response: time to first token and tokens per second.

Chat times every streamed request attempt from the moment it is sent. The
stream records when output arrived (``OutputTimer``); once the step's output
tokens are known, ``complete_output_speed`` turns those marks into the
step's public ``usage.speed`` (``chat/usage.md`` -> Output speed).

Tokens per second is the Model's generation speed while it was writing, not
request throughput: the window starts at the first output token, so queueing
and prompt processing count toward the time to first token instead. Two
Provider behaviors would otherwise make the figure absurd:

- Reasoning a Provider counts but never streams (hidden, or only summarized)
  was generated before or between the visible deltas, outside any window
  vBot can see. When the counted output clearly exceeds what was streamed,
  only the answer counts, over the answer's own window.
- Deltas that arrive in one burst (a buffering proxy, a cached response)
  span no real generation time. A window shorter than half a second, with
  fewer than three deltas or under twenty tokens yields no speed at all.

A speed the Provider measured itself (local servers report their decode
speed) takes precedence over the stream measurement.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from core.chat.messages import ChatMessage, JsonObject
from core.utils.tokens import estimate_message_tokens, estimate_tokens

SPEED_FIELD = "speed"
"""Usage field of the finished step's output speed."""

STREAM_TIMING_FIELD = "stream_timing"
"""Usage field carrying the raw stream marks until the step is completed."""

REPORTED_SPEED_FIELD = "reported_output_tokens_per_second"
"""Normalized Usage field for a decode speed the Provider measured itself."""

MIN_SPEED_TOKENS = 20
MIN_SPEED_WINDOW_MS = 500
MIN_SPEED_DELTAS = 3

# Counted output within this ratio of the locally estimated streamed text
# (plus a small allowance for framing) was streamed as it was generated;
# local and Provider tokenizers differ by far less than hidden Reasoning adds.
_STREAMED_COVERAGE_RATIO = 1.5
_STREAMED_COVERAGE_SLACK_TOKENS = 16


class OutputTimer:
    """Monotonic marks of one stream attempt's output deltas.

    ``request_sent`` starts the attempt; every non-empty Reasoning, answer or
    Tool Call delta afterwards is output, answer text and Tool Calls are also
    answer output.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self._sent: float | None = None
        self._first: float | None = None
        self._first_answer: float | None = None
        self._last: float | None = None
        self._deltas = 0
        self._answer_deltas = 0

    def request_sent(self) -> None:
        self._sent = self._clock()

    def output(self, *, answer: bool) -> None:
        if self._sent is None:
            return
        now = self._clock()
        if self._first is None:
            self._first = now
        self._last = now
        self._deltas += 1
        if answer:
            if self._first_answer is None:
                self._first_answer = now
            self._answer_deltas += 1

    def marks(self) -> JsonObject | None:
        """Milliseconds since the request was sent, or None without output."""
        sent, first, last = self._sent, self._first, self._last
        if sent is None or first is None or last is None:
            return None

        def elapsed_ms(moment: float) -> int:
            return max(0, round((moment - sent) * 1000))

        marks: JsonObject = {
            "first_token_ms": elapsed_ms(first),
            "last_token_ms": elapsed_ms(last),
            "deltas": self._deltas,
        }
        if self._first_answer is not None:
            marks["first_answer_ms"] = elapsed_ms(self._first_answer)
            marks["answer_deltas"] = self._answer_deltas
        return marks


def complete_output_speed(message: ChatMessage) -> ChatMessage:
    """Replace the raw stream marks of a completed step with its ``usage.speed``.

    Needs the step's final output tokens, so it runs after Usage estimation.
    A step without stream marks or a reported speed is returned unchanged.
    """
    usage = message.usage
    if not usage or (STREAM_TIMING_FIELD not in usage and REPORTED_SPEED_FIELD not in usage):
        return message
    usage = dict(usage)
    marks = usage.pop(STREAM_TIMING_FIELD, None)
    reported = reported_speed(usage.pop(REPORTED_SPEED_FIELD, None))
    speed: JsonObject = {}
    if isinstance(marks, dict):
        speed["first_token_ms"] = marks["first_token_ms"]
        if "first_answer_ms" in marks:
            speed["first_answer_ms"] = marks["first_answer_ms"]
        speed["duration_ms"] = marks["last_token_ms"]
    if reported is not None:
        speed["output_tokens_per_second"] = round(reported, 1)
        speed["basis"] = "provider"
    elif isinstance(marks, dict):
        speed.update(_measured_speed(marks, usage, message))
    if speed:
        usage[SPEED_FIELD] = speed
    return replace(message, usage=usage)


def _measured_speed(marks: JsonObject, usage: JsonObject, message: ChatMessage) -> JsonObject:
    output_tokens = _count(usage.get("output_tokens"))
    if output_tokens is None:
        return {}
    answer_tokens, _ = estimate_message_tokens(
        {key: value for key, value in message.to_dict().items() if key in ("content", "tool_calls")}
    )
    streamed_tokens = answer_tokens
    if message.reasoning:
        streamed_tokens += estimate_tokens(message.reasoning)[0]
    estimated = usage.get("output_tokens_estimated") is True
    if (
        output_tokens
        <= streamed_tokens * _STREAMED_COVERAGE_RATIO + _STREAMED_COVERAGE_SLACK_TOKENS
    ):
        basis = "output"
        tokens = output_tokens
        window_ms = marks["last_token_ms"] - marks["first_token_ms"]
        deltas = marks["deltas"]
    else:
        if "first_answer_ms" not in marks:
            return {}
        basis = "answer"
        reasoning_tokens = _count(usage.get("reasoning_tokens"))
        if not estimated and reasoning_tokens is not None and output_tokens > reasoning_tokens:
            tokens = output_tokens - reasoning_tokens
        else:
            tokens = answer_tokens
            estimated = True
        window_ms = marks["last_token_ms"] - marks["first_answer_ms"]
        deltas = marks["answer_deltas"]
    if tokens < MIN_SPEED_TOKENS or window_ms < MIN_SPEED_WINDOW_MS or deltas < MIN_SPEED_DELTAS:
        return {}
    speed: JsonObject = {
        "output_tokens_per_second": round(tokens * 1000 / window_ms, 1),
        "tokens": tokens,
        "window_ms": window_ms,
        "basis": basis,
    }
    if estimated:
        speed["estimated"] = True
    return speed


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def reported_speed(value: Any) -> float | None:
    """Return a valid Provider-reported speed (finite, positive), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)
