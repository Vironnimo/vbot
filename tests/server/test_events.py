"""Tests for the ServerEventBus contract.

Every bus has a stable ``epoch`` (``/ws`` clients use it to detect a server
restart) and stamps each published event with that epoch and a contiguous
``sequence``. Subscribers replay retained events after their cursor and then
follow live publishes, which may come from any thread.
"""

from __future__ import annotations

import asyncio
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
    RUN_COMPLETED_SERVER_EVENT,
    RUN_OUTPUT_SERVER_EVENT,
    RUN_STARTED_SERVER_EVENT,
    ServerEventBus,
)

# -- epoch and sequence -------------------------------------------------------


def test_each_bus_has_a_stable_read_only_uuid4_epoch() -> None:
    bus = ServerEventBus()
    epoch = bus.epoch

    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})

    # A lowercase uuid4 hex string that round-trips through ``uuid.UUID``.
    parsed = uuid.UUID(hex=epoch)
    assert (parsed.hex, parsed.version) == (epoch, 4)
    assert bus.epoch == epoch
    # A per-instance generation, not a class-level value.
    assert len({epoch, *(ServerEventBus().epoch for _ in range(32))}) == 33
    with pytest.raises(AttributeError):
        bus.epoch = "tampered"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del bus.epoch  # type: ignore[misc]
    assert bus.epoch == epoch


def test_publish_stamps_the_epoch_and_a_contiguous_sequence() -> None:
    bus = ServerEventBus()
    assert bus.last_sequence == 0

    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})
    bus.publish(RUN_COMPLETED_SERVER_EVENT, {"id": "a"})
    bus.publish(APP_ERROR_EVENT, payload=None)

    assert [
        (event["type"], event["epoch"], event["sequence"], event["payload"]) for event in bus.events
    ] == [
        (RUN_STARTED_SERVER_EVENT, bus.epoch, 1, {"id": "a"}),
        (RUN_COMPLETED_SERVER_EVENT, bus.epoch, 2, {"id": "a"}),
        (APP_ERROR_EVENT, bus.epoch, 3, {}),
    ]
    assert bus.last_sequence == 3
    with pytest.raises(AttributeError):
        bus.last_sequence = 999  # type: ignore[misc]


# -- event contract -----------------------------------------------------------


def test_publish_accepts_every_contract_event_type_and_rejects_unknown_ones() -> None:
    bus = ServerEventBus()
    event_types = sorted(ALLOWED_SERVER_EVENT_TYPES)

    for event_type in event_types:
        bus.publish(event_type, {"sentinel": event_type})
    with pytest.raises(ValueError):
        bus.publish("unknown.event", {"message": "No contract"})

    # The bus is payload-agnostic: every payload rides through unchanged.
    assert [(event["type"], event["payload"]) for event in bus.events] == [
        (event_type, {"sentinel": event_type}) for event_type in event_types
    ]
    assert {APP_ERROR_EVENT, RESOURCE_CHANGED_EVENT} <= ALLOWED_SERVER_EVENT_TYPES


def test_allowed_resource_kinds_lock_the_documented_wire_contract() -> None:
    # The wire strings, not just the constants, are frozen so an accidental
    # rename of a kind is caught.
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


# -- replay and live delivery -------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_replays_events_after_the_cursor_then_follows_live_ones() -> None:
    bus = ServerEventBus()
    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})
    bus.publish(RUN_OUTPUT_SERVER_EVENT, {"id": "a"})
    bus.publish(RUN_COMPLETED_SERVER_EVENT, {"id": "a"})

    async with (
        aclosing(bus.subscribe()) as everything,
        aclosing(bus.subscribe(after_sequence=1)) as newer,
        aclosing(bus.subscribe(after_sequence=4)) as ahead,
    ):
        replayed_all = [await anext(everything) for _ in range(3)]
        replayed_newer = [await anext(newer) for _ in range(2)]
        # A cursor ahead of the bus replays nothing and waits live.
        first_ahead = asyncio.ensure_future(anext(ahead))
        async with asyncio.timeout(1):
            while bus.subscriber_count == 0:
                await asyncio.sleep(0)
        bus.publish(APP_ERROR_EVENT, {"message": "live"})
        bus.publish(APP_ERROR_EVENT, {"message": "past the cursor"})
        live = [await anext(everything), await anext(newer), await first_ahead]

    assert [event["sequence"] for event in replayed_all] == [1, 2, 3]
    assert [(event["sequence"], event["type"]) for event in replayed_newer] == [
        (2, RUN_OUTPUT_SERVER_EVENT),
        (3, RUN_COMPLETED_SERVER_EVENT),
    ]
    assert [(event["sequence"], event["payload"]["message"]) for event in live] == [
        (4, "live"),
        (4, "live"),
        (5, "past the cursor"),
    ]
    assert all(event["epoch"] == bus.epoch for event in [*replayed_all, *replayed_newer, *live])
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_event_bus_replay_window_is_bounded_without_reusing_sequences() -> None:
    bus = ServerEventBus(event_retention_limit=2)
    bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})
    bus.publish(RUN_OUTPUT_SERVER_EVENT, {"id": "a"})
    bus.publish(RUN_COMPLETED_SERVER_EVENT, {"agent_id": "a"})

    async with aclosing(bus.subscribe(after_sequence=0)) as events:
        replayed = [await anext(events) for _ in range(2)]

    assert [event["sequence"] for event in bus.events] == [2, 3]
    assert [(event["sequence"], event["type"]) for event in replayed] == [
        (2, RUN_OUTPUT_SERVER_EVENT),
        (3, RUN_COMPLETED_SERVER_EVENT),
    ]


@pytest.mark.asyncio
async def test_event_bus_evicts_lagging_live_subscriber() -> None:
    bus = ServerEventBus(subscriber_queue_limit=2)

    async with aclosing(bus.subscribe()) as gen:
        first_event_task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)

        bus.publish(RUN_STARTED_SERVER_EVENT, {"id": "a"})
        first_event = await first_event_task

        bus.publish(RUN_OUTPUT_SERVER_EVENT, {"id": "a"})
        bus.publish(RUN_OUTPUT_SERVER_EVENT, {"id": "a"})
        bus.publish(RUN_COMPLETED_SERVER_EVENT, {"agent_id": "a"})

        assert first_event["type"] == RUN_STARTED_SERVER_EVENT
        assert bus.subscriber_count == 0
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()


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
