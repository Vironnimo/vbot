"""Bounded exact-period detection for one streamed text channel.

This deliberately ignores similar wording: a long suffix must repeat exactly,
so changing table rows, source lines and log counters remain ordinary output.
"""

from __future__ import annotations

from dataclasses import dataclass

_MIN_REPEAT_CHARS = 16_384
_MAX_PERIOD_CHARS = 2_048
_MIN_REPETITIONS = 8
_WINDOW_CHARS = 32_768
_CHECK_STRIDE = 4_096


@dataclass(frozen=True)
class RepetitionSpan:
    start: int
    period: int


class StreamRepetitionGuard:
    """Inspect bounded suffixes at a fixed character cadence, never per token."""

    def __init__(self) -> None:
        self._total = 0
        self._next_check = _MIN_REPEAT_CHARS
        self._tail = ""
        self._pending: list[str] = []

    def feed(self, text: str) -> RepetitionSpan | None:
        self._total += len(text)
        self._pending.append(text[-_WINDOW_CHARS:])
        if self._total < self._next_check:
            return None
        self._tail = (self._tail + "".join(self._pending))[-_WINDOW_CHARS:]
        self._pending.clear()
        self._next_check = self._total + _CHECK_STRIDE
        # A periodic suffix is a periodic prefix when read backwards. The
        # prefix function gives each prefix's shortest period in linear time;
        # unlike shared-fragment counts it cannot mistake varying rows for a loop.
        reverse = self._tail[::-1]
        borders = [0] * len(reverse)
        for index in range(1, len(reverse)):
            border = borders[index - 1]
            while border and reverse[index] != reverse[border]:
                border = borders[border - 1]
            if reverse[index] == reverse[border]:
                border += 1
            borders[index] = border
        for length in range(len(reverse), _MIN_REPEAT_CHARS - 1, -1):
            period = length - borders[length - 1]
            if period <= _MAX_PERIOD_CHARS and length >= period * _MIN_REPETITIONS:
                return RepetitionSpan(self._total - length, period)
        return None


def before_repetition(parts: list[str], span: RepetitionSpan) -> str:
    """Keep useful output before the exact run, including for one huge delta.

    Detection remains bounded; trimming walks the already retained output once
    only after a hit, so a single large delta cannot leave older loop copies behind.
    """
    text = "".join(parts)
    start = span.start
    while start and text[start - 1] == text[start - 1 + span.period]:
        start -= 1
    return text[:start]
