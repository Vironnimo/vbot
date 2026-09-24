"""Tests for the server event bus epoch and sequence contract.

These tests cover Phase 1.1, Task 1 of the run-lifecycle-truth plan: every
``ServerEventBus`` instance exposes a stable ``epoch`` (used by ``/ws``
clients to detect a server restart) and stamps every published event with
that same ``epoch`` and a monotonically increasing ``sequence``.
``last_sequence`` reflects the most recently published event (0 when the
bus is empty).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, contextmanager
from typing import Any

import pytest

from server.events import (
    ALLOWED_RESOURCE_KINDS,
    ALLOWED_SERVER_EVENT_TYPES,
    APP_ERROR_EVENT,
    RESOURCE_CHANGED_EVENT,
    RESOURCE_KIND_MODELS,
    RUN_COMPLETED_SERVER_EVENT,
    RUN_OUTPUT_SERVER_EVENT,
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
    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})
    bus.publish(RUN_COMPLETED_SERVER_EVENT, {"id": "a"})

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
    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})
    assert bus.last_sequence == 1
    assert bus.events[-1]["sequence"] == 1

    bus.publish(RUN_COMPLETED_SERVER_EVENT, {"id": "a"})
    assert bus.last_sequence == 2
    assert bus.events[-1]["sequence"] == 2

    bus.publish(APP_ERROR_EVENT, {"message": "boom"})
    assert bus.last_sequence == 3
    assert bus.events[-1]["sequence"] == 3


def test_event_bus_last_sequence_is_not_writable() -> None:
    # Arrange
    bus = ServerEventBus()
    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})

    # Act / Assert: read-only property.
    with pytest.raises(AttributeError):
        bus.last_sequence = 999  # type: ignore[misc]


# -- publish stamps epoch on every event --------------------------------------


def test_publish_includes_epoch_and_sequence_on_every_event() -> None:
    # Arrange
    bus = ServerEventBus()
    expected_epoch = bus.epoch

    # Act
    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})
    bus.publish(RUN_COMPLETED_SERVER_EVENT, {"id": "a"})
    bus.publish(APP_ERROR_EVENT, {"message": "boom"})

    # Assert: every retained event carries both ``epoch`` and ``sequence``.
    events = bus.events
    for event in events:
        assert event["epoch"] == expected_epoch
    # The three sequences are the three natural numbers in order.
    assert [event["sequence"] for event in events] == [1, 2, 3]


def test_publish_uses_a_none_payload_without_breaking_epoch_stamping() -> None:
    # Arrange
    bus = ServerEventBus()

    # Act
    bus.publish(APP_ERROR_EVENT, payload=None)

    # Assert: epoch/sequence still present, payload is an empty dict (matches
    # the pre-existing ``dict(payload or {})`` contract for None payloads).
    event = bus.events[-1]
    assert event["epoch"] == bus.epoch
    assert event["sequence"] == 1
    assert event["payload"] == {}


# -- subscribe / replay still work; epoch is just a stamp -------------------


@pytest.mark.asyncio
async def test_replayed_events_carry_the_same_epoch_as_new_publishes() -> None:
    # Arrange
    bus = ServerEventBus()
    expected_epoch = bus.epoch
    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})  # sequence 1
    bus.publish(RUN_COMPLETED_SERVER_EVENT, {"id": "a"})  # sequence 2

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
    for event_type in ALLOWED_SERVER_EVENT_TYPES:
        bus.publish(event_type, {"sentinel": event_type})

    # Assert: each event carries the epoch and a unique sequence.
    published = bus.events
    epoch = bus.epoch
    assert len(published) == len(ALLOWED_SERVER_EVENT_TYPES)
    assert len({event["sequence"] for event in published}) == len(published)
    assert all(event["epoch"] == epoch for event in published)


# -- resource_changed is part of the contract allowlist ----------------------


def test_resource_changed_is_in_the_event_contract_allowlist() -> None:
    # Arrange / Assert: the generic invalidation event is a first-class
    # contract event, so /ws clients accept it like any lifecycle event.
    assert RESOURCE_CHANGED_EVENT in ALLOWED_SERVER_EVENT_TYPES


def test_publish_accepts_resource_changed_with_a_kind_payload() -> None:
    # Arrange
    bus = ServerEventBus()

    # Act
    bus.publish(RESOURCE_CHANGED_EVENT, {"kind": RESOURCE_KIND_MODELS})

    # Assert: the payload rides through unchanged (the bus is payload-agnostic).
    event = bus.events[-1]
    assert event["type"] == RESOURCE_CHANGED_EVENT
    assert event["payload"] == {"kind": "models"}


def test_allowed_resource_kinds_lock_the_documented_wire_contract() -> None:
    # Assert: the wire strings (not just the constants) are frozen so an
    # accidental rename of a kind is caught — these are part of the contract.
    assert {
        "models",
        "queue",
        "sessions",
        "agents",
        "providers",
        "clients",
        "channels",
        "debug_traces",
        "projects",
        "cron",
        "calendar",
        "commands",
        "terminals",
        "memories",
        "skills",
        "data_store",
        "extensions",
    } == ALLOWED_RESOURCE_KINDS


# -- integration: the bus re-uses the same uuid module behaviour -------------


def test_event_bus_epoch_is_a_valid_uuid4_hex_value() -> None:
    # Arrange / Act
    bus = ServerEventBus()

    # Assert: the value round-trips through ``uuid.UUID(...).hex`` — the
    # canonical way to assert "this is a uuid4 hex string".
    parsed = uuid.UUID(hex=bus.epoch)
    assert parsed.hex == bus.epoch
    assert parsed.version == 4


# -- publishing from other threads ------------------------------------------


@contextmanager
def _foreign_loop_calls(loop: asyncio.AbstractEventLoop) -> Iterator[list[Any]]:
    """Record ``loop.call_soon`` calls made from threads other than the loop's.

    ``call_soon`` is loop-thread-only; a foreign caller queues a callback
    without waking the loop. ``call_soon_threadsafe`` does not pass through
    it, so a correct handoff records nothing. The calls still run, so a
    failing assertion leaves no stuck task behind.
    """
    owner = threading.get_ident()
    original = loop.call_soon
    foreign: list[Any] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        if threading.get_ident() != owner:
            foreign.append(args[0])
        return original(*args, **kwargs)

    loop.call_soon = recording  # type: ignore[method-assign,assignment]
    try:
        yield foreign
    finally:
        del loop.call_soon


def _run_threads(targets: list[Any]) -> tuple[list[threading.Thread], list[BaseException]]:
    errors: list[BaseException] = []

    def guarded(target: Any) -> None:
        try:
            target()
        except BaseException as error:  # noqa: BLE001 - surfaced by the test
            errors.append(error)

    threads = [threading.Thread(target=guarded, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    return threads, errors


async def _collect(events: AsyncGenerator[dict[str, Any], None], count: int) -> list[Any]:
    return [await anext(events) for _ in range(count)]


@pytest.mark.asyncio
async def test_a_worker_thread_publish_wakes_the_waiting_subscriber_promptly() -> None:
    bus = ServerEventBus()
    with _foreign_loop_calls(asyncio.get_running_loop()) as foreign_calls:
        async with aclosing(bus.subscribe()) as events:
            consumer = asyncio.ensure_future(_collect(events, 1))
            # Let the subscriber register and block on its live queue.
            await asyncio.sleep(0.05)
            started = time.monotonic()
            threads, errors = _run_threads(
                [lambda: bus.publish(RUN_STARTED_SERVER_EVENT, {"run_id": "from-worker"})]
            )
            done, _pending = await asyncio.wait({consumer}, timeout=5)
            elapsed = time.monotonic() - started
            for thread in threads:
                thread.join()

    assert errors == []
    assert foreign_calls == []
    assert done, "the subscriber was not woken by the worker-thread publish"

    [event] = consumer.result()
    assert event["type"] == RUN_STARTED_SERVER_EVENT
    assert event["payload"] == {"run_id": "from-worker"}
    assert event["sequence"] == 1
    # The handoff wakes the idle loop instead of waiting for its next wake-up.
    assert elapsed < 2


@pytest.mark.asyncio
async def test_concurrent_thread_and_loop_publishes_get_unique_contiguous_sequences() -> None:
    bus = ServerEventBus()
    workers, per_worker, on_loop = 6, 40, 40
    total = workers * per_worker + on_loop
    barrier = threading.Barrier(workers)

    def publish_batch(worker: int) -> None:
        barrier.wait()
        for index in range(per_worker):
            bus.publish(RUN_OUTPUT_SERVER_EVENT, {"worker": worker, "index": index})

    with _foreign_loop_calls(asyncio.get_running_loop()) as foreign_calls:
        async with aclosing(bus.subscribe()) as events:
            consumer = asyncio.ensure_future(_collect(events, total))
            await asyncio.sleep(0.05)
            threads, errors = _run_threads(
                [lambda worker=worker: publish_batch(worker) for worker in range(workers)]
            )
            for index in range(on_loop):
                bus.publish(RUN_OUTPUT_SERVER_EVENT, {"worker": "loop", "index": index})
                await asyncio.sleep(0)
            done, _pending = await asyncio.wait({consumer}, timeout=10)
            for thread in threads:
                thread.join()

    assert errors == []
    assert foreign_calls == []
    assert done, "the subscriber did not receive every published event"

    received = consumer.result()
    assert [event["sequence"] for event in received] == list(range(1, total + 1))
    assert bus.last_sequence == total
    # Each publisher's events keep their publish order.
    for publisher in [*range(workers), "loop"]:
        indexes = [
            event["payload"]["index"]
            for event in received
            if event["payload"]["worker"] == publisher
        ]
        expected = per_worker if publisher != "loop" else on_loop
        assert indexes == list(range(expected))


def test_a_publish_after_the_bus_loop_closed_is_discarded() -> None:
    async def create_bus() -> ServerEventBus:
        return ServerEventBus()

    loop = asyncio.new_event_loop()
    try:
        bus = loop.run_until_complete(create_bus())
    finally:
        loop.close()

    bus.publish(APP_ERROR_EVENT, {"message": "after shutdown"})

    assert bus.events == []
    assert bus.last_sequence == 0
