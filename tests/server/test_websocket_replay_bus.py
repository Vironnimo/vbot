"""Tests for websocket replay bus."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Any

import pytest

from server._streams import _parse_after_sequence
from server.events import ServerEventBus


# -- Unit tests for _parse_after_sequence --
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        ("0", 0),
        ("5", 5),
        ("100", 100),
        ("-1", 0),
        ("-999", 0),
        ("abc", 0),
        ("3.14", 0),
        ("", 0),
    ],
)
def test_parse_after_sequence_valid_and_invalid_inputs(raw: str | None, expected: int) -> None:
    assert _parse_after_sequence(raw) == expected


# -- Unit tests for ServerEventBus.subscribe with after_sequence --
@pytest.mark.asyncio
async def test_event_bus_subscribe_replays_only_newer_events() -> None:
    bus = ServerEventBus()
    bus.publish("run_started", {"id": "a"})  # sequence 1
    bus.publish("run_output", {"id": "a"})  # sequence 2
    bus.publish("run_completed", {"agent_id": "a"})  # sequence 3

    events: list[dict[str, Any]] = []
    async for event in bus.subscribe(after_sequence=1):
        events.append(event)
        if len(events) == 2:
            break

    assert events[0]["sequence"] == 2
    assert events[0]["type"] == "run_output"
    assert events[1]["sequence"] == 3
    assert events[1]["type"] == "run_completed"


@pytest.mark.asyncio
async def test_event_bus_subscribe_after_sequence_zero_receives_all_events() -> None:
    bus = ServerEventBus()
    bus.publish("run_started", {"id": "a"})

    events: list[dict[str, Any]] = []
    async for event in bus.subscribe(after_sequence=0):
        events.append(event)
        break

    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["type"] == "run_started"


@pytest.mark.asyncio
async def test_event_bus_replay_window_is_bounded_without_reusing_sequences() -> None:
    bus = ServerEventBus(event_retention_limit=2)
    bus.publish("run_started", {"id": "a"})
    bus.publish("run_output", {"id": "a"})
    bus.publish("run_completed", {"agent_id": "a"})

    events: list[dict[str, Any]] = []
    async for event in bus.subscribe(after_sequence=0):
        events.append(event)
        if len(events) == 2:
            break

    assert [event["sequence"] for event in bus.events] == [2, 3]
    assert [event["sequence"] for event in events] == [2, 3]
    assert [event["type"] for event in events] == ["run_output", "run_completed"]


@pytest.mark.asyncio
async def test_event_bus_evicts_lagging_live_subscriber() -> None:
    bus = ServerEventBus(subscriber_queue_limit=2)

    async with aclosing(bus.subscribe()) as gen:
        first_event_task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)

        bus.publish("run_started", {"id": "a"})
        first_event = await first_event_task

        bus.publish("run_output", {"id": "a"})
        bus.publish("run_output", {"id": "a"})
        bus.publish("run_completed", {"agent_id": "a"})

        assert first_event["type"] == "run_started"
        assert bus.subscriber_count == 0
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()


@pytest.mark.asyncio
async def test_event_bus_subscribe_after_sequence_higher_skips_all_replays() -> None:
    """When after_sequence exceeds all existing sequences, no events are replayed.
    The subscriber goes straight to the live subscription loop."""
    bus = ServerEventBus()
    bus.publish("run_started", {"id": "a"})  # sequence 1

    async with aclosing(bus.subscribe(after_sequence=100)) as gen:
        # No replayed events — __anext__ enters the live queue wait immediately.
        # Use wait_for to confirm it does NOT return a replayed event quickly.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gen.__anext__(), timeout=0.25)

    # Generator closed — subscriber removed
    assert bus.subscriber_count == 0
