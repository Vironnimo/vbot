"""Tests for the server event bus epoch and sequence contract.

These tests cover Phase 1.1, Task 1 of the run-lifecycle-truth plan: every
``ServerEventBus`` instance exposes a stable ``epoch`` (used by ``/ws``
clients to detect a server restart) and stamps every published event with
that same ``epoch`` and a monotonically increasing ``sequence``.
``last_sequence`` reflects the most recently published event (0 when the
bus is empty).
"""

from __future__ import annotations

import re
import uuid
from contextlib import aclosing
from typing import Any

import pytest

from server.events import (
    AGENT_CREATED_EVENT,
    AGENT_UPDATED_EVENT,
    ALLOWED_SERVER_EVENT_TYPES,
    APP_ERROR_EVENT,
    RUN_STARTED_SERVER_EVENT,
    ServerEventBus,
)

# Length of a ``uuid.uuid4().hex`` value — a 128-bit hex string.
_HEX32 = 32
_UUID4_HEX_PATTERN = re.compile(r"^[0-9a-f]{32}$")


# -- epoch property ----------------------------------------------------------


def test_event_bus_epoch_is_generated_as_uuid4_hex_on_init() -> None:
    # Arrange / Act
    bus = ServerEventBus()

    # Assert: epoch is a lowercase 32-char hex string (uuid.uuid4().hex format).
    epoch = bus.epoch
    assert isinstance(epoch, str)
    assert len(epoch) == _HEX32
    assert _UUID4_HEX_PATTERN.match(epoch) is not None


def test_event_bus_epoch_is_stable_across_the_instance_lifetime() -> None:
    # Arrange
    bus = ServerEventBus()

    # Act
    bus.publish(AGENT_CREATED_EVENT, {"id": "a"})
    bus.publish(AGENT_UPDATED_EVENT, {"id": "a"})

    # Assert: every read returns the same value.
    assert bus.epoch == bus.epoch
    assert bus.epoch == bus.epoch


def test_each_event_bus_instance_gets_a_distinct_epoch() -> None:
    # Arrange / Act
    first = ServerEventBus()
    second = ServerEventBus()

    # Assert: independent instances are independent generations. Sampling many
    # pairs guards against an astronomically unlikely uuid4 collision and
    # documents that the epoch is a per-process value, not a class-level one.
    seen = {first.epoch, second.epoch}
    for _ in range(32):
        seen.add(ServerEventBus().epoch)
    assert len(seen) == 34


def test_event_bus_epoch_is_not_mutable_from_outside() -> None:
    # Arrange
    bus = ServerEventBus()
    original = bus.epoch

    # Act: attempt to overwrite the read-only property.
    with pytest.raises(AttributeError):
        bus.epoch = "tampered"  # type: ignore[misc]

    # Assert: the underlying value is unchanged.
    assert bus.epoch == original
    # The private attribute the property wraps is also protected by name-mangling
    # style; assigning to ``_epoch`` directly would mutate it, so verify the
    # public surface cannot be used to do that.
    with pytest.raises(AttributeError):
        del bus.epoch  # type: ignore[misc]


# -- last_sequence property --------------------------------------------------


def test_event_bus_last_sequence_is_zero_before_any_publish() -> None:
    # Arrange
    bus = ServerEventBus()

    # Assert
    assert bus.last_sequence == 0


def test_event_bus_last_sequence_tracks_the_most_recent_publish() -> None:
    # Arrange
    bus = ServerEventBus()

    # Act / Assert: increments by one per publish, matching the sequence field.
    bus.publish(AGENT_CREATED_EVENT, {"id": "a"})
    assert bus.last_sequence == 1
    assert bus.events[-1]["sequence"] == 1

    bus.publish(AGENT_UPDATED_EVENT, {"id": "a"})
    assert bus.last_sequence == 2
    assert bus.events[-1]["sequence"] == 2

    bus.publish(APP_ERROR_EVENT, {"message": "boom"})
    assert bus.last_sequence == 3
    assert bus.events[-1]["sequence"] == 3


def test_event_bus_last_sequence_is_not_writable() -> None:
    # Arrange
    bus = ServerEventBus()
    bus.publish(AGENT_CREATED_EVENT, {"id": "a"})

    # Act / Assert: read-only property.
    with pytest.raises(AttributeError):
        bus.last_sequence = 999  # type: ignore[misc]


