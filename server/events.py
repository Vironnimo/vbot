"""Internal server event bus for WebSocket push events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

JsonObject = dict[str, Any]

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


class ServerEventBus:
    """Replayable in-memory event bus for server lifecycle events."""

    def __init__(self) -> None:
        self.events: list[JsonObject] = []
        self._subscribers: list[asyncio.Queue[JsonObject]] = []

    def publish(self, event_type: str, payload: JsonObject | None = None) -> JsonObject:
        """Publish one provider-agnostic server event to active subscribers."""
        if event_type not in ALLOWED_SERVER_EVENT_TYPES:
            raise ValueError(f"unsupported server event type: {event_type}")
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

    async def subscribe(self, *, after_sequence: int = 0) -> AsyncGenerator[JsonObject, None]:
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
