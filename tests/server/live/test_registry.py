"""Live call ownership: one active call, its owner socket, what the app shows, UI requests,
the call's Live Tools, Run announcements and shutdown."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]

from core.model_tasks.live import LiveCallHost, LiveRunNotice, LiveStartRejected
from server.app import create_app
from server.events import ServerEventBus
from server.live.owner import (
    LIVE_AUDIO_FRAME_MAX_BYTES,
    LIVE_SOCKET_CLOSE_ENDED,
    LIVE_SOCKET_CLOSE_LAGGED,
    LIVE_SOCKET_CLOSE_REPLACED,
    LiveOwnerStream,
)
from server.live.registry import LiveCallLimits, LiveCallRegistry, LiveRegistryClosedError
from server.rpc.errors import RpcError
from tests.server.rpc_test_support import StubAdapter, StubRuntime

JsonObject = dict[str, Any]

STARTED_AT = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
FAST = LiveCallLimits(
    attach_timeout_seconds=5.0,
    reattach_grace_seconds=5.0,
    ui_request_timeout_seconds=5.0,
    shutdown_close_timeout_seconds=0.1,
    abort_timeout_seconds=0.5,
    end_call_delay_seconds=0.01,
)


async def settle(predicate: Callable[[], bool]) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class FakeCall:
    """A provider call that closes when asked and records announcements."""

    def __init__(self, call_id: str, host: LiveCallHost, media: str = "webrtc") -> None:
        self.id = call_id
        self.log_id = "live_" + call_id.replace("-", "")
        self.host = host
        self.media: JsonObject = (
            {"type": "relay", "audio": {"encoding": "pcm16", "sample_rate": 24000, "channels": 1}}
            if media == "relay"
            else {"type": "webrtc", "sdp": f"answer-{call_id}"}
        )
        self.audio: list[bytes] = []
        self.notices: list[LiveRunNotice] = []
        self.close_calls = 0
        self.abort_calls = 0
        self.close_mode = "finish"
        self.announce_error: Exception | None = None
        self._closed = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_mode == "hang":
            await asyncio.Event().wait()
        if self.close_mode == "fail":
            raise RuntimeError("fixture close failure")
        self.finish("user_stopped")

    async def abort(self) -> None:
        self.abort_calls += 1
        self._closed.set()

    def announce_run(self, notice: LiveRunNotice) -> None:
        if self.announce_error is not None:
            error, self.announce_error = self.announce_error, None
            raise error
        self.notices.append(notice)

    def push_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def finish(self, reason: str | None) -> None:
        self.host.publish({"type": "closed", "reason": reason, "usage": {"total_tokens": 3}})
        self._closed.set()


class FakeService:
    """The Live voice service double creating :class:`FakeCall` objects."""

    def __init__(self) -> None:
        self.calls: list[FakeCall] = []
        self.starts: list[tuple[str, str | None]] = []
        self.rejection: str | None = None
        self.gate: asyncio.Event | None = None

    async def start_call(
        self, *, media: str, offer_sdp: str | None, host: LiveCallHost
    ) -> FakeCall:
        if self.rejection is not None:
            raise LiveStartRejected(self.rejection)
        if self.gate is not None:
            await self.gate.wait()
        call = FakeCall(f"call-{len(self.calls) + 1}", host, media)
        self.calls.append(call)
        self.starts.append((media, offer_sdp))
        return call


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, method: str, params: JsonObject) -> JsonObject:
        self.calls.append((method, params))
        if self.gate is not None:
            gate, self.gate = self.gate, None
            await gate.wait()
        if self.error is not None:
            raise self.error
        answers: dict[str, JsonObject] = {
            "chat.run_result": {"content": "Done.", "truncated": True},
            "chat.stream": {"run_id": "run_1", "session_id": "ses_new"},
            "agent.list": {"agents": [{"id": "joel", "name": "Joel"}]},
            "project.list": {"projects": []},
            "terminal.list": {"terminals": [], "groups": []},
        }
        return answers.get(method, {"sessions": []})


async def run_tool(call: FakeCall, name: str, arguments: JsonObject) -> JsonObject:
    """Run one Live Tool call through the call's host and return its result."""
    return await call.host.run_live_tool(name, arguments)


