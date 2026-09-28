"""The shared in-process replay event stream: replay/live handoff, retention and lag eviction."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.event_stream import ReplayEventStream

pytestmark = pytest.mark.asyncio

Event = dict[str, int]


def _stream(*, retention: int = 100, queue: int = 100, **options: Any) -> ReplayEventStream[Event]:
    return ReplayEventStream[Event](
        event_retention_limit=retention,
        subscriber_queue_limit=queue,
        sequence_of=lambda event: event["sequence"],
        **options,
    )


def _publish(stream: ReplayEventStream[Event], sequences: range, **fields: int) -> None:
    for sequence in sequences:
        stream.publish({"sequence": sequence, **fields})


async def test_subscribe_replays_retained_events_then_live_events() -> None:
    stream = _stream()
    _publish(stream, range(1, 3))
    received: list[int] = []

    async def consumer() -> None:
        async for event in stream.subscribe(after_sequence=0):
            received.append(event["sequence"])
            if event["sequence"] == 1:
                # Published while the consumer is still inside historical replay.
                stream.publish({"sequence": 3})
            if event["sequence"] >= 4:
                return

    async def producer() -> None:
        while len(received) < 3:
            await asyncio.sleep(0)
        stream.publish({"sequence": 4})

    async with asyncio.timeout(2):
        await asyncio.gather(consumer(), producer())

    assert received == [1, 2, 3, 4]
    assert stream.subscriber_count == 0


async def test_slow_historical_replay_is_not_evicted_by_live_traffic() -> None:
    """Live publishes during a large retained replay must not kill the subscriber.

    Before the fix, subscribe registered for live fan-out first. A slow walk of
    the retention window let the bounded live queue overflow and silently end
    the stream mid-replay (WebSocket/SSE clients saw a dead push channel).
    """
    stream = _stream(queue=5)
    _publish(stream, range(1, 21))
    received: list[int] = []

    async def consumer() -> None:
        async for event in stream.subscribe(after_sequence=0):
            received.append(event["sequence"])
            # Yield so concurrent live publishes run while replay is in progress.
            await asyncio.sleep(0)
            if event["sequence"] >= 39:
                return

    async def producer() -> None:
        await asyncio.sleep(0)
        for sequence in range(21, 40):
            stream.publish({"sequence": sequence})
            await asyncio.sleep(0)

    await asyncio.gather(consumer(), producer())

    assert received == list(range(1, 40))
    assert stream.subscriber_count == 0


@pytest.mark.parametrize(
    ("retention", "published", "after", "expected"),
    [(100, 3, 2, [3]), (3, 7, 2, [5, 6, 7])],
    ids=["skips-at-or-below-cursor", "head-starts-after-dropped-events"],
)
async def test_replay_starts_after_the_cursor_or_at_the_retained_head(
    retention: int, published: int, after: int, expected: list[int]
) -> None:
    stream = _stream(retention=retention)
    _publish(stream, range(1, published + 1))

    received = [
        event["sequence"] async for event in stream.subscribe(after_sequence=after, live=False)
    ]

    assert received == expected


async def test_subscribe_stops_on_terminal_event() -> None:
    stream = _stream(terminal_when=lambda event: event.get("terminal", 0) == 1)
    stream.publish({"sequence": 1})
    stream.publish({"sequence": 2, "terminal": 1})
    stream.publish({"sequence": 3})

    received = [event["sequence"] async for event in stream.subscribe(after_sequence=0)]

    assert received == [1, 2]
    assert stream.subscriber_count == 0


@pytest.mark.parametrize(
    ("options", "size"),
    [({"queue": 2}, 1), ({"byte_limit": 10, "size_of": lambda event: event["size"]}, 4)],
    ids=["event-count", "byte-budget"],
)
async def test_live_subscriber_is_evicted_when_it_lags_after_catch_up(
    options: dict[str, Any], size: int
) -> None:
    stream = _stream(**options)

    async with asyncio.timeout(2):
        iterator = stream.subscribe(after_sequence=0)
        first = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        stream.publish({"sequence": 1, "size": size})
        assert (await first)["sequence"] == 1

        _publish(stream, range(2, 5), size=size)

        assert stream.subscriber_count == 0
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await iterator.aclose()


@pytest.mark.parametrize(
    ("kept", "limit", "expected"),
    [
        ([1, 0, 1, 1, 1, 1], 10, [3, 4, 5, 6]),
        ([1, 1, 1, 1, 1, 1], 2, [5, 6]),
        ([0, 0, 0, 0, 0, 0], 10, [6]),
    ],
    ids=["stops-at-first-unkept", "capped", "newest-always-kept"],
)
async def test_compact_keeps_the_newest_contiguous_suffix_as_replay_head(
    kept: list[int], limit: int, expected: list[int]
) -> None:
    stream = _stream(byte_limit=100, size_of=lambda event: event["size"])
    for sequence, keep in enumerate(kept, start=1):
        stream.publish({"sequence": sequence, "keep": keep, "size": 10})

    stream.compact(limit=limit, keep=lambda event: event["keep"] == 1)

    assert [event["sequence"] for event in stream.events] == expected
    replayed = [event["sequence"] async for event in stream.subscribe(live=False)]
    assert replayed == expected
    # Compaction released the byte budget of everything it dropped.
    _publish(stream, range(7, 7 + 10 - len(expected)), size=10, keep=1)
    assert stream.events[0]["sequence"] == expected[0]


async def test_byte_budget_evicts_old_replay_and_oversized_events() -> None:
    stream = _stream(retention=10, queue=10, byte_limit=10, size_of=lambda event: event["size"])
    _publish(stream, range(1, 5), size=4)
    assert [event["sequence"] for event in stream.events] == [3, 4]

    stream.publish({"sequence": 5, "size": 11})
    assert stream.events == []

    stream.publish({"sequence": 6, "size": 2})
    assert [event["sequence"] async for event in stream.subscribe(live=False)] == [6]


async def test_events_lost_from_retention_during_historical_replay_evict_subscriber() -> None:
    lagged: list[bool] = []
    stream = _stream(retention=10, queue=10, on_lagged=lambda: lagged.append(True))
    _publish(stream, range(1, 11))
    received: list[int] = []

    async with asyncio.timeout(2):
        async for event in stream.subscribe(after_sequence=0):
            received.append(event["sequence"])
            if event["sequence"] == 1:
                _publish(stream, range(11, 41))

    assert received == list(range(1, 11))
    assert lagged == [True]
    assert stream.subscriber_count == 0


async def test_events_lost_from_retention_during_catch_up_evict_subscriber() -> None:
    lagged: list[bool] = []
    stream = _stream(retention=10, queue=10, on_lagged=lambda: lagged.append(True))
    _publish(stream, range(1, 4))
    received: list[int] = []

    async with asyncio.timeout(2):
        async for event in stream.subscribe(after_sequence=0):
            received.append(event["sequence"])
            if event["sequence"] == 1:
                # Retained for the catch-up scan after historical replay.
                stream.publish({"sequence": 4})
            if event["sequence"] == 4:
                # The catch-up subscriber's queue keeps 5..14 and drops the
                # rest; retention keeps only 25..34, so 15..24 are gone.
                _publish(stream, range(5, 35))

    assert received == list(range(1, 15))
    assert lagged == [True]
    assert stream.subscriber_count == 0
