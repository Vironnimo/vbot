"""Regression tests for the shared in-process replay event stream."""

from __future__ import annotations

import asyncio

import pytest

from core.event_stream import ReplayEventStream


def _sequence_of(event: dict[str, int]) -> int:
    return event["sequence"]


@pytest.mark.asyncio
async def test_subscribe_replays_retained_events_then_live_events() -> None:
    stream = ReplayEventStream[dict[str, int]](
        event_retention_limit=100,
        subscriber_queue_limit=100,
        sequence_of=_sequence_of,
    )
    stream.publish({"sequence": 1})
    stream.publish({"sequence": 2})

    received: list[int] = []

    async def consumer() -> None:
        async for event in stream.subscribe(after_sequence=0):
            received.append(event["sequence"])
            if event["sequence"] >= 3:
                return

    async def producer() -> None:
        await asyncio.sleep(0)
        stream.publish({"sequence": 3})

    await asyncio.gather(consumer(), producer())

    assert received == [1, 2, 3]


@pytest.mark.asyncio
async def test_slow_historical_replay_is_not_evicted_by_live_traffic() -> None:
    """Live publishes during a large retained replay must not kill the subscriber.

    Before the fix, subscribe registered for live fan-out first. A slow walk of
    the retention window let the bounded live queue overflow and silently end
    the stream mid-replay (WebSocket/SSE clients saw a dead push channel).
    """

    stream = ReplayEventStream[dict[str, int]](
        event_retention_limit=100,
        subscriber_queue_limit=5,
        sequence_of=_sequence_of,
    )
    for sequence in range(1, 21):
        stream.publish({"sequence": sequence})

    received: list[int] = []

    async def consumer() -> None:
        async for event in stream.subscribe(after_sequence=0):
            received.append(event["sequence"])
            # Yield to the event loop so concurrent live publishes can run
            # while historical replay is still in progress.
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


@pytest.mark.asyncio
async def test_live_subscriber_is_still_evicted_when_it_lags_after_catch_up() -> None:
    stream = ReplayEventStream[dict[str, int]](
        event_retention_limit=100,
        subscriber_queue_limit=2,
        sequence_of=_sequence_of,
    )

    async with asyncio.timeout(2):
        generator = stream.subscribe(after_sequence=0)
        first_event_task = asyncio.create_task(generator.__anext__())
        await asyncio.sleep(0)

        stream.publish({"sequence": 1})
        first_event = await first_event_task
        assert first_event["sequence"] == 1

        stream.publish({"sequence": 2})
        stream.publish({"sequence": 3})
        stream.publish({"sequence": 4})

        assert stream.subscriber_count == 0
        with pytest.raises(StopAsyncIteration):
            await generator.__anext__()
        await generator.aclose()


@pytest.mark.asyncio
async def test_subscribe_skips_events_at_or_below_after_sequence() -> None:
    stream = ReplayEventStream[dict[str, int]](
        event_retention_limit=100,
        subscriber_queue_limit=100,
        sequence_of=_sequence_of,
    )
    stream.publish({"sequence": 1})
    stream.publish({"sequence": 2})
    stream.publish({"sequence": 3})

    received: list[int] = []
    async for event in stream.subscribe(after_sequence=2, live=False):
        received.append(event["sequence"])

    assert received == [3]


@pytest.mark.asyncio
async def test_subscribe_stops_on_terminal_event() -> None:
    def sequence_of(event: dict[str, int]) -> int:
        return event["sequence"]

    def is_terminal(event: dict[str, int]) -> bool:
        return event.get("terminal", 0) == 1

    stream = ReplayEventStream[dict[str, int]](
        event_retention_limit=100,
        subscriber_queue_limit=100,
        sequence_of=sequence_of,
        terminal_when=is_terminal,
    )
    stream.publish({"sequence": 1})
    stream.publish({"sequence": 2, "terminal": 1})
    stream.publish({"sequence": 3})

    received: list[int] = []
    async for event in stream.subscribe(after_sequence=0):
        received.append(event["sequence"])

    assert received == [1, 2]
    assert stream.subscriber_count == 0


@pytest.mark.asyncio
async def test_events_published_during_historical_replay_are_not_lost() -> None:
    stream = ReplayEventStream[dict[str, int]](
        event_retention_limit=100,
        subscriber_queue_limit=100,
        sequence_of=_sequence_of,
    )
    stream.publish({"sequence": 1})
    stream.publish({"sequence": 2})

    received: list[int] = []

    async def consumer() -> None:
        async for event in stream.subscribe(after_sequence=0):
            received.append(event["sequence"])
            if event["sequence"] == 1:
                # Publish while the consumer is still inside historical replay.
                stream.publish({"sequence": 3})
            if event["sequence"] >= 3:
                return

    await consumer()
    assert received == [1, 2, 3]
