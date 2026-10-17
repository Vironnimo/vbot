"""In-memory rate limits and aggregated logging of refused access."""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

GLOBAL_KEY = "*"
_MAX_KEYS = 4096


class SlidingWindow:
    """At most ``limit`` events per ``window`` seconds for each key.

    Memory is bounded: each key keeps only its ``limit`` latest events, which
    decide whether it is blocked, and the least recently used key is forgotten
    first, which can only make a limit more lenient for that key, never block
    anyone else.
    """

    def __init__(
        self,
        limit: int,
        window: float,
        *,
        clock: Callable[[], float],
        max_keys: int = _MAX_KEYS,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._events: OrderedDict[Hashable, deque[float]] = OrderedDict()

    def blocked(self, key: Hashable) -> float | None:
        """Seconds until ``key`` may act again, or ``None`` when it may now."""
        events = self._current(key)
        if events is None or len(events) < self._limit:
            return None
        return max(1.0, events[0] + self._window - self._clock())

    def record(self, key: Hashable) -> None:
        events = self._current(key)
        if events is None:
            events = deque(maxlen=self._limit)
            self._events[key] = events
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)
        events.append(self._clock())

    def acquire(self, key: Hashable) -> float | None:
        """Record one event unless blocked; returns the wait when blocked."""
        wait = self.blocked(key)
        if wait is None:
            self.record(key)
        return wait

    def _current(self, key: Hashable) -> deque[float] | None:
        events = self._events.get(key)
        if events is None:
            return None
        self._events.move_to_end(key)
        horizon = self._clock() - self._window
        while events and events[0] <= horizon:
            events.popleft()
        return events


@dataclass(frozen=True)
class RateLimit:
    """One limit per effective client and one for everyone together."""

    per_client: SlidingWindow
    overall: SlidingWindow

    def acquire(self, client: str) -> float | None:
        wait = self.per_client.blocked(client) or self.overall.blocked(GLOBAL_KEY)
        if wait is None:
            self.per_client.record(client)
            self.overall.record(GLOBAL_KEY)
        return wait

    def blocked(self, client: str) -> float | None:
        return self.per_client.blocked(client) or self.overall.blocked(GLOBAL_KEY)

    def record(self, client: str) -> None:
        self.per_client.record(client)
        self.overall.record(GLOBAL_KEY)


#: How many distinct clients one summary line names.
_SUMMARY_CLIENTS = 10


@dataclass
class _Burst:
    count: int = 0
    clients: set[str] = field(default_factory=set)
    more_clients: bool = False
    flush_scheduled: bool = False

    def add(self, client: str) -> None:
        self.count += 1
        if client in self.clients:
            return
        if len(self.clients) < _SUMMARY_CLIENTS:
            self.clients.add(client)
        else:
            self.more_clients = True


class RefusalLog:
    """Log refused access at WARNING without letting a flood fill the log.

    The first refusal of a reason is logged at once. Further refusals of that
    reason within ``interval`` seconds are counted and logged as one summary
    line when the interval ends. ``schedule(delay, callback)`` runs the summary;
    the server passes the Event Loop's ``call_later``.
    """

    def __init__(
        self,
        logger: logging.Logger,
        *,
        interval: float,
        schedule: Callable[[float, Callable[[], None]], object],
    ) -> None:
        self._logger = logger
        self._interval = interval
        self._schedule = schedule
        self._bursts: dict[str, _Burst] = {}

    def refused(self, reason: str, *, client: str | None, detail: str) -> None:
        burst = self._bursts.get(reason)
        if burst is None:
            self._logger.warning(
                "Access refused (reason=%s client=%s %s)", reason, client or "unknown", detail
            )
            self._bursts[reason] = burst = _Burst()
            burst.flush_scheduled = True
            self._schedule(self._interval, lambda: self._flush(reason))
            return
        burst.add(client or "unknown")

    def _flush(self, reason: str) -> None:
        burst = self._bursts.get(reason)
        if burst is None:
            return
        if burst.count == 0:
            del self._bursts[reason]
            return
        self._logger.warning(
            "Access refused repeatedly (reason=%s count=%d clients=%s interval=%ds)",
            reason,
            burst.count,
            ",".join(sorted(burst.clients)) + (",..." if burst.more_clients else ""),
            round(self._interval),
        )
        # Keep summarizing while refusals continue; a quiet interval ends the burst.
        self._bursts[reason] = _Burst(flush_scheduled=True)
        self._schedule(self._interval, lambda: self._flush(reason))
