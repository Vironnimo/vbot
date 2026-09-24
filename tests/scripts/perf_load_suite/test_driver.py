"""Workload seeding and per-turn Run following, against stubbed RPC/SSE clients."""

import asyncio
import json
from pathlib import Path

import pytest

from scripts.perf_load_suite.directive import PerfDirective
from scripts.perf_load_suite.driver import (
    UI_AGENT_ID,
    SessionTarget,
    apply_run_event,
    run_turn,
    seed_workload,
)
from scripts.perf_load_suite.fake_provider import format_marker
from scripts.perf_load_suite.metrics import RunRecord
from scripts.perf_load_suite.rpc import SseEvent

DIRECTIVE = PerfDirective(tag="L1-s000-t1", steps=2, tokens=10, rate=80, think_ms=0)
TARGET = SessionTarget(0, "perf-agent-01", "session-0")


class SeedRpc:
    def __init__(self):
        self.calls = []
        self.sessions = 0

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "project.add":
            return {"project": {"project_id": "p1"}, "scan": {}}
        if method == "session.create":
            self.sessions += 1
            return {"agent_id": params["agent_id"], "session_id": f"s{self.sessions}"}
        return {}


def test_sessions_are_spread_over_rooted_identity_agents():
    rpc = SeedRpc()

    workload = seed_workload(
        rpc, fixture_dir=Path("/fixture"), sessions=5, identity_agents=2, dedicated_ui_agent=False
    )

    assert workload.project_id == "p1"
    assert workload.agent_ids == ("perf-agent-01", "perf-agent-02")
    assert [target.agent_id for target in workload.sessions] == [
        "perf-agent-01",
        "perf-agent-02",
        "perf-agent-01",
        "perf-agent-02",
        "perf-agent-01",
    ]
    assert workload.ui_session is None
    rooted = [params for method, params in rpc.calls if method == "agent.update"]
    assert rooted == [
        {"id": "perf-agent-01", "root_project_id": "p1"},
        {"id": "perf-agent-02", "root_project_id": "p1"},
    ]


def test_ui_session_gets_its_own_agent_and_becomes_current():
    rpc = SeedRpc()

    workload = seed_workload(
        rpc, fixture_dir=Path("/fixture"), sessions=3, identity_agents=5, dedicated_ui_agent=True
    )

    assert workload.agent_ids == (UI_AGENT_ID, "perf-agent-01", "perf-agent-02", "perf-agent-03")
    assert workload.ui_session == SessionTarget(0, UI_AGENT_ID, "s1")
    assert [target.agent_id for target in workload.sessions] == [
        UI_AGENT_ID,
        "perf-agent-01",
        "perf-agent-02",
    ]
    assert rpc.calls[-1] == ("agent.update", {"id": UI_AGENT_ID, "current_session_id": "s1"})


def _record():
    return RunRecord(
        tag=DIRECTIVE.tag,
        session_index=0,
        turn_index=0,
        agent_id=TARGET.agent_id,
        session_id=TARGET.session_id,
        directive=DIRECTIVE,
        sent_at=100.0,
    )


def test_run_events_fill_the_record():
    record = _record()

    assert not apply_run_event(record, "run_started", {}, 100.1)
    assert not apply_run_event(
        record,
        "tool_call_result",
        {
            "tool_call": {"id": "c1", "name": "read"},
            "result": {"ok": True},
            "timing": {"duration_ms": 12},
        },
        100.2,
    )
    assert not apply_run_event(
        record, "assistant_output_delta", {"content_delta": f"{format_marker(100.25)}hi"}, 100.3
    )
    assert not apply_run_event(record, "assistant_output_delta", {"content_delta": "more"}, 100.4)
    assert apply_run_event(record, "run_completed", {"status": "completed"}, 100.5)

    assert record.first_event_at == 100.1
    assert record.first_delta_at == 100.3
    assert record.delta_events == 2
    assert record.delta_latencies_ms == pytest.approx([50.0])
    assert [(t.call_id, t.name, t.duration_ms, t.ok) for t in record.tool_timings] == [
        ("c1", "read", 12.0, True)
    ]
    assert (record.status, record.finished_at, record.error) == ("completed", 100.5, None)


def test_failed_run_keeps_its_error():
    record = _record()

    assert apply_run_event(
        record, "run_failed", {"status": "failed", "error": "Provider 500"}, 101.0
    )

    assert (record.status, record.error, record.ok) == ("failed", "Provider 500", False)


class StreamClient:
    def __init__(self, response, events, *, stall=False):
        self.response = response
        self.stream = events
        self.stall = stall
        self.calls = []

    async def call(self, method, params=None):
        self.calls.append((method, params))
        return self.response if method == "chat.stream" else {}

    async def events(self, path):
        assert path == "/api/runs/r1/events"
        for event in self.stream:
            yield event
        if self.stall:
            await asyncio.sleep(10)


def _sse(event_type, payload):
    return SseEvent(event_type, json.dumps({"type": event_type, "payload": payload}), "1")


STARTED = {"run_id": "r1", "sse_url": "/api/runs/r1/events", "status": "running"}


@pytest.mark.asyncio
async def test_turn_follows_the_run_until_its_terminal_event():
    client = StreamClient(
        STARTED,
        [
            SseEvent("heartbeat", "{}", None),
            _sse("assistant_output_delta", {"content_delta": "hello"}),
            _sse("run_completed", {"status": "completed"}),
            _sse("assistant_output_delta", {"content_delta": "ignored"}),
        ],
    )

    record = await run_turn(client, TARGET, 0, DIRECTIVE, timeout_seconds=5)

    assert (record.status, record.run_id, record.delta_events) == ("completed", "r1", 1)
    method, params = client.calls[0]
    assert method == "chat.stream"
    assert params["content"].startswith(DIRECTIVE.render())
    assert (params["agent_id"], params["session_id"]) == (TARGET.agent_id, TARGET.session_id)


@pytest.mark.asyncio
async def test_queued_turn_is_an_error():
    client = StreamClient({"queued": True, "item": {}}, [])

    record = await run_turn(client, TARGET, 0, DIRECTIVE, timeout_seconds=5)

    assert record.status == "error"
    assert "queued" in record.error


@pytest.mark.asyncio
async def test_stream_ending_without_terminal_event_is_an_error():
    client = StreamClient(STARTED, [_sse("assistant_output_delta", {"content_delta": "x"})])

    record = await run_turn(client, TARGET, 0, DIRECTIVE, timeout_seconds=5)

    assert record.status == "error"
    assert "without a terminal event" in record.error


@pytest.mark.asyncio
async def test_timed_out_turn_is_cancelled():
    client = StreamClient(STARTED, [], stall=True)

    record = await run_turn(client, TARGET, 0, DIRECTIVE, timeout_seconds=0.05)

    assert record.status == "timeout"
    assert client.calls[-1] == ("chat.cancel", {"run_id": "r1", "reason": "perf-load timeout"})
