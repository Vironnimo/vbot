"""Output speed of one streamed Model response: time to first token and tokens per second.

Chat times every streamed request attempt from the moment it is sent. The
stream records when output arrived (``OutputTimer``); once the step's output
tokens are known, ``complete_output_speed`` turns those marks into the
step's public ``usage.speed`` (``chat/usage.md`` -> Output speed).

Tokens per second is the Model's generation speed while it was writing, not
request throughput: the window starts at the first output, so queueing and
prompt processing count toward the time to first token instead, and only
output that arrived after the first network read counts, since the first
read's tokens were generated before the window began. Provider behaviors
that would otherwise make the figure absurd:

- Reasoning a Provider counts but never streams (hidden, or only summarized)
  was generated before or between the visible deltas, outside any window
  vBot can see. When the counted output clearly exceeds what was streamed,
  only the answer counts, over the answer's own window.
- Output that arrives in bursts (a buffering proxy, a cached response, a busy
  Event Loop) spans no real generation time. Deltas that follow each other
  within two milliseconds came from one read; a window needs four such
  arrivals, and none after the first may carry half of its output.

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

ARRIVAL_GAP_SECONDS = 0.002
"""Deltas closer together than this arrived with one network read."""

MIN_SPEED_ARRIVALS = 4
MIN_SPEED_WINDOW_MS = 100
MIN_SPEED_TOKENS = 10
MAX_ARRIVAL_SHARE = 0.5
"""No single arrival after the first may carry more of the window's output."""

# Counted output within this ratio of the locally estimated streamed text
# (plus a small allowance for framing) was streamed as it was generated;
# local and Provider tokenizers differ by far less than hidden Reasoning adds.
_STREAMED_COVERAGE_RATIO = 1.5
_STREAMED_COVERAGE_SLACK_TOKENS = 16


class _Arrivals:
    """When one kind of output (all of it, or the answer) arrived, and how much."""

    def __init__(self) -> None:
        self.first: float | None = None
        self.last: float | None = None
        self.chars = 0
        self.first_chars = 0
        self.arrivals = 0
        self.max_arrival_chars = 0
        self._arrival_chars = 0

    def add(self, now: float, chars: int) -> None:
        if self.last is None or now - self.last > ARRIVAL_GAP_SECONDS:
            self.arrivals += 1
            self._arrival_chars = 0
        if self.first is None:
            self.first = now
        self.last = now
        self.chars += chars
        self._arrival_chars += chars
        if self.arrivals == 1:
            self.first_chars += chars
        else:
            self.max_arrival_chars = max(self.max_arrival_chars, self._arrival_chars)

    def marks(self, sent: float) -> JsonObject | None:
        if self.first is None or self.last is None:
            return None
        return {
            "first_ms": _elapsed_ms(sent, self.first),
            "last_ms": _elapsed_ms(sent, self.last),
            "chars": self.chars,
            "first_chars": self.first_chars,
            "arrivals": self.arrivals,
            "max_arrival_chars": self.max_arrival_chars,
        }


class OutputTimer:
    """Monotonic marks of one stream attempt's output deltas.

    ``request_sent`` starts the attempt; every non-empty Reasoning, answer or
    Tool Call delta afterwards is output, answer text and Tool Calls are also
    answer output. ``chars`` is the delta's text length, which apportions the
    counted tokens between the first read and the rest of the window.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self._sent: float | None = None
        self._output = _Arrivals()
        self._answer = _Arrivals()

    def request_sent(self) -> None:
        self._sent = self._clock()

    def output(self, chars: int, *, answer: bool) -> None:
        if self._sent is None:
            return
        now = self._clock()
        self._output.add(now, chars)
        if answer:
            self._answer.add(now, chars)

    def marks(self) -> JsonObject | None:
        """Arrival marks in ms since the request was sent, or None without output."""
        if self._sent is None:
            return None
        output = self._output.marks(self._sent)
        if output is None:
            return None
        marks: JsonObject = {"output": output}
        answer = self._answer.marks(self._sent)
        if answer is not None:
            marks["answer"] = answer
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
        speed["first_token_ms"] = marks["output"]["first_ms"]
        if "answer" in marks:
            speed["first_answer_ms"] = marks["answer"]["first_ms"]
        speed["duration_ms"] = marks["output"]["last_ms"]
    if reported is not None:
        speed.update(_reported_speed(reported, usage))
    elif isinstance(marks, dict):
        speed.update(_measured_speed(marks, usage, message))
    if speed:
        usage[SPEED_FIELD] = speed
    return replace(message, usage=usage)


def reported_speed(value: Any) -> float | None:
    """Return a valid Provider-reported speed (finite, positive), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _reported_speed(rate: float, usage: JsonObject) -> JsonObject:
    speed: JsonObject = {"output_tokens_per_second": round(rate, 1)}
    tokens = _count(usage.get("output_tokens"))
    if tokens and usage.get("output_tokens_estimated") is not True:
        # The window the Provider's own measurement implies, so every speed
        # can be weighted by its tokens alike.
        speed.update(tokens=tokens, window_ms=max(1, round(tokens * 1000 / rate)))
    speed["basis"] = "provider"
    return speed


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
        arrivals = marks["output"]
    else:
        arrivals = marks.get("answer")
        if arrivals is None:
            return {}
        basis = "answer"
        reasoning_tokens = _count(usage.get("reasoning_tokens"))
        if not estimated and reasoning_tokens is not None and output_tokens > reasoning_tokens:
            tokens = output_tokens - reasoning_tokens
        else:
            tokens = answer_tokens
            estimated = True
    window_ms = arrivals["last_ms"] - arrivals["first_ms"]
    window_chars = arrivals["chars"] - arrivals["first_chars"]
    if (
        arrivals["arrivals"] < MIN_SPEED_ARRIVALS
        or window_ms < MIN_SPEED_WINDOW_MS
        or window_chars <= 0
        or arrivals["max_arrival_chars"] > window_chars * MAX_ARRIVAL_SHARE
    ):
        return {}
    # The first read's share of the output was generated before the window.
    window_tokens = tokens * window_chars / arrivals["chars"]
    if window_tokens < MIN_SPEED_TOKENS:
        return {}
    speed: JsonObject = {
        "output_tokens_per_second": round(window_tokens * 1000 / window_ms, 1),
        "tokens": round(window_tokens),
        "window_ms": window_ms,
        "basis": basis,
    }
    if estimated:
        speed["estimated"] = True
    return speed


def _elapsed_ms(start: float, moment: float) -> int:
    return max(0, round((moment - start) * 1000))


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
