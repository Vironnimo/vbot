"""Internal server event bus for WebSocket push events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

JsonObject = dict[str, Any]


class ServerEventBus:
    """Replayable in-memory event bus for server lifecycle events."""

    def __init__(self) -> None:
        self.events: list[JsonObject] = []
        self._subscribers: list[asyncio.Queue[JsonObject]] = []

    def publish(self, event_type: str, payload: JsonObject | None = None) -> JsonObject:
        """Publish one provider-agnostic server event to active subscribers."""
        event = {
            "sequence": len(self.events) + 1,
            "type": event_type,
            "payload": dict(payload or {}),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.events.append(event)
        for subscriber in list(self._subscribers):
            subscriber.put_nowait(event)
        return event

    async def subscribe(self, *, after_sequence: int = 0) -> AsyncIterator[JsonObject]:
        """Replay existing events and stream new events until the client disconnects."""
        for event in self.events:
            if _event_sequence(event) > after_sequence:
                yield event

        queue: asyncio.Queue[JsonObject] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                if _event_sequence(event) > after_sequence:
                    yield event
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    @property
    def subscriber_count(self) -> int:
        """Return active subscriber count for leak-focused tests."""
        return len(self._subscribers)


def _event_sequence(event: JsonObject) -> int:
    sequence = event.get("sequence", 0)
    return sequence if isinstance(sequence, int) else 0
