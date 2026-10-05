"""Split a long dictation into pieces at speech pauses.

A dictation has no length limit, but one upload to the speech server does.
:class:`PieceCutter` therefore closes a piece once it holds
``target_seconds`` of audio and a pause follows, so each piece can be
transcribed while the user keeps speaking, and no word is cut in two. Without
a pause within ``search_seconds`` (or when the next block would exceed the
upload budget) it cuts at the quietest moment it still holds. A change of the
recording rate closes the piece at once, since one WAV has one rate. Every
block ends up in exactly one piece; nothing is dropped.

Loudness is the RMS of each capture block. A block counts as quiet when it
stays below twice the piece's noise floor (a low percentile of its block
loudness), but never below an absolute floor for digital silence. numpy is
imported on first use, so importing this module keeps Desktop startup light.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from desktop.speech.server_client import WAV_HEADER_BYTES

PIECE_TARGET_SECONDS = 90.0
"""A piece looks for a pause to end at once it holds this much audio."""
PIECE_SEARCH_SECONDS = 30.0
"""How long a piece waits for a pause before it cuts at its quietest moment."""
PAUSE_SECONDS = 0.3
"""How long the audio must stay quiet to count as a pause between words."""

_NOISE_FLOOR_PERCENTILE = 10
_QUIET_FACTOR = 2.0
_MIN_QUIET_RMS = 30.0


@dataclass(frozen=True)
class Piece:
    """Mono PCM16 audio of one piece at ``rate`` Hz."""

    pcm: bytes
    rate: int
    seconds: float


@dataclass
class _Block:
    pcm: bytes
    rms: float
    seconds: float


class PieceCutter:
    """Collects recording blocks and hands out finished pieces (see the module doc)."""

    def __init__(
        self,
        *,
        budget_bytes: Callable[[], int],
        target_seconds: float = PIECE_TARGET_SECONDS,
        search_seconds: float = PIECE_SEARCH_SECONDS,
    ) -> None:
        self._budget_bytes = budget_bytes
        self._target_seconds = target_seconds
        self._search_seconds = search_seconds
        self._blocks: list[_Block] = []
        self._rate = 0
        self._seconds = 0.0
        self._bytes = 0

    def add(self, pcm: bytes, rate: int, seconds: float) -> list[Piece]:
        """Add one block; returns the pieces it completed, in order."""
        pieces: list[Piece] = []
        if self._blocks and rate != self._rate:
            pieces.append(self._take(len(self._blocks)))
        budget = self._budget_bytes() - WAV_HEADER_BYTES
        if self._blocks and self._bytes + len(pcm) > budget:
            pieces.append(self._take(self._quietest_cut(len(self._blocks))))
            while self._blocks and self._bytes + len(pcm) > budget:
                pieces.append(self._take(len(self._blocks)))
        self._rate = rate
        self._blocks.append(_Block(pcm, _rms(pcm), seconds))
        self._seconds += seconds
        self._bytes += len(pcm)
        if self._seconds < self._target_seconds:
            return pieces
        if self._ends_in_pause():
            pieces.append(self._take(len(self._blocks)))
        elif self._seconds >= self._target_seconds + self._search_seconds:
            window = self._blocks_within(self._search_seconds)
            pieces.append(self._take(self._quietest_cut(window)))
        return pieces

    def finish(self) -> Piece | None:
        """Return the rest of the audio as the last piece, if there is any."""
        return self._take(len(self._blocks)) if self._blocks else None

    def _take(self, count: int) -> Piece:
        taken, self._blocks = self._blocks[:count], self._blocks[count:]
        seconds = sum(block.seconds for block in taken)
        pcm = b"".join(block.pcm for block in taken)
        self._seconds -= seconds
        self._bytes -= len(pcm)
        return Piece(pcm, self._rate, seconds)

    def _quiet_level(self) -> float:
        levels = sorted(block.rms for block in self._blocks)
        floor = levels[(len(levels) - 1) * _NOISE_FLOOR_PERCENTILE // 100]
        return max(_MIN_QUIET_RMS, floor * _QUIET_FACTOR)

    def _ends_in_pause(self) -> bool:
        quiet = self._quiet_level()
        held = 0.0
        for block in reversed(self._blocks):
            if block.rms > quiet:
                return False
            held += block.seconds
            if held >= PAUSE_SECONDS:
                return True
        return False

    def _blocks_within(self, seconds: float) -> int:
        """How many of the newest blocks span ``seconds``."""
        held = 0.0
        for count, block in enumerate(reversed(self._blocks), start=1):
            held += block.seconds
            if held >= seconds:
                return count
        return len(self._blocks)

    def _quietest_cut(self, window: int) -> int:
        """Return where to cut among the newest ``window`` blocks: at the
        quietest block of the quietest pause-long run; never 0, so a piece is
        not empty."""
        start = max(len(self._blocks) - window, 1)
        run = max(1, self._blocks_count_for(PAUSE_SECONDS))
        best_first, best_level = None, float("inf")
        for first in range(start, len(self._blocks) - run + 1):
            level = sum(block.rms for block in self._blocks[first : first + run]) / run
            if level < best_level:
                best_first, best_level = first, level
        if best_first is None:
            return len(self._blocks)
        candidates = range(best_first, best_first + run)
        return max(1, min(candidates, key=lambda index: self._blocks[index].rms))

    def _blocks_count_for(self, seconds: float) -> int:
        block_seconds = self._blocks[-1].seconds if self._blocks else 0.0
        return round(seconds / block_seconds) if block_seconds > 0 else 1


def _rms(pcm: bytes) -> float:
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return math.sqrt(float(np.mean(samples.astype(np.float64) ** 2)))
