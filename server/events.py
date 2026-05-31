"""Internal server event bus for WebSocket push events."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

JsonObject = dict[str, Any]
DEFAULT_SERVER_EVENT_RETENTION_LIMIT = 4096
DEFAULT_SERVER_EVENT_SUBSCRIBER_QUEUE_LIMIT = 1024
_LOGGER = logging.getLogger("vbot.server.events")

APP_ERROR_EVENT = "app_error"
AGENT_CREATED_EVENT = "agent.created"
AGENT_UPDATED_EVENT = "agent.updated"
AGENT_DELETED_EVENT = "agent.deleted"
RUN_STARTED_SERVER_EVENT = "run_started"
RUN_OUTPUT_SERVER_EVENT = "run_output"
RUN_COMPLETED_SERVER_EVENT = "run_completed"
RUN_CANCELLED_SERVER_EVENT = "run_cancelled"
RUN_FAILED_SERVER_EVENT = "run_failed"
PROVIDER_AUTH_COMPLETED_EVENT = "provider_auth_completed"

ALLOWED_SERVER_EVENT_TYPES = frozenset(
    {
        APP_ERROR_EVENT,
        AGENT_CREATED_EVENT,
        AGENT_UPDATED_EVENT,
        AGENT_DELETED_EVENT,
        RUN_STARTED_SERVER_EVENT,
        RUN_OUTPUT_SERVER_EVENT,
        RUN_COMPLETED_SERVER_EVENT,
        RUN_CANCELLED_SERVER_EVENT,
        RUN_FAILED_SERVER_EVENT,
        PROVIDER_AUTH_COMPLETED_EVENT,
    }
)


class _LaggedSubscriberSentinel:
    """Internal marker that closes a lagging live subscriber."""


_LAGGED_SUBSCRIBER = _LaggedSubscriberSentinel()


@dataclass
class _ServerEventSubscriber:
    queue: asyncio.Queue[JsonObject | _LaggedSubscriberSentinel]
    closed: bool = False


class ServerEventBus:
    """Replayable in-memory event bus for server lifecycle events."""

    def __init__(
        self,
        *,
        event_retention_limit: int = DEFAULT_SERVER_EVENT_RETENTION_LIMIT,
        subscriber_queue_limit: int = DEFAULT_SERVER_EVENT_SUBSCRIBER_QUEUE_LIMIT,
    ) -> None:
        if event_retention_limit < 1:
            raise ValueError("event_retention_limit must be positive")
        if subscriber_queue_limit < 1:
            raise ValueError("subscriber_queue_limit must be positive")
        self._events: deque[JsonObject] = deque(maxlen=event_retention_limit)
        self._subscribers: list[_ServerEventSubscriber] = []
        self._subscriber_queue_limit = subscriber_queue_limit
        self._next_sequence = 1

    @property
    def events(self) -> list[JsonObject]:
        """Return the currently retained replay window."""
        return list(self._events)

    def publish(self, event_type: str, payload: JsonObject | None = None) -> JsonObject:
        """Publish one provider-agnostic server event to active subscribers."""
        if event_type not in ALLOWED_SERVER_EVENT_TYPES:
            raise ValueError(f"unsupported server event type: {event_type}")
        event = {
            "sequence": self._next_sequence,
            "type": event_type,
            "payload": dict(payload or {}),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._next_sequence += 1
        self._events.append(event)
        for subscriber in list(self._subscribers):
            self._publish_to_subscriber(subscriber, event)
        return event

    async def subscribe(self, *, after_sequence: int = 0) -> AsyncGenerator[JsonObject, None]:
        """Replay existing events and stream new events until the client disconnects."""
        subscriber = _ServerEventSubscriber(
            queue=asyncio.Queue(maxsize=self._subscriber_queue_limit)
        )
        self._subscribers.append(subscriber)
        try:
            for event in list(self._events):
                if subscriber.closed:
                    return
                sequence = _event_sequence(event)
                if sequence > after_sequence:
                    yield event
                    after_sequence = sequence

            while True:
                item = await subscriber.queue.get()
                if item is _LAGGED_SUBSCRIBER:
                    return
                event = cast(JsonObject, item)
                sequence = _event_sequence(event)
                if sequence > after_sequence:
                    yield event
                    after_sequence = sequence
        finally:
            self._remove_subscriber(subscriber)

    @property
    def subscriber_count(self) -> int:
        """Return active subscriber count for leak-focused tests."""
        return len(self._subscribers)

    def _publish_to_subscriber(
        self,
        subscriber: _ServerEventSubscriber,
        event: JsonObject,
    ) -> None:
        if subscriber.closed:
            return
        try:
            subscriber.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._evict_lagging_subscriber(subscriber)

    def _evict_lagging_subscriber(self, subscriber: _ServerEventSubscriber) -> None:
        if subscriber.closed:
            return
        self._remove_subscriber(subscriber)
        _drain_queue(subscriber.queue)
        subscriber.queue.put_nowait(_LAGGED_SUBSCRIBER)
        _LOGGER.warning("Evicted lagging server event subscriber")

    def _remove_subscriber(self, subscriber: _ServerEventSubscriber) -> None:
        subscriber.closed = True
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)


def _drain_queue(queue: asyncio.Queue[Any]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _event_sequence(event: JsonObject) -> int:
    sequence = event.get("sequence", 0)
    return sequence if isinstance(sequence, int) else 0