class OwnerReader:
    """Read an owner stream the way the socket route does."""

    def __init__(self, owner: LiveOwnerStream) -> None:
        self.owner = owner
        self.frames: list[Any] = []
        self.done = False
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async with aclosing(self.owner.frames()) as frames:
            async for frame in frames:
                self.frames.append(frame)
        self.done = True

    def types(self) -> list[str]:
        return [
            "audio" if isinstance(frame, bytes) else str(frame["type"]) for frame in self.frames
        ]

    async def stop(self) -> None:
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task


class Harness:
    def __init__(
        self,
        limits: LiveCallLimits = FAST,
        *,
        bus: ServerEventBus | None = None,
    ) -> None:
        self.bus = bus or ServerEventBus()
        self.rpc = FakeRpc()
        self.service = FakeService()
        self.registry = LiveCallRegistry(
            events=self.bus,
            rpc=self.rpc,
            limits=limits,
            clock=lambda: STARTED_AT,
        )
        self.readers: list[OwnerReader] = []

    async def start(self, sdp: str = "v=0 offer") -> FakeCall:
        await self.registry.start(self.service, media="webrtc", offer_sdp=sdp)
        return self.service.calls[-1]

    async def start_relay(self, wake_phrases: tuple[str, ...] = ()) -> FakeCall:
        await self.registry.start(self.service, media="relay", wake_phrases=wake_phrases)
        return self.service.calls[-1]

    def attach(self, call: FakeCall) -> OwnerReader:
        owner = self.registry.attach(call.id)
        assert owner is not None
        reader = OwnerReader(owner)
        self.readers.append(reader)
        return reader

    async def close(self) -> None:
        await self.registry.aclose()
        for reader in self.readers:
            await reader.stop()


@pytest_asyncio.fixture
async def live() -> AsyncIterator[Harness]:
    harness = Harness()
    try:
        yield harness
    finally:
        await harness.close()


# -- start and replacement ----------------------------------------------------


@pytest.mark.asyncio
async def test_start_buffers_updates_until_the_owner_attaches(live: Harness) -> None:
    call = await live.start()
    assert live.service.starts == [("webrtc", "v=0 offer")]
    assert live.registry.active_call_id == call.id
    call.host.publish({"type": "state", "phase": "connecting"})
    call.host.publish({"type": "state", "phase": "live"})
    reader = live.attach(call)
    call.host.publish({"type": "caption", "role": "user", "text": "hi", "final": True})
    await settle(lambda: len(reader.frames) == 3)
    assert [frame.get("phase", frame["type"]) for frame in reader.frames] == [
        "connecting",
        "live",
        "caption",
    ]


