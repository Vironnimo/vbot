"""Replay, fan-out, and lag eviction for in-process ordered event streams."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

EventT = TypeVar("EventT")


class _LaggedSubscriberSentinel:
    """Internal marker that closes a subscriber whose queue overflowed."""


_LAGGED_SUBSCRIBER = _LaggedSubscriberSentinel()


@dataclass
class _Subscriber(Generic[EventT]):
    queue: asyncio.Queue[EventT | _LaggedSubscriberSentinel]
    closed: bool = False
    # While True, publish drops on a full queue instead of evicting. Catch-up
    # re-reads the retention buffer, so live traffic during a slow historical
    # replay must not silently kill the subscriber mid-stream.
    catching_up: bool = True


class ReplayEventStream(Generic[EventT]):
    """Own retained replay, live fan-out, and bounded subscriber queues.

    The event's shape and terminal semantics stay with the consuming domain.
    This owner only requires a monotonically increasing sequence extractor and,
    optionally, a terminal-event predicate. A lagging subscriber is removed and
    woken with an internal sentinel so its async iterator closes without leaking.
    """

    def __init__(
        self,
        *,
        event_retention_limit: int,
        subscriber_queue_limit: int,
        sequence_of: Callable[[EventT], int],
        terminal_when: Callable[[EventT], bool] | None = None,
        on_lagged: Callable[[], None] | None = None,
    ) -> None:
        if event_retention_limit < 1:
            raise ValueError("event_retention_limit must be positive")
        if subscriber_queue_limit < 1:
            raise ValueError("subscriber_queue_limit must be positive")
        self._events: deque[EventT] = deque(maxlen=event_retention_limit)
        self._subscribers: list[_Subscriber[EventT]] = []
        self._subscriber_queue_limit = subscriber_queue_limit
        self._sequence_of = sequence_of
        self._terminal_when = terminal_when
        self._on_lagged = on_lagged

    @property
    def events(self) -> list[EventT]:
        """Return the currently retained replay window."""

        return list(self._events)

    @property
    def subscriber_count(self) -> int:
        """Return the number of active live subscribers."""

        return len(self._subscribers)

    def publish(self, event: EventT) -> None:
        """Retain one event and fan it out to every live subscriber."""

        self._events.append(event)
        for subscriber in list(self._subscribers):
            self._publish_to_subscriber(subscriber, event)

    async def subscribe(
        self,
        *,
        after_sequence: int = 0,
        live: bool = True,
    ) -> AsyncGenerator[EventT, None]:
        """Replay newer retained events, then optionally stream live events."""

        subscriber: _Subscriber[EventT] | None = None
        try:
            # Historical replay happens before live registration so a slow
            # consumer cannot fill its live queue (and get evicted) while
            # walking a large retained window. Events published during that
            # walk stay in retention and are picked up by catch-up below.
            async for event in self._iter_retained(after_sequence=after_sequence):
                yield event
                after_sequence = self._sequence_of(event)
                if self._is_terminal(event):
                    return

            if not live:
                return

            subscriber = _Subscriber(queue=asyncio.Queue(maxsize=self._subscriber_queue_limit))
            self._subscribers.append(subscriber)

            # Catch up anything published during historical replay or between
            # registration and the live wait. Dropped full-queue publishes
            # during this phase are safe: retention still holds them.
            while True:
                progressed = False
                async for event in self._iter_retained(
                    after_sequence=after_sequence,
                    subscriber=subscriber,
                ):
                    yield event
                    after_sequence = self._sequence_of(event)
                    progressed = True
                    if self._is_terminal(event):
                        return

                drained = self._drain_subscriber_queue(subscriber)
                for event in drained:
                    sequence = self._sequence_of(event)
                    if sequence <= after_sequence:
                        continue
                    yield event
                    after_sequence = sequence
                    progressed = True
                    if self._is_terminal(event):
                        return

                if subscriber.closed:
                    return
                if not progressed:
                    break

            subscriber.catching_up = False

            while True:
                item = await subscriber.queue.get()
                if item is _LAGGED_SUBSCRIBER:
                    return
                event = cast(EventT, item)
                sequence = self._sequence_of(event)
                if sequence <= after_sequence:
                    continue
                yield event
                after_sequence = sequence
                if self._is_terminal(event):
                    return
        finally:
            if subscriber is not None:
                self._remove_subscriber(subscriber)

    async def _iter_retained(
        self,
        *,
        after_sequence: int,
        subscriber: _Subscriber[EventT] | None = None,
    ) -> AsyncGenerator[EventT, None]:
        for event in list(self._events):
            if subscriber is not None and subscriber.closed:
                return
            sequence = self._sequence_of(event)
            if sequence <= after_sequence:
                continue
            yield event
            after_sequence = sequence
            if self._is_terminal(event):
                return

    def _drain_subscriber_queue(self, subscriber: _Subscriber[EventT]) -> list[EventT]:
        drained: list[EventT] = []
        while True:
            try:
                item = subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:
                return drained
            if item is _LAGGED_SUBSCRIBER:
                # Lag eviction is disabled during catch-up; treat a sentinel as
                # a closed subscriber if it ever appears.
                subscriber.closed = True
                return drained
            drained.append(cast(EventT, item))

    def _is_terminal(self, event: EventT) -> bool:
        return self._terminal_when is not None and self._terminal_when(event)

    def _publish_to_subscriber(self, subscriber: _Subscriber[EventT], event: EventT) -> None:
        if subscriber.closed:
            return
        try:
            subscriber.queue.put_nowait(event)
        except asyncio.QueueFull:
            if subscriber.catching_up:
                # Retention still holds the event for the catch-up rescan.
                return
            self._evict_lagging_subscriber(subscriber)

    def _evict_lagging_subscriber(self, subscriber: _Subscriber[EventT]) -> None:
        if subscriber.closed:
            return
        self._remove_subscriber(subscriber)
        _drain_queue(subscriber.queue)
        subscriber.queue.put_nowait(_LAGGED_SUBSCRIBER)
        if self._on_lagged is not None:
            self._on_lagged()

    def _remove_subscriber(self, subscriber: _Subscriber[EventT]) -> None:
        subscriber.closed = True
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)


def _drain_queue(queue: asyncio.Queue[Any]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