# -- publish stamps epoch on every event --------------------------------------


def test_publish_includes_epoch_and_sequence_on_every_event() -> None:
    # Arrange
    bus = ServerEventBus()
    expected_epoch = bus.epoch

    # Act
    first = bus.publish(AGENT_CREATED_EVENT, {"id": "a"})
    second = bus.publish(AGENT_UPDATED_EVENT, {"id": "a"})
    third = bus.publish(APP_ERROR_EVENT, {"message": "boom"})

    # Assert: every event dict carries both ``epoch`` and ``sequence``.
    for event in (first, second, third):
        assert event["epoch"] == expected_epoch
        assert isinstance(event["sequence"], int)
        assert event["sequence"] >= 1
    # The three sequences are the three natural numbers in order.
    assert [first["sequence"], second["sequence"], third["sequence"]] == [1, 2, 3]


def test_published_event_matches_retained_window_entry() -> None:
    # Arrange
    bus = ServerEventBus()

    # Act
    published = bus.publish(RUN_STARTED_SERVER_EVENT, {"run_id": "r-1"})

    # Assert: the public events list (used by /ws replay) carries the same
    # epoch+sequence stamp as the value returned from publish.
    retained = bus.events[-1]
    assert retained["epoch"] == published["epoch"]
    assert retained["sequence"] == published["sequence"]
    assert retained["type"] == published["type"]


def test_publish_uses_a_none_payload_without_breaking_epoch_stamping() -> None:
    # Arrange
    bus = ServerEventBus()

    # Act
    event = bus.publish(APP_ERROR_EVENT, payload=None)

    # Assert: epoch/sequence still present, payload is an empty dict (matches
    # the pre-existing ``dict(payload or {})`` contract for None payloads).
    assert event["epoch"] == bus.epoch
    assert event["sequence"] == 1
    assert event["payload"] == {}


# -- subscribe / replay still work; epoch is just a stamp -------------------


@pytest.mark.asyncio
async def test_replayed_events_carry_the_same_epoch_as_new_publishes() -> None:
    # Arrange
    bus = ServerEventBus()
    expected_epoch = bus.epoch
    bus.publish(AGENT_CREATED_EVENT, {"id": "a"})  # sequence 1
    bus.publish(AGENT_UPDATED_EVENT, {"id": "a"})  # sequence 2

    # Act: subscribe with after_sequence=0 replays both, then a live publish.
    received: list[dict[str, Any]] = []
    async with aclosing(bus.subscribe(after_sequence=0)) as gen:
        received.append(await gen.__anext__())
        received.append(await gen.__anext__())
        bus.publish(APP_ERROR_EVENT, {"message": "boom"})
        received.append(await gen.__anext__())
        if len(received) == 3:
            # No more events to wait for — exit by raising StopAsyncIteration
            # through aclosing by breaking via the explicit close path.
            await gen.aclose()

    # Assert: every delivered event — replay or live — carries the bus epoch.
    assert all(event["epoch"] == expected_epoch for event in received)
    assert [event["sequence"] for event in received] == [1, 2, 3]


# -- contract allowlist unchanged, every event type still works ---------------


def test_publish_stamps_epoch_for_every_allowed_event_type() -> None:
    # Arrange
    bus = ServerEventBus()

    # Act: publish one event of every allowed type.
    published = [
        bus.publish(event_type, {"sentinel": event_type})
        for event_type in ALLOWED_SERVER_EVENT_TYPES
    ]

    # Assert: each event carries the epoch and a unique sequence.
    epoch = bus.epoch
    assert len(published) == len(ALLOWED_SERVER_EVENT_TYPES)
    assert len({event["sequence"] for event in published}) == len(published)
    assert all(event["epoch"] == epoch for event in published)


# -- integration: the bus re-uses the same uuid module behaviour -------------


def test_event_bus_epoch_is_a_valid_uuid4_hex_value() -> None:
    # Arrange / Act
    bus = ServerEventBus()

    # Assert: the value round-trips through ``uuid.UUID(...).hex`` — the
    # canonical way to assert "this is a uuid4 hex string".
    parsed = uuid.UUID(hex=bus.epoch)
    assert parsed.hex == bus.epoch
    assert parsed.version == 4
