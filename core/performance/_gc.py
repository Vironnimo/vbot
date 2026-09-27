"""Cyclic garbage collection pause observer.

CPython runs a collection on whichever thread crosses the allocation threshold
and holds the GIL for the whole collection, so every pause also blocks the
Event Loop. The observer registers one ``gc.callbacks`` entry. That callback
runs inside the collector, possibly while its thread holds any lock of this
process, so it takes none: it only timestamps, adds to a running total and
appends to a bounded deque that the monitor drains on the Event Loop.
"""

from __future__ import annotations

import gc
from collections import deque
from contextlib import suppress
from time import perf_counter
from typing import Any

_MAX_PENDING = 4096


class GcObserver:
    """Time each collection of this process while installed."""

    def __init__(self, *, max_pending: int = _MAX_PENDING) -> None:
        # (generation, started, ended) perf_counter values; oldest dropped when full.
        self._pending: deque[tuple[int, float, float]] = deque(maxlen=max_pending)
        self._started = 0.0
        self._total_s = 0.0
        self._installed = False

    @property
    def total_s(self) -> float:
        """Seconds spent in collections since construction, all threads."""
        return self._total_s

    def install(self) -> None:
        if not self._installed:
            gc.callbacks.append(self._on_collection)
            self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            self._installed = False
            with suppress(ValueError):
                gc.callbacks.remove(self._on_collection)

    def drain(self) -> list[tuple[int, float, float]]:
        """Remove and return finished collections, oldest first."""
        drained: list[tuple[int, float, float]] = []
        pending = self._pending
        while True:
            try:
                drained.append(pending.popleft())
            except IndexError:
                return drained

    def _on_collection(self, phase: str, info: dict[str, Any]) -> None:
        now = perf_counter()
        if phase == "start":
            self._started = now
            return
        # Collections never overlap, so start and stop always pair up.
        started = self._started
        self._total_s += now - started
        self._pending.append((info["generation"], started, now))
