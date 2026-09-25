"""Live Run announcements: which finished Runs reach the call, and how."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from core.model_tasks.live import LiveRunNotice
from server._live_feed import LiveRunFeed
from server.events import ServerEventBus
from server.rpc.errors import RpcError

JsonObject = dict[str, Any]

STARTED_AT = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


async def settle(predicate: Callable[[], bool]) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class FakeRunResults:
    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.error: Exception | None = None

    async def __call__(self, method: str, params: JsonObject) -> JsonObject:
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return {
            "run_id": params["run_id"],
            "content": f"reply {params['run_id']}",
            "truncated": True,
        }


class Harness:
    def __init__(self, bus: ServerEventBus | None = None) -> None:
        self.bus = bus or ServerEventBus()
        self.rpc = FakeRunResults()
        self.notices: list[LiveRunNotice] = []
        self.failures = 0
        self.announce_error: Exception | None = None
        self.refs: dict[tuple[str, str], str] = {}
        self.feed = LiveRunFeed(
            events=self.bus,
            rpc=self.rpc,
            announce=self._announce,
            report_failure=self._report_failure,
            describe_session=self._describe_session,
            started_at=STARTED_AT,
            after_sequence=self.bus.last_sequence,
        )

    def _announce(self, notice: LiveRunNotice) -> None:
        if self.announce_error is not None:
            error, self.announce_error = self.announce_error, None
            raise error
        self.notices.append(notice)

    def _describe_session(self, agent_id: str, session_id: str) -> str:
        return self.refs.setdefault((agent_id, session_id), f"s{len(self.refs) + 1}")

    def _report_failure(self) -> None:
        self.failures += 1

    def finish(
        self,
        run_id: str,
        *,
        event_type: str = "run_completed",
        at: datetime | None = None,
        **fields: Any,
    ) -> None:
        payload: JsonObject = {
            "run_id": run_id,
            "agent_id": "joel",
            "project_id": "vbot",
            "session_id": "s1",
            "run_event_timestamp": (at or STARTED_AT + timedelta(seconds=5)).isoformat(),
            **fields,
        }
        self.bus.publish(event_type, payload)


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[Harness]:
    current = Harness()
    current.feed.start()
    try:
        yield current
    finally:
        await current.feed.aclose()


@pytest.mark.asyncio
async def test_announces_finished_runs_with_their_outcome_and_exact_address(
    harness: Harness,
) -> None:
    harness.finish("r1")
    harness.finish("r2", event_type="run_failed", project_id=None)
    harness.finish("r3", event_type="run_interrupted")
    await settle(lambda: len(harness.notices) == 3)
    assert harness.notices[0] == LiveRunNotice(
        kind="completed",
        run_id="r1",
        agent_id="joel@vbot",
        session_id="s1",
        excerpt="reply r1",
        truncated=True,
        session_ref="s1",
    )
    assert [(notice.kind, notice.agent_id) for notice in harness.notices[1:]] == [
        ("failed", "joel"),
        ("interrupted", "joel@vbot"),
    ]
    assert harness.rpc.calls[0] == (
        "chat.run_result",
        {"agent_id": "joel@vbot", "session_id": "s1", "run_id": "r1"},
    )


@pytest.mark.asyncio
async def test_skips_cancelled_background_earlier_and_incomplete_runs(harness: Harness) -> None:
    harness.finish("cancelled", event_type="run_cancelled")
    harness.finish("background", contributes_to_agent_activity=False)
    harness.finish("earlier", at=STARTED_AT - timedelta(seconds=1))
    harness.finish("no-agent", agent_id="")
    harness.finish("no-session", session_id=None)
    harness.bus.publish("run_started", {"run_id": "started", "agent_id": "joel"})
    harness.finish("last")
    await settle(lambda: len(harness.notices) == 1)
    await drain()
    assert [notice.run_id for notice in harness.notices] == ["last"]
    assert [params["run_id"] for _method, params in harness.rpc.calls] == ["last"]


@pytest.mark.asyncio
async def test_announces_each_run_once(harness: Harness) -> None:
    harness.finish("r1")
    harness.finish("r1", event_type="run_failed")
    harness.finish("r2")
    await settle(lambda: len(harness.notices) == 2)
    await drain()
    assert [notice.run_id for notice in harness.notices] == ["r1", "r2"]


@pytest.mark.asyncio
async def test_does_not_replay_runs_published_before_the_call() -> None:
    bus = ServerEventBus()
    bus.publish(
        "run_completed",
        {
            "run_id": "old",
            "agent_id": "joel",
            "session_id": "s1",
            "run_event_timestamp": (STARTED_AT + timedelta(seconds=1)).isoformat(),
        },
    )
    current = Harness(bus)
    current.feed.start()
    try:
        current.finish("new")
        await settle(lambda: len(current.notices) == 1)
        await drain()
        assert [notice.run_id for notice in current.notices] == ["new"]
    finally:
        await current.feed.aclose()


@pytest.mark.asyncio
async def test_reports_an_unavailable_result_and_keeps_following(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.rpc.error = RpcError("domain_error", "Run result not found")
    with caplog.at_level(logging.WARNING, logger="vbot.server.live"):
        harness.finish("gone")
        await settle(lambda: harness.failures == 1)
    assert harness.notices == []
    assert "code=domain_error" in caplog.text
    harness.rpc.error = None
    harness.finish("r2")
    await settle(lambda: len(harness.notices) == 1)


@pytest.mark.asyncio
async def test_reports_an_unexpected_result_failure(harness: Harness) -> None:
    harness.rpc.error = RuntimeError("fixture failure")
    harness.finish("r1")
    await settle(lambda: harness.failures == 1)
    assert harness.notices == []


@pytest.mark.asyncio
async def test_a_rejected_announcement_does_not_stop_the_feed(harness: Harness) -> None:
    harness.announce_error = RuntimeError("fixture rejection")
    harness.finish("r1")
    harness.finish("r2")
    await settle(lambda: len(harness.notices) == 1)
    assert harness.notices[0].run_id == "r2"
    assert harness.failures == 0


@pytest.mark.asyncio
async def test_resumes_after_the_bus_evicts_a_lagging_feed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    current = Harness(ServerEventBus(subscriber_queue_limit=1))
    current.feed.start()
    try:
        await drain()
        with caplog.at_level(logging.WARNING, logger="vbot.server.events"):
            for index in range(5):
                current.finish(f"r{index}")
        assert "Evicted lagging server event subscriber" in caplog.text
        await settle(lambda: len(current.notices) == 5)
        assert [notice.run_id for notice in current.notices] == [f"r{i}" for i in range(5)]
    finally:
        await current.feed.aclose()


@pytest.mark.asyncio
async def test_closing_stops_following_the_bus(harness: Harness) -> None:
    await drain()
    assert harness.bus.subscriber_count == 1
    await harness.feed.aclose()
    assert harness.bus.subscriber_count == 0
    harness.finish("late")
    await drain()
    assert harness.notices == []
