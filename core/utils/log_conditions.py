"""Log a persistent condition when it starts, changes or ends - not on every repetition.

A condition such as a missing include or an invalid file holds on every read,
build or poll until someone fixes it. ``LoggedConditions`` remembers which
conditions a caller already logged and in which state, so the caller logs a
condition once when it starts, again only when its state changes, and once when
it ends (``logging.md`` -> Transitions, not repetitions). The memory is bounded:
beyond ``limit`` conditions the least recently seen one is forgotten, and a
forgotten condition that still holds is logged again.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Hashable

DEFAULT_CONDITION_LIMIT = 256

_ABSENT = object()


class LoggedConditions:
    """The persistent conditions one owner has logged, each with its last state."""

    def __init__(self, *, limit: int = DEFAULT_CONDITION_LIMIT) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._states: OrderedDict[Hashable, Hashable] = OrderedDict()
        self._lock = threading.Lock()

    def started(self, key: Hashable, state: Hashable = True) -> bool:
        """Record that condition ``key`` holds in ``state``.

        Returns ``True`` when the caller should log it: the condition is new,
        its state changed, or it was forgotten. A repetition returns ``False``.
        """
        with self._lock:
            known = self._states.get(key, _ABSENT)
            if known is not _ABSENT:
                self._states.move_to_end(key)
                if known == state:
                    return False
            self._states[key] = state
            if len(self._states) > self._limit:
                self._states.popitem(last=False)
            return True

    def ended(self, key: Hashable) -> bool:
        """Forget condition ``key``; ``True`` when it was logged, so its end can be."""
        with self._lock:
            return self._states.pop(key, _ABSENT) is not _ABSENT


__all__ = ["DEFAULT_CONDITION_LIMIT", "LoggedConditions"]
