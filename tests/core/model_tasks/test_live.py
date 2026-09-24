"""Live voice service and call lifecycle, offline with a fake wire and fake brain."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import core.model_tasks.live as live_module
from core.model_tasks._live_brain import DelegationInput
from core.model_tasks._live_call import LiveCallSession
from core.model_tasks._live_openai import ControlJoinError
from core.model_tasks._live_wire import (
    WireCaption,
    WireClosed,
    WireDelegation,
    WireStarted,
    WireUsage,
)
from core.model_tasks.live import LiveRunNotice, LiveStartRejected, LiveVoiceService
from core.model_tasks.model_tasks import TaskModelError
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
)
from core.utils.errors import ConfigError

OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


class FakeWire:
    call_id = "rtc_1"
    answer_sdp = "v=0\r\nanswer"

    def __init__(self, *, confirm_close: bool = True) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[tuple[Any, ...]] = []
        self.confirm_close = confirm_close
        self.closed = False

    def push(self, *events: Any) -> None:
        for event in events:
            self.queue.put_nowait(event)

    async def events(self):  # type: ignore[no-untyped-def]
        while True:
            event = await self.queue.get()
            if event is None:
                yield WireClosed(reason=None, usage=None, confirmed=False)
                return
            yield event
            if isinstance(event, WireClosed):
                return

    async def deliver_result(self, delegation_id: str, text: str) -> None:
        self.sent.append(("result", delegation_id, text))

    async def announce(self, text: str) -> None:
        self.sent.append(("announce", text))

    async def request_close(self) -> None:
        self.sent.append(("close",))
        if self.confirm_close:
            self.push(WireClosed("client_request", {"audio_duration_ms": 900}, confirmed=True))

    async def aclose(self) -> None:
        self.closed = True
        self.push(None)


class FakeBrain:
    def __init__(self) -> None:
        self.inputs: list[DelegationInput] = []
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()

    async def answer(self, delegation: DelegationInput) -> str:
        self.inputs.append(delegation)
        self.started.set()
        await self.release.wait()
        return f"answer {len(self.inputs)}"


class FakeHost:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    def publish(self, update: dict[str, Any]) -> None:
        self.updates.append(update)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [update for update in self.updates if update["type"] == kind]


def _call(wire: FakeWire, brain: FakeBrain, host: FakeHost, **options: Any) -> LiveCallSession:
    call = LiveCallSession(wire=wire, brain=brain, host=host, target="openai/gpt-live-1", **options)  # type: ignore[arg-type]
    call.start()
    return call


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _notice(run_id: str = "run-1") -> LiveRunNotice:
    return LiveRunNotice(
        kind="completed",
        run_id=run_id,
        agent_id="coder@web",
        session_id="s-1",
        excerpt="All tests pass.",
        truncated=False,
    )


@pytest.mark.asyncio
async def test_call_goes_live_relays_captions_and_answers_delegations():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)

    wire.push(
        WireStarted(expires_at=None),
        WireCaption("user", "Start a terminal", final=True),
        WireCaption("assistant", "On it", final=False),
        WireDelegation("item_1", "Start one Codex terminal"),
        WireUsage({"audio_duration_ms": 400}),
    )
    await _until(lambda: ("result", "item_1", "answer 1") in wire.sent)
    await call.close()

    assert call.media == {"type": "webrtc", "sdp": "v=0\r\nanswer"}
    assert [u["phase"] for u in host.of_type("state")] == [
        "connecting",
        "live",
        "closing",
        "closed",
    ]
    assert host.of_type("caption") == [
        {"type": "caption", "role": "user", "text": "Start a terminal", "final": True},
        {"type": "caption", "role": "assistant", "text": "On it", "final": False},
    ]
    assert brain.inputs == [
        DelegationInput(
            request="Start one Codex terminal",
            conversation="User: Start a terminal\nAssistant (still speaking): On it",
            updates="",
        )
    ]
    assert host.of_type("activity") == [
        {"type": "activity", "busy": True, "label": "working"},
        {"type": "activity", "busy": False, "label": None},
    ]
    assert wire.sent[-1] == ("close",)
    assert host.of_type("closed") == [
        {"type": "closed", "reason": "client_request", "usage": {"audio_duration_ms": 900}}
    ]


@pytest.mark.asyncio
async def test_conversation_continues_while_delegations_run_concurrently():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    brain.release.clear()
    call = _call(wire, brain, host)

    wire.push(WireStarted(None), WireDelegation("item_1", "Read the chat"))
    await _until(lambda: len(brain.inputs) == 1)
    wire.push(
        WireCaption("user", "Also, what time is it?", final=True),
        WireDelegation("item_2", "Start Codex"),
    )
    await _until(lambda: len(brain.inputs) == 2)

    assert host.of_type("caption")[-1]["text"] == "Also, what time is it?"
    assert host.of_type("activity") == [{"type": "activity", "busy": True, "label": "working"}]
    brain.release.set()
    await _until(lambda: len([s for s in wire.sent if s[0] == "result"]) == 2)
    assert host.of_type("activity")[-1] == {"type": "activity", "busy": False, "label": None}
    await call.close()


@pytest.mark.asyncio
async def test_delegation_without_request_text_waits_for_user_speech_to_settle():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    call = _call(wire, brain, host, user_quiet=0.5, user_quiet_max_wait=0.5)

    wire.push(
        WireStarted(None),
        WireCaption("user", "Start two", final=False),
        WireDelegation("del_1", None),
    )
    await asyncio.sleep(0.05)
    wire.push(WireCaption("user", "Start two terminals", final=True))
    await _until(lambda: len(brain.inputs) == 1)

    assert brain.inputs[0].request is None
    assert brain.inputs[0].conversation == "User: Start two terminals"
    await call.close()


@pytest.mark.asyncio
async def test_delegation_with_request_text_starts_immediately():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    call = _call(wire, brain, host, user_quiet=30, user_quiet_max_wait=30)

    wire.push(
        WireStarted(None), WireCaption("user", "Stop", final=False), WireDelegation("i", "Stop it")
    )
    await asyncio.wait_for(brain.started.wait(), 1)
    await call.close()


@pytest.mark.asyncio
async def test_run_notices_are_spoken_once_while_live_and_reach_later_delegations():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)

    call.announce_run(_notice("early"))
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))
    call.announce_run(_notice("run-1"))
    call.announce_run(_notice("run-1"))
    await _until(lambda: any(s[0] == "announce" for s in wire.sent))
    wire.push(WireDelegation("i", "What did coder say?"))
    await _until(lambda: len(brain.inputs) == 1)
    await call.close()

    announcements = [s[1] for s in wire.sent if s[0] == "announce"]
    assert announcements == [
        'vBot update: {"run": "completed", "agent": "coder@web", "session_id": "s-1", '
        '"result_excerpt": "All tests pass.", "excerpt_truncated": false}'
    ]
    assert brain.inputs[0].updates.count("vBot update") == 2


@pytest.mark.asyncio
async def test_long_run_excerpts_are_shortened_for_speech():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))

    call.announce_run(LiveRunNotice("failed", "r", "a", "s", excerpt="x" * 5000, truncated=False))
    await _until(lambda: any(s[0] == "announce" for s in wire.sent))
    await call.close()

    text = next(s[1] for s in wire.sent if s[0] == "announce")
    assert '"excerpt_truncated": true' in text
    assert len(text) < 800


@pytest.mark.asyncio
async def test_abort_asks_provider_to_close_and_reports_aborted_once():
    wire, brain, host = FakeWire(confirm_close=False), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)
    wire.push(WireStarted(None))

    await call.abort()
    await call.abort()

    assert ("close",) in wire.sent
    assert wire.closed
    assert host.of_type("closed") == [{"type": "closed", "reason": "aborted", "usage": None}]


@pytest.mark.asyncio
async def test_lost_control_channel_fails_the_call_and_drops_pending_work():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    brain.release.clear()
    call = _call(wire, brain, host)
    wire.push(WireStarted(None), WireUsage({"audio_duration_ms": 10}), WireDelegation("i", "x"))
    await _until(lambda: len(brain.inputs) == 1)

    wire.push(None)
    await call.wait_closed()

    assert host.of_type("state")[-1] == {"type": "state", "phase": "failed"}
    assert host.of_type("closed") == [
        {"type": "closed", "reason": "connection_lost", "usage": {"audio_duration_ms": 10}}
    ]
    assert not any(s[0] == "result" for s in wire.sent)


@pytest.mark.asyncio
async def test_media_that_never_connects_ends_the_call():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    call = _call(wire, brain, host, start_timeout=0.05)

    await asyncio.wait_for(call.wait_closed(), 2)

    assert ("close",) in wire.sent
    assert host.of_type("closed") == [
        {"type": "closed", "reason": "start_timeout", "usage": {"audio_duration_ms": 900}}
    ]
    assert host.of_type("state")[-1] == {"type": "state", "phase": "failed"}


@pytest.mark.asyncio
async def test_unconfirmed_close_tears_down_after_timeout():
    wire, brain, host = FakeWire(confirm_close=False), FakeBrain(), FakeHost()
    call = _call(wire, brain, host, close_timeout=0.05)
    wire.push(WireStarted(None))

    await call.close()

    assert wire.closed
    assert host.of_type("closed") == [{"type": "closed", "reason": "closed", "usage": None}]


@pytest.mark.asyncio
async def test_slow_delegation_returns_a_timeout_note():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    brain.release.clear()
    call = _call(wire, brain, host, delegation_timeout=0.05)

    wire.push(WireStarted(None), WireDelegation("i", "Do something slow"))
    await _until(lambda: any(s[0] == "result" for s in wire.sent))
    await call.close()

    result = next(s for s in wire.sent if s[0] == "result")
    assert result[2].startswith("The request took too long and was stopped.")


class FakeModelTasks:
    def __init__(
        self,
        target: str = "openai/gpt-live-1-codex::subscription",
        options: dict[str, Any] | None = None,
        configured: bool = True,
    ) -> None:
        self.target = target
        self.options = options or {"voice": "cove", "backend_model": "gpt-5.6-terra"}
        self.configured = configured

    def binding_for(self, task_type: str) -> SimpleNamespace:
        if not self.configured:
            raise TaskModelError("live_voice is not configured")
        return SimpleNamespace(target=self.target)

    def binding_is_usable(self, task_type: str) -> bool:
        return True

    def validate_execution_target(self, binding: Any) -> None:
        return None

    def options_with_defaults(self, binding: Any) -> dict[str, Any]:
        return dict(self.options)


@pytest.fixture
def candidates(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake_candidates(models: Any, provider_id: str, connection_id: str) -> tuple[Any, ...]:
        calls.append((provider_id, connection_id))
        return (SimpleNamespace(model_id="gpt-5.6-terra"), SimpleNamespace(model_id="gpt-5.6-luna"))

    monkeypatch.setattr(live_module, "live_backend_candidates", fake_candidates)
    return calls


def _service(model_tasks: FakeModelTasks | None = None) -> LiveVoiceService:
    return LiveVoiceService(model_tasks or FakeModelTasks(), SimpleNamespace(models=object()))  # type: ignore[arg-type]


def test_status_reports_binding_without_starting_anything():
    assert _service(FakeModelTasks(configured=False)).status() == {
        "configured": False,
        "usable": False,
        "target": None,
    }
    assert _service().status() == {
        "configured": True,
        "usable": True,
        "target": "openai/gpt-live-1-codex::subscription",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offer",
    ["", "not-sdp", "v=0\r\nm=video", "v=0 m=audio" + "x" * 65536],
    ids=["empty", "not-sdp", "no-audio", "oversized"],
)
async def test_invalid_offers_are_rejected_before_resolution(offer: str):
    with pytest.raises(LiveStartRejected) as caught:
        await _service(FakeModelTasks(configured=False)).start_call(
            offer_sdp=offer, host=FakeHost()
        )
    assert caught.value.code == "invalid_offer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_tasks",
    [
        FakeModelTasks(configured=False),
        FakeModelTasks(target="xai/grok-voice::api-key"),
        FakeModelTasks(options={"voice": "cove", "backend_model": "gpt-image-2"}),
        FakeModelTasks(
            options={
                "voice": "cove",
                "backend_model": "gpt-5.6-terra",
                "backend_thinking_effort": "turbo",
            }
        ),
        FakeModelTasks(
            options={
                "voice": "cove",
                "backend_model": "gpt-5.6-terra",
                "backend_thinking_effort": None,
            }
        ),
    ],
    ids=[
        "unbound",
        "unsupported-provider",
        "backend-not-candidate",
        "unknown-effort",
        "null-effort",
    ],
)
async def test_unusable_configuration_is_not_configured(model_tasks, candidates):
    with pytest.raises(LiveStartRejected) as caught:
        await _service(model_tasks).start_call(offer_sdp=OFFER, host=FakeHost())
    assert caught.value.code == "not_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderAuthError("denied"), "access_denied"),
        (ProviderRateLimitError("slow"), "rate_limited"),
        (ProviderOutcomeUnknownError("unknown", operation_key="k"), "outcome_unknown"),
        (NetworkError("down"), "provider_error"),
        (ConfigError("no credential"), "not_configured"),
        (ControlJoinError("rtc_1", "OSError"), "control_failed"),
    ],
)
async def test_creation_failures_map_to_stable_codes(error, code, candidates, monkeypatch):
    async def failing_wire(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(live_module, "open_openai_live_wire", failing_wire)
    with pytest.raises(LiveStartRejected) as caught:
        await _service().start_call(offer_sdp=OFFER, host=FakeHost())
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_start_call_opens_the_bound_target_and_returns_a_running_call(
    candidates: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[dict[str, Any]] = []
    wire = FakeWire()

    async def open_wire(runtime: Any, target_ref: Any, **kwargs: Any) -> FakeWire:
        opened.append({"target": target_ref.target, **kwargs})
        return wire

    monkeypatch.setattr(live_module, "open_openai_live_wire", open_wire)
    host = FakeHost()

    call = await _service().start_call(offer_sdp=OFFER, host=host)

    assert candidates == [("openai", "subscription")]
    assert opened[0]["target"] == "openai/gpt-live-1-codex::subscription"
    assert opened[0]["voice"] == "cove"
    assert opened[0]["offer_sdp"] == OFFER
    assert call.id == "rtc_1"
    assert host.updates == [{"type": "state", "phase": "connecting"}]
    await call.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "sent"),
    [
        ({}, "low"),
        ({"backend_thinking_effort": ""}, None),
        ({"backend_thinking_effort": "high"}, "high"),
    ],
    ids=["saved-before-the-option", "model-default", "configured"],
)
async def test_the_backend_uses_the_configured_reasoning_effort(
    stored: dict[str, Any],
    sent: str | None,
    candidates: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brains: list[Any] = []

    async def open_wire(runtime: Any, target_ref: Any, **kwargs: Any) -> FakeWire:
        return FakeWire()

    def record_brain(runtime: Any, target: Any, execute_tool: Any, **kwargs: Any) -> FakeBrain:
        brains.append(target)
        return FakeBrain()

    monkeypatch.setattr(live_module, "open_openai_live_wire", open_wire)
    monkeypatch.setattr(live_module, "LiveBrain", record_brain)
    model_tasks = FakeModelTasks(
        options={"voice": "cove", "backend_model": "gpt-5.6-luna", **stored}
    )

    call = await _service(model_tasks).start_call(offer_sdp=OFFER, host=FakeHost())

    assert [(target.model_id, target.thinking_effort) for target in brains] == [
        ("gpt-5.6-luna", sent)
    ]
    await call.close()