@pytest.mark.asyncio
async def test_keeps_only_the_newest_updates_while_no_owner_is_attached() -> None:
    harness = Harness(LiveCallLimits(update_buffer_limit=3, owner_queue_limit=3))
    try:
        call = await harness.start()
        for index in range(5):
            call.host.publish({"type": "activity", "busy": True, "label": str(index)})
        reader = harness.attach(call)
        await settle(lambda: len(reader.frames) == 3)
        assert [frame["label"] for frame in reader.frames] == ["2", "3", "4"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_new_start_replaces_and_aborts_the_active_call(live: Harness) -> None:
    first = await live.start()
    reader = live.attach(first)
    second = await live.start()
    await settle(lambda: reader.done)
    assert reader.frames == [{"type": "closed", "reason": "replaced", "usage": None}]
    assert reader.owner.close_code == LIVE_SOCKET_CLOSE_ENDED
    assert first.abort_calls == 1
    assert first.close_calls == 0
    assert live.registry.active_call_id == second.id
    assert second.abort_calls == 0


@pytest.mark.asyncio
async def test_a_rejected_start_propagates_after_ending_the_previous_call(live: Harness) -> None:
    first = await live.start()
    live.service.rejection = "not_configured"
    with pytest.raises(LiveStartRejected) as exc_info:
        await live.start()
    assert exc_info.value.code == "not_configured"
    assert first.abort_calls == 1
    assert live.registry.active_call_id is None


def test_limits_must_let_an_owner_receive_the_whole_buffer() -> None:
    with pytest.raises(ValueError, match="owner_queue_limit"):
        LiveCallLimits(update_buffer_limit=10, owner_queue_limit=5)


# -- owner socket ownership ---------------------------------------------------


@pytest.mark.asyncio
async def test_ends_a_call_whose_owner_never_attaches() -> None:
    harness = Harness(LiveCallLimits(attach_timeout_seconds=0.05))
    try:
        call = await harness.start()
        await settle(lambda: call.abort_calls == 1)
        await settle(lambda: harness.registry.active_call_id is None)
        # The final update stays attachable briefly for a late owner socket.
        reader = harness.attach(call)
        await settle(lambda: reader.done)
        assert reader.frames == [{"type": "closed", "reason": None, "usage": None}]
        assert reader.owner.finished is True
        assert harness.registry.attach(call.id) is None
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_ends_a_call_whose_owner_does_not_return() -> None:
    harness = Harness(LiveCallLimits(attach_timeout_seconds=5, reattach_grace_seconds=0.05))
    try:
        call = await harness.start()
        first = harness.attach(call)
        first.owner.detach()
        second = harness.attach(call)
        await asyncio.sleep(0.1)
        assert call.abort_calls == 0
        second.owner.detach()
        await settle(lambda: call.abort_calls == 1)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_newer_owner_socket_replaces_the_previous_one() -> None:
    harness = Harness(LiveCallLimits(reattach_grace_seconds=0.05))
    try:
        call = await harness.start()
        first = harness.attach(call)
        second = harness.attach(call)
        await settle(lambda: first.done)
        assert first.owner.close_code == LIVE_SOCKET_CLOSE_REPLACED
        first.owner.detach()
        call.host.publish({"type": "state", "phase": "live"})
        await settle(lambda: len(second.frames) == 1)
        assert first.frames == []
        await asyncio.sleep(0.1)
        assert call.abort_calls == 0
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_lagging_owner_is_disconnected_and_its_updates_wait_for_the_next() -> None:
    harness = Harness(LiveCallLimits(update_buffer_limit=2, owner_queue_limit=2))
    try:
        call = await harness.start()
        owner = harness.registry.attach(call.id)
        assert owner is not None
        for index in range(3):
            call.host.publish({"type": "activity", "busy": True, "label": str(index)})
        assert owner.close_code == LIVE_SOCKET_CLOSE_LAGGED
        stale = OwnerReader(owner)
        harness.readers.append(stale)
        await settle(lambda: stale.done)
        assert stale.frames == []
        # The buffer keeps the newest updates within its bound.
        fresh = harness.attach(call)
        await settle(lambda: len(fresh.frames) == 2)
        assert [frame["label"] for frame in fresh.frames] == ["1", "2"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_unknown_calls_cannot_be_attached_stopped_or_answered(live: Harness) -> None:
    assert live.registry.attach("missing") is None
    assert live.registry.stop("missing") is False
    assert live.registry.resolve_ui_request("missing", "ui-1", result={}) is False


# -- relayed audio --------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_audio_reaches_only_an_attached_owner(live: Harness) -> None:
    call = await live.start_relay()
    assert live.service.starts == [("relay", None)]
    call.host.publish_audio(b"\x01\x00")
    call.host.publish({"type": "state", "phase": "live"})
    reader = live.attach(call)
    call.host.publish_audio(b"\x02\x00")
    call.host.publish_audio(b"")
    call.host.publish({"type": "playback_clear"})
    await settle(lambda: len(reader.frames) == 3)
    assert reader.frames == [
        {"type": "state", "phase": "live"},
        b"\x02\x00",
        {"type": "playback_clear"},
    ]


@pytest.mark.asyncio
async def test_relay_audio_beyond_its_bound_is_dropped_and_keeps_the_owner() -> None:
    harness = Harness(LiveCallLimits(owner_audio_limit_bytes=4))
    try:
        call = await harness.start_relay()
        owner = harness.registry.attach(call.id)
        assert owner is not None
        for index in range(3):
            call.host.publish_audio(bytes([index, 0]))
        call.host.publish({"type": "state", "phase": "live"})
        assert owner.close_code is None
        reader = OwnerReader(owner)
        harness.readers.append(reader)
        await settle(lambda: len(reader.frames) == 3)
        assert reader.frames == [b"\x00\x00", b"\x01\x00", {"type": "state", "phase": "live"}]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_microphone_audio_from_the_owner_reaches_the_call(live: Harness) -> None:
    call = await live.start_relay()
    first = live.registry.attach(call.id)
    assert first is not None
    first.receive_audio(b"\x01\x00\x02\x00")
    for malformed in (b"", b"\x01", bytes(LIVE_AUDIO_FRAME_MAX_BYTES + 2)):
        first.receive_audio(malformed)
    first.receive_audio(bytes(LIVE_AUDIO_FRAME_MAX_BYTES))
    second = live.registry.attach(call.id)
    assert second is not None
    first.receive_audio(b"\x03\x00")
    second.receive_audio(b"\x04\x00")
    assert call.audio == [b"\x01\x00\x02\x00", bytes(LIVE_AUDIO_FRAME_MAX_BYTES), b"\x04\x00"]


# -- stopping and ending ------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_closes_gracefully_and_forwards_the_final_update(live: Harness) -> None:
    call = await live.start()
    reader = live.attach(call)
    assert live.registry.stop(call.id) is True
    assert live.registry.active_call_id is None
    # Checked at every Event Loop step: the step that ends the call also releases
    # it, so no owner socket can attach to an ended call and miss its final update.
    for _ in range(100):
        if live.registry.stop(call.id) is False:
            break
        await asyncio.sleep(0)
    assert live.registry.attach(call.id) is None
    await settle(lambda: reader.done)
    assert reader.frames == [
        {"type": "closed", "reason": "user_stopped", "usage": {"total_tokens": 3}}
    ]
    assert reader.owner.close_code == LIVE_SOCKET_CLOSE_ENDED
    assert call.close_calls == 1
    assert call.abort_calls == 0


@pytest.mark.asyncio
async def test_stop_aborts_when_the_graceful_close_fails(
    live: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    call = await live.start()
    call.close_mode = "fail"
    with caplog.at_level(logging.DEBUG, logger="vbot.server.live"):
        live.registry.stop(call.id)
        await settle(lambda: call.abort_calls == 1)
    # The call id may be the Provider's; logs name the call by its vBot-owned id.
    assert call.log_id in caplog.text
    assert call.id not in caplog.text


@pytest.mark.asyncio
async def test_a_call_ending_on_its_own_is_forgotten_after_delivery(live: Harness) -> None:
    call = await live.start()
    reader = live.attach(call)
    call.finish("provider_closed")
    call.host.publish({"type": "caption", "role": "assistant", "text": "late", "final": True})
    await settle(lambda: reader.done)
    assert reader.types() == ["closed"]
    await settle(lambda: live.registry.active_call_id is None)
    assert live.registry.attach(call.id) is None


@pytest.mark.asyncio
async def test_a_call_ending_without_a_closed_update_gets_one(live: Harness) -> None:
    call = await live.start()
    reader = live.attach(call)
    await call.abort()
    await settle(lambda: reader.done)
    assert reader.frames == [{"type": "closed", "reason": None, "usage": None}]


# -- UI requests and Tools ------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_requests_round_trip_through_the_owner(live: Harness) -> None:
    call = await live.start()
    reader = live.attach(call)
    task = asyncio.create_task(run_tool(call, "open", {"view": "terminals"}))
    await settle(lambda: len(reader.frames) == 1)
    request = reader.frames[0]
    assert request["type"] == "ui_request"
    assert request["action"] == "open"
    assert request["args"] == {"view": "terminals"}
    request_id = request["request_id"]
    assert live.registry.resolve_ui_request(call.id, request_id, result={"applied": True})
    assert await task == {
        "ok": True,
        "error": None,
        "data": {"content": "Opened the terminals view."},
        "artifacts": [],
    }
    assert live.registry.resolve_ui_request(call.id, request_id, result={}) is False


@pytest.mark.asyncio
async def test_an_owner_error_code_fails_the_operation(live: Harness) -> None:
    call = await live.start()
    reader = live.attach(call)
    task = asyncio.create_task(run_tool(call, "open", {"view": "terminals"}))
    await settle(lambda: len(reader.frames) == 1)
    live.registry.resolve_ui_request(call.id, reader.frames[0]["request_id"], error="unknown_view")
    result = await task
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_view"
    assert result["error"]["message"] == "The app could not do this (unknown_view)."


@pytest.mark.asyncio
async def test_an_unanswered_ui_request_times_out_as_uncertain() -> None:
    harness = Harness(LiveCallLimits(ui_request_timeout_seconds=0.05))
    try:
        call = await harness.start()
        harness.attach(call)
        result = await run_tool(call, "open", {"view": "terminals"})
        assert result["error"]["code"] == "ui_timeout"
        assert "may or may not show the change" in result["error"]["message"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_ui_requests_wait_for_a_returning_owner_and_fail_without_one(
    live: Harness,
) -> None:
    call = await live.start()
    unattached = await run_tool(call, "open", {"view": "terminals"})
    assert unattached["error"]["code"] == "ui_unavailable"
    lost = live.attach(call)
    await lost.stop()
    lost.owner.detach()
    # A request made while the owner reconnects reaches it once it is back.
    task = asyncio.create_task(run_tool(call, "open", {"view": "terminals"}))
    await drain()
    reader = live.attach(call)
    await settle(lambda: len(reader.frames) == 2)
    # The failed first open was reported to the app while nobody was attached.
    assert reader.types() == ["action", "ui_request"]
    await call.abort()
    ended = await task
    assert ended["error"]["code"] == "ui_unavailable"


@pytest.mark.asyncio
async def test_tool_executions_of_one_call_never_overlap(live: Harness) -> None:
    call = await live.start()
    gate = asyncio.Event()
    live.rpc.gate = gate
    first = asyncio.create_task(run_tool(call, "overview", {}))
    second = asyncio.create_task(run_tool(call, "overview", {}))
    await drain()
    # The first overview waits on the app; the second has not begun.
    assert [method for method, _params in live.rpc.calls].count("agent.list") == 1
    gate.set()
    assert (await first)["data"]["content"].startswith("Agents: Joel.")
    assert (await second)["ok"] is True
    methods = [method for method, _params in live.rpc.calls]
    half = len(methods) // 2
    assert sorted(methods[:half]) == sorted(methods[half:])


@pytest.mark.asyncio
async def test_tools_report_voice_stopped_once_the_call_is_stopping(live: Harness) -> None:
    call = await live.start()
    call.close_mode = "hang"
    live.registry.stop(call.id)
    result = await run_tool(call, "overview", {})
    assert result["error"]["code"] == "voice_stopped"
    assert live.rpc.calls == []


@pytest.mark.asyncio
async def test_the_host_carries_the_wake_phrases_of_the_start(live: Harness) -> None:
    call = await live.start_relay(wake_phrases=("Hey Nabu", "Hey Jarvis"))
    assert call.host.wake_phrases == ("Hey Nabu", "Hey Jarvis")


@pytest.mark.asyncio
async def test_each_call_starts_with_refs_of_its_own(live: Harness) -> None:
    first = await live.start()
    await run_tool(first, "start_agent_session", {"agent": "joel", "task": "Plan the trip"})
    assert first.host.known_refs() == "- s1: Session at Joel"

    second = await live.start()
    assert second.host.known_refs() == ""


@pytest.mark.asyncio
async def test_the_owner_reports_what_the_app_shows_to_the_call(live: Harness) -> None:
    call = await live.start()
    assert (await run_tool(call, "overview", {}))["data"]["content"].startswith("Agents: Joel.")
    owner = live.registry.attach(call.id)
    assert owner is not None
    owner.receive_text(
        json.dumps(
            {
                "type": "context",
                "view": "terminals",
                "selected_agent_id": "joel",
                "selected_project_id": "",
                "chat_session": {"agent_id": "joel"},
                "extra": "ignored",
            }
        )
    )
    shown = "App: terminals view; selected Agent: Joel."
    assert (await run_tool(call, "overview", {}))["data"]["content"].startswith(shown)
    # Other frames, oversized or malformed frames, and a replaced owner change nothing.
    newer = live.registry.attach(call.id)
    assert newer is not None
    for text in (
        json.dumps({"type": "state", "view": "chat"}),
        "not json",
        json.dumps({"type": "context", "view": "chat", "pad": "x" * 5000}),
    ):
        newer.receive_text(text)
    owner.receive_text(json.dumps({"type": "context", "view": "chat"}))
    assert (await run_tool(call, "overview", {}))["data"]["content"].startswith(shown)


@pytest.mark.asyncio
async def test_end_call_closes_the_call_gracefully_after_the_goodbye(live: Harness) -> None:
    call = await live.start()
    reader = live.attach(call)
    result = await run_tool(call, "end_call", {})
    assert result["ok"] is True
    assert call.close_calls == 0
    await settle(lambda: live.registry.active_call_id is None)
    assert call.close_calls == 1
    assert call.abort_calls == 0
    # The owner learns that the voice model hung up, not that the user stopped.
    await settle(lambda: reader.done)
    assert reader.frames[-1] == {
        "type": "closed",
        "reason": "hung_up",
        "usage": {"total_tokens": 3},
    }


@pytest.mark.asyncio
async def test_each_executed_tool_call_is_shown_to_the_owner_with_what_it_named(
    live: Harness,
) -> None:
    call = await live.start()
    reader = live.attach(call)
    await run_tool(call, "start_agent_session", {"agent": "joel", "task": "Plan " + "x" * 300})
    # A call that names no Live Tool runs nothing and shows nothing.
    await run_tool(call, "browse", {})
    await settle(lambda: reader.types() == ["action"])

    action = reader.frames[0]
    assert action["tool"] == "start_agent_session"
    assert action["ok"] is True
    assert action["arguments"]["agent"] == "joel"
    assert action["arguments"]["task"].endswith("x...")
    assert len(action["arguments"]["task"]) == 200
    assert action["result"].startswith("Started a Session at Joel with the task: s1.")
    assert action["links"] == [
        {
            "ref": "s1",
            "kind": "session",
            "agent_id": "joel",
            "session_id": "ses_new",
            "label": "Session at Joel",
        }
    ]


@pytest.mark.asyncio
async def test_a_live_call_nobody_uses_is_warned_then_ended() -> None:
    limits = replace(FAST, idle_seconds=0.3, idle_warning_seconds=0.2)
    harness = Harness(limits)
    try:
        call = await harness.start()
        reader = harness.attach(call)
        call.host.publish({"type": "state", "phase": "live"})
        await settle(lambda: "idle" in reader.types())
        warning = reader.frames[-1]
        assert warning["type"] == "idle"
        assert 0 < warning["ends_in"] <= 0.2
        # The owner keeps the call; the warning is withdrawn.
        reader.owner.receive_text(json.dumps({"type": "stay"}))
        await settle(lambda: reader.frames[-1] == {"type": "idle", "ends_in": None})
        assert call.close_calls == 0
        await settle(lambda: reader.done)
        assert reader.types()[-3:] == ["idle", "idle", "closed"]
        assert reader.frames[-1]["reason"] == "idle"
        assert call.close_calls == 1
    finally:
        await harness.close()


# -- Run announcements ----------------------------------------------------------


def finish_run(
    bus: ServerEventBus,
    run_id: str,
    *,
    event_type: str = "run_completed",
    at: datetime | None = None,
    **fields: Any,
) -> None:
    bus.publish(
        event_type,
        {
            "run_id": run_id,
            "agent_id": "joel",
            "project_id": "vbot",
            "session_id": "s1",
            "run_event_timestamp": (at or STARTED_AT + timedelta(seconds=1)).isoformat(),
            **fields,
        },
    )


@pytest.mark.asyncio
async def test_hands_runs_finishing_during_the_call_to_it_with_their_outcome(
    live: Harness,
) -> None:
    call = await live.start()
    finish_run(live.bus, "r1")
    finish_run(live.bus, "r2", event_type="run_failed", project_id=None)
    finish_run(live.bus, "r3", event_type="run_interrupted")
    await settle(lambda: len(call.notices) == 3)
    assert call.notices[0] == LiveRunNotice(
        kind="completed",
        run_id="r1",
        agent_id="joel@vbot",
        session_id="s1",
        excerpt="Done.",
        truncated=True,
        session_ref="s1",
    )
    assert [(notice.kind, notice.agent_id) for notice in call.notices[1:]] == [
        ("failed", "joel"),
        ("interrupted", "joel@vbot"),
    ]
    # The excerpt is the exact Run's result, never the Session's newest reply.
    assert live.rpc.calls[0] == (
        "chat.run_result",
        {"agent_id": "joel@vbot", "session_id": "s1", "run_id": "r1"},
    )


@pytest.mark.asyncio
async def test_announces_each_run_of_the_call_once_and_skips_the_others(live: Harness) -> None:
    # Published before the call started: not replayed, whatever its timestamp says.
    finish_run(live.bus, "before-start")
    call = await live.start()
    finish_run(live.bus, "cancelled", event_type="run_cancelled")
    finish_run(live.bus, "background", contributes_to_agent_activity=False)
    finish_run(live.bus, "earlier", at=STARTED_AT - timedelta(seconds=1))
    finish_run(live.bus, "no-agent", agent_id="")
    finish_run(live.bus, "no-session", session_id=None)
    live.bus.publish("run_started", {"run_id": "started", "agent_id": "joel"})
    finish_run(live.bus, "r1")
    finish_run(live.bus, "r1", event_type="run_failed")
    finish_run(live.bus, "r2")
    await settle(lambda: len(call.notices) == 2)
    await drain()
    assert [notice.run_id for notice in call.notices] == ["r1", "r2"]
    assert [params["run_id"] for _method, params in live.rpc.calls] == ["r1", "r2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "logged"),
    [
        pytest.param(
            RpcError("domain_error", "Run result not found"), "code=domain_error", id="unavailable"
        ),
        pytest.param(RuntimeError("fixture failure"), "failed unexpectedly", id="unexpected"),
    ],
)
async def test_reports_an_announcement_that_could_not_be_loaded_and_keeps_following(
    live: Harness, caplog: pytest.LogCaptureFixture, error: Exception, logged: str
) -> None:
    call = await live.start()
    reader = live.attach(call)
    live.rpc.error = error
    with caplog.at_level(logging.WARNING, logger="vbot.server.live"):
        finish_run(live.bus, "gone")
        await settle(lambda: len(reader.frames) == 1)
    assert reader.frames == [{"type": "error", "code": "notification_failed", "fatal": False}]
    assert logged in caplog.text
    assert call.notices == []
    live.rpc.error = None
    finish_run(live.bus, "r2")
    await settle(lambda: len(call.notices) == 1)


@pytest.mark.asyncio
async def test_a_rejected_announcement_does_not_stop_the_announcements(live: Harness) -> None:
    call = await live.start()
    reader = live.attach(call)
    call.announce_error = RuntimeError("fixture rejection")
    finish_run(live.bus, "r1")
    finish_run(live.bus, "r2")
    await settle(lambda: len(call.notices) == 1)
    await drain()
    assert call.notices[0].run_id == "r2"
    assert reader.frames == []


@pytest.mark.asyncio
async def test_announcements_resume_after_the_bus_evicts_the_lagging_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness(bus=ServerEventBus(subscriber_queue_limit=1))
    try:
        call = await harness.start()
        await drain()
        with caplog.at_level(logging.WARNING, logger="vbot.server.events"):
            for index in range(5):
                finish_run(harness.bus, f"r{index}")
        assert "Evicted lagging server event subscriber" in caplog.text
        await settle(lambda: len(call.notices) == 5)
        assert [notice.run_id for notice in call.notices] == [f"r{index}" for index in range(5)]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_stops_announcing_once_the_call_ended(live: Harness) -> None:
    call = await live.start()
    await settle(lambda: live.bus.subscriber_count == 1)
    live.registry.stop(call.id)
    await settle(lambda: live.bus.subscriber_count == 0)
    finish_run(live.bus, "late")
    await drain()
    assert call.notices == []


# -- shutdown -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_closes_calls_and_refuses_new_ones() -> None:
    harness = Harness()
    call = await harness.start()
    reader = harness.attach(call)
    await harness.registry.aclose()
    await settle(lambda: reader.done)
    assert call.close_calls == 1
    assert call.abort_calls == 0
    assert reader.types() == ["closed"]
    assert reader.owner.close_code == LIVE_SOCKET_CLOSE_ENDED
    assert harness.bus.subscriber_count == 0
    with pytest.raises(LiveRegistryClosedError):
        await harness.start()
    assert harness.registry.attach(call.id) is None
    await harness.close()


@pytest.mark.asyncio
async def test_shutdown_aborts_a_call_that_does_not_close_in_time() -> None:
    harness = Harness()
    call = await harness.start()
    call.close_mode = "hang"
    reader = harness.attach(call)
    await harness.registry.aclose()
    assert call.abort_calls == 1
    await settle(lambda: reader.done)
    assert reader.frames == [{"type": "closed", "reason": None, "usage": None}]
    await harness.close()


@pytest.mark.asyncio
async def test_shutdown_during_a_start_ends_the_new_call() -> None:
    harness = Harness()
    harness.service.gate = asyncio.Event()
    start = asyncio.create_task(harness.start())
    await drain()
    await harness.registry.aclose()
    harness.service.gate.set()
    with pytest.raises(LiveRegistryClosedError):
        await start
    assert harness.service.calls[0].abort_calls == 1
    assert harness.bus.subscriber_count == 0
    await harness.close()


# -- socket route and RPC over the app ----------------------------------------------


def live_app(tmp_path: Path) -> tuple[Any, FakeService]:
    runtime = StubRuntime(tmp_path, StubAdapter())
    service = FakeService()
    cast(Any, runtime).live_voice = service
    return create_app(runtime=cast(Any, runtime)), service


def portal(client: TestClient) -> Any:
    """The blocking portal running the app's Event Loop."""
    assert client.portal is not None
    return client.portal


def rpc(client: TestClient, method: str, params: JsonObject) -> JsonObject:
    response = client.post("/api/rpc", json={"method": method, "params": params})
    assert response.status_code == 200
    body: JsonObject = response.json()
    return body


def test_socket_delivers_call_updates_and_closes_after_stop(tmp_path: Path) -> None:
    app, service = live_app(tmp_path)
    with TestClient(app) as client:
        started = rpc(client, "live.start", {"media": "webrtc", "sdp": "v=0 offer"})["result"]
        assert started == {"call_id": "call-1", "media": {"type": "webrtc", "sdp": "answer-call-1"}}
        portal(client).call(service.calls[0].host.publish, {"type": "state", "phase": "live"})
        with client.websocket_connect("/ws/live/call-1") as websocket:
            assert websocket.receive_json() == {"type": "state", "phase": "live"}
            websocket.send_json({"type": "ignored"})
            assert rpc(client, "live.stop", {"call_id": "call-1"})["result"] == {"stopping": True}
            assert websocket.receive_json()["type"] == "closed"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_json()
        assert exc_info.value.code == LIVE_SOCKET_CLOSE_ENDED


def test_socket_relays_audio_both_ways_as_binary_frames(tmp_path: Path) -> None:
    app, service = live_app(tmp_path)
    with TestClient(app) as client:
        started = rpc(client, "live.start", {"media": "relay"})["result"]
        assert started["media"]["type"] == "relay"
        call = service.calls[0]
        with client.websocket_connect("/ws/live/call-1") as websocket:
            websocket.send_bytes(b"\x01\x00\x02\x00")
            websocket.send_bytes(b"\x01")
            websocket.send_json({"type": "ignored"})
            portal(client).call(call.host.publish_audio, b"\x05\x00")
            assert websocket.receive_bytes() == b"\x05\x00"
            portal(client).call(call.host.publish, {"type": "playback_clear"})
            assert websocket.receive_json() == {"type": "playback_clear"}
            assert call.audio == [b"\x01\x00\x02\x00"]


def test_socket_carries_ui_requests_answered_through_rpc(tmp_path: Path) -> None:
    app, service = live_app(tmp_path)
    with TestClient(app) as client:
        rpc(client, "live.start", {"media": "webrtc", "sdp": "v=0 offer"})
        call = service.calls[0]
        with client.websocket_connect("/ws/live/call-1") as websocket:
            operation = portal(client).start_task_soon(
                run_tool, call, "open", {"view": "terminals"}
            )
            request = websocket.receive_json()
            assert request == {
                "type": "ui_request",
                "request_id": request["request_id"],
                "action": "open",
                "args": {"view": "terminals"},
            }
            answer = {"call_id": "call-1", "request_id": request["request_id"]}
            accepted = rpc(client, "live.ui_result", {**answer, "result": {"applied": True}})
            assert accepted["result"] == {"accepted": True}
            assert operation.result(timeout=5)["data"] == {"content": "Opened the terminals view."}
            repeated = rpc(client, "live.ui_result", {**answer, "error": "late"})
            assert repeated["result"] == {"accepted": False}


def test_socket_rejects_an_unknown_call(tmp_path: Path) -> None:
    app, _service = live_app(tmp_path)
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/live/missing") as websocket,
        pytest.raises(WebSocketDisconnect) as exc_info,
    ):
        websocket.receive_json()
    assert exc_info.value.code == 1008


def test_server_shutdown_ends_the_active_call(tmp_path: Path) -> None:
    app, service = live_app(tmp_path)
    with TestClient(app) as client:
        rpc(client, "live.start", {"media": "webrtc", "sdp": "v=0 offer"})
    assert service.calls[0].close_calls == 1
    assert app.state.live_calls.active_call_id is None


def test_socket_text_frames_reach_the_owner_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    receive_text = LiveOwnerStream.receive_text

    def record_text(owner: LiveOwnerStream, text: str) -> None:
        received.append(text)
        receive_text(owner, text)

    monkeypatch.setattr(LiveOwnerStream, "receive_text", record_text)
    app, _service = live_app(tmp_path)
    with TestClient(app) as client:
        rpc(client, "live.start", {"media": "webrtc", "sdp": "v=0 offer"})
        with client.websocket_connect("/ws/live/call-1") as websocket:
            websocket.send_json({"type": "context", "view": "terminals"})
            for _ in range(500):
                if received:
                    break
                portal(client).call(asyncio.sleep, 0.01)
    assert [json.loads(text) for text in received] == [{"type": "context", "view": "terminals"}]
