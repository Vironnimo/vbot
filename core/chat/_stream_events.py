"""Bounded, scheduled visible delta publication for a streaming Model request."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from core.chat.streaming import (
    StreamingDeltaBatcher,
    StreamingVisibleDelta,
)

if TYPE_CHECKING:
    from core.runs import Run


class _StreamingRunDeltaEmitter:
    """Schedule bounded Run-event batches without delaying a quiet stream."""

    def __init__(self, run: Run) -> None:
        self._run = run
        self._batcher = StreamingDeltaBatcher()
        self._flush_handle: asyncio.TimerHandle | None = None

    def add(self, delta: StreamingVisibleDelta) -> None:
        ready = self._batcher.add(delta)
        if ready:
            self._cancel_flush()
            self._emit(ready)
        if self._batcher.has_pending and self._flush_handle is None:
            delay = self._batcher.seconds_until_flush()
            assert delay is not None
            self._flush_handle = asyncio.get_running_loop().call_later(delay, self._flush_due)

    def flush(self) -> None:
        self._cancel_flush()
        self._emit(self._batcher.flush())

    def close(self) -> None:
        self._cancel_flush()

    def _flush_due(self) -> None:
        self._flush_handle = None
        self._emit(self._batcher.flush(now=time.monotonic()))

    def _cancel_flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None

    def _emit(self, deltas: list[StreamingVisibleDelta]) -> None:
        for delta in deltas:
            self._run.emit(delta.event_type, delta.payload)
