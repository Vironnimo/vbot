"""Internal server event bus for WebSocket push events."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any

from core.event_stream import ReplayEventStream

JsonObject = dict[str, Any]
DEFAULT_SERVER_EVENT_RETENTION_LIMIT = 4096
DEFAULT_SERVER_EVENT_SUBSCRIBER_QUEUE_LIMIT = 1024
_LOGGER = logging.getLogger("vbot.server.events")

APP_ERROR_EVENT = "app_error"
RUN_STARTED_SERVER_EVENT = "run_started"
RUN_OUTPUT_SERVER_EVENT = "run_output"
RUN_COMPLETED_SERVER_EVENT = "run_completed"
RUN_CANCELLED_SERVER_EVENT = "run_cancelled"
RUN_FAILED_SERVER_EVENT = "run_failed"
RUN_INTERRUPTED_SERVER_EVENT = "run_interrupted"
PROVIDER_AUTH_COMPLETED_EVENT = "provider_auth_completed"
RESOURCE_CHANGED_EVENT = "resource_changed"
BASH_PROCESS_STATUS_CHANGED_EVENT = "bash_process_status_changed"

ALLOWED_SERVER_EVENT_TYPES = frozenset(
    {
        APP_ERROR_EVENT,
        RUN_STARTED_SERVER_EVENT,
        RUN_OUTPUT_SERVER_EVENT,
        RUN_COMPLETED_SERVER_EVENT,
        RUN_CANCELLED_SERVER_EVENT,
        RUN_FAILED_SERVER_EVENT,
        RUN_INTERRUPTED_SERVER_EVENT,
        PROVIDER_AUTH_COMPLETED_EVENT,
        RESOURCE_CHANGED_EVENT,
        BASH_PROCESS_STATUS_CHANGED_EVENT,
    }
)

# The generic "resource X changed → reload it" signal (RESOURCE_CHANGED_EVENT)
# carries no data beyond which kind of shared app state changed. One kind = one
# constant, no new plumbing; an optional scope narrows the signal to a single
# agent/session where that makes sense (queue/sessions). Adding a kind means
# adding it here and pointing one more consumer at it. Agent CRUD rides this
# channel too (kind "agents") rather than a parallel set of agent.* events.
RESOURCE_KIND_MODELS = "models"
RESOURCE_KIND_QUEUE = "queue"
RESOURCE_KIND_SESSIONS = "sessions"
RESOURCE_KIND_AGENTS = "agents"
RESOURCE_KIND_PROVIDERS = "providers"
RESOURCE_KIND_CLIENTS = "clients"
RESOURCE_KIND_CHANNELS = "channels"
RESOURCE_KIND_DEBUG_TRACES = "debug_traces"
RESOURCE_KIND_PROJECTS = "projects"
RESOURCE_KIND_CRON = "cron"
RESOURCE_KIND_CALENDAR = "calendar"
RESOURCE_KIND_COMMANDS = "commands"
RESOURCE_KIND_TERMINALS = "terminals"
RESOURCE_KIND_MEMORIES = "memories"
RESOURCE_KIND_SKILLS = "skills"
RESOURCE_KIND_DATA_STORE = "data_store"
RESOURCE_KIND_EXTENSIONS = "extensions"

ALLOWED_RESOURCE_KINDS = frozenset(
    {
        RESOURCE_KIND_MODELS,
        RESOURCE_KIND_QUEUE,
        RESOURCE_KIND_SESSIONS,
        RESOURCE_KIND_AGENTS,
        RESOURCE_KIND_PROVIDERS,
        RESOURCE_KIND_CLIENTS,
        RESOURCE_KIND_CHANNELS,
        RESOURCE_KIND_DEBUG_TRACES,
        RESOURCE_KIND_PROJECTS,
        RESOURCE_KIND_CRON,
        RESOURCE_KIND_CALENDAR,
        RESOURCE_KIND_COMMANDS,
        RESOURCE_KIND_TERMINALS,
        RESOURCE_KIND_MEMORIES,
        RESOURCE_KIND_SKILLS,
        RESOURCE_KIND_DATA_STORE,
        RESOURCE_KIND_EXTENSIONS,
    }
)


class ServerEventBus:
    """Replayable in-memory event bus for server lifecycle events.

    The bus belongs to one Event Loop: the loop running when it is constructed,
    or else the loop of its first subscriber. Sequence numbering, retention and
    subscriber queues are loop-only state, so ``publish`` from any other thread
    hands the event to that loop. Callers need not know which thread they run on.
    """

    def __init__(
        self,
        *,
        event_retention_limit: int = DEFAULT_SERVER_EVENT_RETENTION_LIMIT,
        subscriber_queue_limit: int = DEFAULT_SERVER_EVENT_SUBSCRIBER_QUEUE_LIMIT,
    ) -> None:
        self._event_stream = ReplayEventStream[JsonObject](
            event_retention_limit=event_retention_limit,
            subscriber_queue_limit=subscriber_queue_limit,
            sequence_of=_event_sequence,
            on_lagged=lambda: _LOGGER.warning("Evicted lagging server event subscriber"),
        )
        self._next_sequence = 1
        # Generation identifier used by /ws clients to detect a server restart
        # (bus restarts at sequence 1, so a sequence regression on its own is
        # ambiguous — the epoch is the authoritative "new server" signal).
        self._epoch = uuid.uuid4().hex
        self._loop = _running_loop()

    @property
    def epoch(self) -> str:
        """Return this bus instance's generation identifier."""
        return self._epoch

    @property
    def last_sequence(self) -> int:
        """Return the sequence number of the most recently published event (0 if none)."""
        return self._next_sequence - 1

    @property
    def events(self) -> list[JsonObject]:
        """Return the currently retained replay window."""
        return self._event_stream.events

    def publish(self, event_type: str, payload: JsonObject | None = None) -> None:
        """Publish one provider-agnostic server event to active subscribers.

        On the bus's Event Loop, or before the bus has one, the event is
        retained and fanned out immediately. From another thread it is handed
        to that loop and published there in arrival order; it receives its
        sequence number then. Once the loop has closed, no subscriber can
        receive the event, so it is discarded.
        """
        if event_type not in ALLOWED_SERVER_EVENT_TYPES:
            raise ValueError(f"unsupported server event type: {event_type}")
        payload = dict(payload or {})
        loop = self._loop
        if loop is None or _running_loop() is loop:
            self._publish_on_loop(event_type, payload)
            return
        try:
            loop.call_soon_threadsafe(self._publish_on_loop, event_type, payload)
        except RuntimeError:
            _LOGGER.debug("Discarded %s server event after the Event Loop closed", event_type)

    def _publish_on_loop(self, event_type: str, payload: JsonObject) -> None:
        event = {
            "sequence": self._next_sequence,
            "epoch": self._epoch,
            "type": event_type,
            "payload": payload,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._next_sequence += 1
        self._event_stream.publish(event)

    async def subscribe(self, *, after_sequence: int = 0) -> AsyncGenerator[JsonObject, None]:
        """Replay existing events and stream new events until the client disconnects."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        async with aclosing(self._event_stream.subscribe(after_sequence=after_sequence)) as events:
            async for event in events:
                yield event

    @property
    def subscriber_count(self) -> int:
        """Return active subscriber count for leak-focused tests."""
        return self._event_stream.subscriber_count


def _event_sequence(event: JsonObject) -> int:
    sequence = event.get("sequence", 0)
    return sequence if isinstance(sequence, int) else 0


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
