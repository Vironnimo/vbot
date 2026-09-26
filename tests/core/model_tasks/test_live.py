"""Live voice service and call lifecycle, offline with a fake wire and fake brain."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import core.model_tasks._live_call as live_call_module
import core.model_tasks.live as live_module
from core.model_tasks._live_brain import DelegationInput
from core.model_tasks._live_call import LiveCallSession
from core.model_tasks._live_openai import ControlJoinError
from core.model_tasks._live_tools import (
    DIRECT_VOICE_INSTRUCTIONS,
    VOICE_INSTRUCTIONS,
    live_success,
    voice_instructions,
)
from core.model_tasks._live_wire import (
    WireAudio,
    WireCaption,
    WireClosed,
    WireDelegation,
    WirePlaybackClear,
    WireSendError,
    WireStarted,
    WireToolCall,
    WireUsage,
    relay_media,
)
from core.model_tasks.live import LiveRunNotice, LiveStartRejected, LiveVoiceService
from core.model_tasks.model_tasks import TaskModelError, parse_task_model_target_id
from core.model_tasks.task_execution import TaskUsage
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
)
from core.usage import UsageRecorder
from core.utils.errors import ConfigError

OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
XAI_TARGET = "xai/grok-voice-think-fast-2.0::subscription"


class FakeWire:
    call_id = "rtc_1"

    def __init__(
        self,
        *,
        confirm_close: bool = True,
        relay: bool = False,
        announces_as_user_input: bool = False,
    ) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[tuple[Any, ...]] = []
        self.audio: list[bytes] = []
        self.audio_error: Exception | None = None
        self.confirm_close = confirm_close
        self.closed = False
        self.media = relay_media() if relay else {"type": "webrtc", "sdp": "v=0\r\nanswer"}
        self.announces_as_user_input = announces_as_user_input

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

    async def send_audio(self, pcm: bytes) -> None:
        if self.audio_error is not None:
            raise self.audio_error
        self.audio.append(pcm)

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
        self.audio: list[bytes] = []
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.records: list[dict[str, Any]] = []
        self.tool_result: Any = {"ok": True}
        self.tool_release = asyncio.Event()
        self.tool_release.set()
        self.refs = ""

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.executed.append((name, arguments))
        await self.tool_release.wait()
        if isinstance(self.tool_result, Exception):
            raise self.tool_result
        return dict(self.tool_result)

    def known_refs(self) -> str:
        return self.refs

    def publish(self, update: dict[str, Any]) -> None:
        self.updates.append(update)

    def publish_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    def record(self, event: dict[str, Any]) -> None:
        self.records.append(event)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [update for update in self.updates if update["type"] == kind]


def _call(
    wire: FakeWire, brain: FakeBrain | None, host: FakeHost, **options: Any
) -> LiveCallSession:
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
        session_ref="s3",
    )


@pytest.mark.asyncio
async def test_call_goes_live_relays_captions_and_answers_delegations():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    host.refs = "- s1: Session at Coder"
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
            refs="- s1: Session at Coder",
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
async def test_live_cumulative_usage_is_saved_once_and_survives_lost_control(
    recorder: UsageRecorder,
) -> None:
    wire, host = FakeWire(), FakeHost()
    accounting = TaskUsage(recorder, "live_voice", parse_task_model_target_id(XAI_TARGET))
    call_id = await accounting.start()
    call = _call(wire, None, host, usage_accounting=accounting, usage_call_id=call_id)
    wire.push(
        WireStarted(None),
        WireUsage({"input_tokens": 4, "output_tokens": 2}),
        WireUsage({"input_tokens": 7, "output_tokens": 5}),
        WireClosed(reason=None, usage=None, confirmed=False),
    )
    await call.wait_closed()
    _, records = recorder.read_since()
    assert len(records) == 1
    assert (records[0].kind, records[0].status) == ("live_voice", "failed")
    assert records[0].usage["input_tokens"] == 7
    assert records[0].usage["output_tokens"] == 5
    assert len(host.of_type("closed")) == 1


@pytest.mark.asyncio
async def test_live_usage_failure_still_publishes_closed(caplog: Any) -> None:
    from unittest.mock import AsyncMock

    wire, host = FakeWire(), FakeHost()
    accounting = SimpleNamespace(finish=AsyncMock(side_effect=RuntimeError("test disk failure")))
    call = _call(wire, None, host, usage_accounting=accounting, usage_call_id="test-call")
    wire.push(WireClosed(reason="client_request", usage=None, confirmed=True))
    await call.wait_closed()
    assert host.of_type("state")[-1]["phase"] == "closed"
    assert len(host.of_type("closed")) == 1
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["failure", "timeout"])
async def test_transport_cleanup_cannot_prevent_call_completion(
    cleanup: str, monkeypatch: pytest.MonkeyPatch
):
    wire, host = FakeWire(), FakeHost()

    async def close_transport() -> None:
        if cleanup == "failure":
            raise RuntimeError("test socket cleanup failure")
        await asyncio.Event().wait()

    monkeypatch.setattr(wire, "aclose", close_transport)
    monkeypatch.setattr(live_call_module, "_TEARDOWN_TIMEOUT_SECONDS", 0.01)
    call = _call(wire, None, host)
    wire.push(WireClosed(reason="client_request", usage=None, confirmed=True))

    await asyncio.wait_for(call.wait_closed(), 1)

    assert host.of_type("closed") == [{"type": "closed", "reason": "client_request", "usage": None}]


@pytest.mark.asyncio
async def test_abort_during_transport_cleanup_keeps_abort_reason_priority(
    monkeypatch: pytest.MonkeyPatch,
):
    wire, host = FakeWire(), FakeHost()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def close_transport() -> None:
        cleanup_started.set()
        await cleanup_release.wait()
        wire.closed = True

    monkeypatch.setattr(wire, "aclose", close_transport)
    call = _call(wire, None, host)
    wire.push(WireClosed(reason="client_request", usage=None, confirmed=True))
    await asyncio.wait_for(cleanup_started.wait(), 1)
    abort = asyncio.create_task(call.abort())
    await _until(lambda: ("close",) in wire.sent)
    cleanup_release.set()

    await asyncio.wait_for(abort, 1)

    assert wire.closed
    assert host.of_type("closed") == [{"type": "closed", "reason": "aborted", "usage": None}]


@pytest.mark.asyncio
async def test_cancellation_during_transport_cleanup_preserves_terminal_usage(
    recorder: UsageRecorder, monkeypatch: pytest.MonkeyPatch
):
    wire, host = FakeWire(), FakeHost()
    cleanup_started = asyncio.Event()

    async def close_transport() -> None:
        cleanup_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(wire, "aclose", close_transport)
    accounting = TaskUsage(recorder, "live_voice", parse_task_model_target_id(XAI_TARGET))
    call_id = await accounting.start()
    call = _call(wire, None, host, usage_accounting=accounting, usage_call_id=call_id)
    wire.push(
        WireClosed(
            reason="client_request", usage={"input_tokens": 7, "output_tokens": 5}, confirmed=True
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), 1)
    assert call._reader is not None
    call._reader.cancel()

    await asyncio.wait_for(call.wait_closed(), 1)

    _, records = recorder.read_since()
    assert len(records) == 1
    assert records[0].status == "completed"
    assert records[0].usage["input_tokens"] == 7
    assert records[0].usage["output_tokens"] == 5
    assert len(host.of_type("closed")) == 1


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
        'vBot update: {"run": "completed", "agent": "coder@web", "session": "s3", '
        '"result_excerpt": "All tests pass.", "excerpt_truncated": false}'
    ]
    assert brain.inputs[0].updates.count("vBot update") == 2


@pytest.mark.asyncio
async def test_run_notices_omit_the_excerpt_where_announcements_count_as_user_input():
    wire, brain, host = FakeWire(announces_as_user_input=True), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))

    call.announce_run(_notice("run-1"))
    await _until(lambda: any(s[0] == "announce" for s in wire.sent))
    wire.push(WireDelegation("i", "What did coder say?"))
    await _until(lambda: len(brain.inputs) == 1)
    await call.close()

    assert [s[1] for s in wire.sent if s[0] == "announce"] == [
        'vBot update: {"run": "completed", "agent": "coder@web", "session": "s3"}'
    ]
    assert '"result_excerpt": "All tests pass."' in brain.inputs[0].updates


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


def test_status_reports_binding_and_media_without_starting_anything():
    assert _service(FakeModelTasks(configured=False)).status() == {
        "configured": False,
        "usable": False,
        "target": None,
        "media": None,
    }
    assert _service().status() == {
        "configured": True,
        "usable": True,
        "target": "openai/gpt-live-1-codex::subscription",
        "media": "webrtc",
    }
    assert _service(FakeModelTasks(target=XAI_TARGET)).status()["media"] == "relay"
    assert _service(FakeModelTasks(target="mistral/voxtral::api-key")).status()["media"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offer",
    ["", "not-sdp", "v=0\r\nm=video", "v=0 m=audio" + "x" * 65536],
    ids=["empty", "not-sdp", "no-audio", "oversized"],
)
async def test_invalid_offers_are_rejected_before_resolution(offer: str):
    with pytest.raises(LiveStartRejected) as caught:
        await _service(FakeModelTasks(configured=False)).start_call(
            media="webrtc", offer_sdp=offer, host=FakeHost()
        )
    assert caught.value.code == "invalid_offer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_tasks",
    [
        FakeModelTasks(configured=False),
        FakeModelTasks(target="mistral/voxtral::api-key"),
        FakeModelTasks(options={"voice": "cove", "backend_model": ""}),
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
                "backend_thinking_effort": 3,
            }
        ),
    ],
    ids=[
        "unbound",
        "unsupported-provider",
        "no-backend-without-direct-tools",
        "backend-not-candidate",
        "unknown-effort",
        "non-string-effort",
    ],
)
async def test_unusable_configuration_is_not_configured(model_tasks, candidates):
    with pytest.raises(LiveStartRejected) as caught:
        await _service(model_tasks).start_call(media="webrtc", offer_sdp=OFFER, host=FakeHost())
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
        await _service().start_call(media="webrtc", offer_sdp=OFFER, host=FakeHost())
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

    call = await _service().start_call(media="webrtc", offer_sdp=OFFER, host=host)

    assert candidates == [("openai", "subscription")]
    assert opened[0]["target"] == "openai/gpt-live-1-codex::subscription"
    assert opened[0]["voice"] == "cove"
    assert opened[0]["offer_sdp"] == OFFER
    assert opened[0]["instructions"] == VOICE_INSTRUCTIONS
    assert call.id == "rtc_1"
    assert host.updates == [{"type": "state", "phase": "connecting"}]
    await call.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "sent"),
    [
        ({}, "low"),
        ({"backend_thinking_effort": None}, "low"),
        ({"backend_thinking_effort": ""}, None),
        ({"backend_thinking_effort": "high"}, "high"),
    ],
    ids=["saved-before-the-option", "unset", "model-default", "configured"],
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

    call = await _service(model_tasks).start_call(media="webrtc", offer_sdp=OFFER, host=FakeHost())

    assert [(target.model_id, target.thinking_effort) for target in brains] == [
        ("gpt-5.6-luna", sent)
    ]
    await call.close()


@pytest.mark.asyncio
async def test_relay_audio_flows_only_while_live():
    wire, brain, host = FakeWire(relay=True), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)

    call.push_audio(b"\x01\x00")
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))
    call.push_audio(b"\x02\x00")
    call.push_audio(b"")
    call.push_audio(b"\x03\x00")
    await _until(lambda: len(wire.audio) == 2)
    wire.push(WireAudio("item_1", b"\x10\x00"), WirePlaybackClear())
    await _until(lambda: bool(host.of_type("playback_clear")))

    assert call.media == relay_media()
    assert wire.audio == [b"\x02\x00", b"\x03\x00"]
    assert host.audio == [b"\x10\x00"]
    assert host.of_type("playback_clear") == [{"type": "playback_clear"}]
    await call.close()
    call.push_audio(b"\x04\x00")
    await asyncio.sleep(0.01)
    assert wire.audio == [b"\x02\x00", b"\x03\x00"]


@pytest.mark.asyncio
async def test_webrtc_calls_ignore_pushed_audio():
    wire, brain, host = FakeWire(), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))

    call.push_audio(b"\x01\x00")
    await asyncio.sleep(0.01)

    assert wire.audio == []
    await call.close()


@pytest.mark.asyncio
async def test_relay_audio_backlog_drops_the_oldest_audio():
    wire, brain, host = FakeWire(relay=True), FakeBrain(), FakeHost()
    call = _call(wire, brain, host)
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))

    one_second = bytes(48 * 1000)
    for index in range(4):
        call.push_audio(bytes([index]) + one_second[1:])
    await _until(lambda: len(wire.audio) == 2)

    assert [frame[0] for frame in wire.audio] == [2, 3]
    await call.close()


@pytest.mark.asyncio
async def test_relay_audio_stops_when_the_socket_is_gone():
    wire, brain, host = FakeWire(relay=True), FakeBrain(), FakeHost()
    wire.audio_error = WireSendError("closed")
    call = _call(wire, brain, host)
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))

    call.push_audio(b"\x01\x00")
    await asyncio.sleep(0.01)
    wire.audio_error = None
    call.push_audio(b"\x02\x00")
    await asyncio.sleep(0.01)

    assert wire.audio == []
    await call.close()


@pytest.mark.asyncio
async def test_direct_tool_calls_run_on_the_host_and_return_plain_text_results():
    wire, host = FakeWire(relay=True), FakeHost()
    host.tool_release.clear()
    host.tool_result = live_success("Sent to s1 (Coder).")
    call = _call(wire, None, host)

    wire.push(
        WireStarted(None),
        WireToolCall("call_1", "send_message", {"target": "s1", "text": "yes"}),
    )
    await _until(lambda: len(host.executed) == 1)
    assert host.of_type("activity") == [{"type": "activity", "busy": True, "label": "working"}]
    host.tool_release.set()
    await _until(lambda: any(s[0] == "result" for s in wire.sent))
    await call.close()

    assert host.executed == [("send_message", {"target": "s1", "text": "yes"})]
    assert next(s for s in wire.sent if s[0] == "result") == (
        "result",
        "call_1",
        "Sent to s1 (Coder).",
    )
    assert host.of_type("activity")[-1] == {"type": "activity", "busy": False, "label": None}


@pytest.mark.asyncio
async def test_direct_tool_calls_in_other_spellings_run_as_the_live_tool_they_mean():
    wire, host = FakeWire(relay=True), FakeHost()
    host.tool_result = live_success("Showing t1.")
    call = _call(wire, None, host)

    wire.push(WireStarted(None), WireToolCall("c", "functions.show", '{"terminal": "t1"}'))
    await _until(lambda: any(s[0] == "result" for s in wire.sent))
    await call.close()

    assert host.executed == [("open", {"target": "t1"})]
    # The record keeps the call as the Model wrote it and as it ran.
    [record] = host.records
    assert record == {
        "type": "tool",
        "mode": "direct",
        "called": "functions.show",
        "tool": "open",
        "arguments": '{"terminal": "t1"}',
        "run_arguments": {"target": "t1"},
        "ok": True,
        "result": "Showing t1.",
        "duration_ms": record["duration_ms"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "code"),
    [
        (WireToolCall("c", "shell", {"command": "ls"}), "unknown_tool"),
        (WireToolCall("c", "send_message", ["s1"]), "invalid_arguments"),
        (WireToolCall("c", "send_message", "not json"), "invalid_arguments"),
        (WireToolCall("c", "send_message", {"target": "s1"}), "invalid_arguments"),
    ],
    ids=["unknown-tool", "array-arguments", "undecodable-arguments", "missing-field"],
)
async def test_direct_tool_calls_that_must_not_run_are_refused(event: WireToolCall, code: str):
    wire, host = FakeWire(relay=True), FakeHost()
    call = _call(wire, None, host)

    wire.push(WireStarted(None), event)
    await _until(lambda: any(s[0] == "result" for s in wire.sent))
    await call.close()

    assert host.executed == []
    assert next(s for s in wire.sent if s[0] == "result")[2].startswith(f"Error ({code}): ")
    assert [(record["tool"], record["ok"]) for record in host.records] == [(None, False)]


@pytest.mark.asyncio
async def test_failing_or_slow_direct_tool_calls_answer_with_an_error_and_never_retry():
    wire, host = FakeWire(relay=True), FakeHost()
    host.tool_result = RuntimeError("boom")
    call = _call(wire, None, host, delegation_timeout=0.05)

    wire.push(
        WireStarted(None), WireToolCall("fail", "start_coding_terminal", {"program": "codex"})
    )
    await _until(lambda: any(s[1] == "fail" for s in wire.sent if s[0] == "result"))
    host.tool_result = live_success("Nothing runs.")
    host.tool_release.clear()
    wire.push(WireToolCall("slow", "overview", {}))
    await _until(lambda: any(s[1] == "slow" for s in wire.sent if s[0] == "result"))
    await call.close()

    results = {s[1]: s[2] for s in wire.sent if s[0] == "result"}
    assert results["fail"] == (
        "Error (tool_failed): The Tool call failed. It may have partly completed; do not repeat "
        "it. Call overview to see what happened."
    )
    assert results["slow"].startswith("Error (timeout): The Tool call took too long")
    assert [name for name, _arguments in host.executed] == ["start_coding_terminal", "overview"]


@pytest.mark.asyncio
async def test_a_delegation_without_a_backend_model_is_answered_without_running():
    wire, host = FakeWire(relay=True), FakeHost()
    call = _call(wire, None, host)

    wire.push(WireStarted(None), WireDelegation("d", "Open the terminals"))
    await _until(lambda: any(s[0] == "result" for s in wire.sent))
    await call.close()

    assert next(s for s in wire.sent if s[0] == "result")[2].startswith("No backend model")
    assert host.executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_tasks", "media"),
    [
        (FakeModelTasks(), "relay"),
        (
            FakeModelTasks(target=XAI_TARGET, options={"voice": "eve", "backend_model": ""}),
            "webrtc",
        ),
    ],
    ids=["openai-needs-webrtc", "xai-needs-relay"],
)
async def test_a_start_with_the_other_media_kind_is_a_media_mismatch(
    model_tasks: FakeModelTasks, media: str, candidates: list[tuple[Any, ...]]
) -> None:
    with pytest.raises(LiveStartRejected) as caught:
        await _service(model_tasks).start_call(media=media, offer_sdp=OFFER, host=FakeHost())
    assert caught.value.code == "media_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        {"backend_model": ""},
        {},
        {"backend_model": "", "backend_thinking_effort": "turbo"},
    ],
    ids=["no-backend", "backend-unset", "stale-effort-is-ignored"],
)
async def test_xai_without_a_backend_model_runs_in_direct_tools_mode(
    stored: dict[str, Any], candidates: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[dict[str, Any]] = []
    brains: list[Any] = []

    async def open_wire(runtime: Any, target_ref: Any, **kwargs: Any) -> FakeWire:
        opened.append({"target": target_ref.target, **kwargs})
        return FakeWire(relay=True)

    monkeypatch.setattr(live_module, "open_xai_live_wire", open_wire)
    monkeypatch.setattr(live_module, "LiveBrain", lambda *args, **kwargs: brains.append(args))
    model_tasks = FakeModelTasks(target=XAI_TARGET, options={"voice": "eve", **stored})

    call = await _service(model_tasks).start_call(media="relay", host=FakeHost())

    assert opened == [
        {
            "target": XAI_TARGET,
            "instructions": DIRECT_VOICE_INSTRUCTIONS,
            "voice": "eve",
            "direct_tools": True,
        }
    ]
    assert brains == []
    assert candidates == []
    assert call.media == relay_media()
    await call.close()


@pytest.mark.asyncio
async def test_xai_with_a_backend_model_delegates_to_it(
    candidates: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[dict[str, Any]] = []
    brains: list[Any] = []

    async def open_wire(runtime: Any, target_ref: Any, **kwargs: Any) -> FakeWire:
        opened.append(kwargs)
        return FakeWire(relay=True)

    def record_brain(runtime: Any, target: Any, execute_tool: Any, **kwargs: Any) -> FakeBrain:
        brains.append(target)
        return FakeBrain()

    monkeypatch.setattr(live_module, "open_xai_live_wire", open_wire)
    monkeypatch.setattr(live_module, "LiveBrain", record_brain)
    model_tasks = FakeModelTasks(
        target=XAI_TARGET, options={"voice": "eve", "backend_model": "gpt-5.6-terra"}
    )

    call = await _service(model_tasks).start_call(media="relay", host=FakeHost())

    assert opened[0]["direct_tools"] is False
    assert opened[0]["instructions"] == VOICE_INSTRUCTIONS
    assert candidates == [("xai", "subscription")]
    assert [(target.provider_id, target.model_id) for target in brains] == [
        ("xai", "gpt-5.6-terra")
    ]
    await call.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_tasks", "media", "opener", "direct_tools"),
    [
        (FakeModelTasks(), "webrtc", "open_openai_live_wire", False),
        (
            FakeModelTasks(target=XAI_TARGET, options={"voice": "eve", "backend_model": ""}),
            "relay",
            "open_xai_live_wire",
            True,
        ),
        (
            FakeModelTasks(
                target=XAI_TARGET, options={"voice": "eve", "backend_model": "gpt-5.6-terra"}
            ),
            "relay",
            "open_xai_live_wire",
            False,
        ),
    ],
    ids=["openai", "xai-direct-tools", "xai-delegate"],
)
async def test_wake_phrases_reach_the_voice_instructions_of_every_wire(
    model_tasks: FakeModelTasks,
    media: str,
    opener: str,
    direct_tools: bool,
    candidates: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[dict[str, Any]] = []
    phrases = ("Hey Nabu", "Hey Jarvis")

    async def open_wire(runtime: Any, target_ref: Any, **kwargs: Any) -> FakeWire:
        opened.append(kwargs)
        return FakeWire(relay=media == "relay")

    monkeypatch.setattr(live_module, opener, open_wire)
    monkeypatch.setattr(live_module, "LiveBrain", lambda *args, **kwargs: FakeBrain())

    call = await _service(model_tasks).start_call(
        media=media,
        offer_sdp=OFFER if media == "webrtc" else None,
        wake_phrases=phrases,
        host=FakeHost(),
    )

    instructions = opened[0]["instructions"]
    assert instructions == voice_instructions(direct_tools=direct_tools, wake_phrases=phrases)
    assert instructions != voice_instructions(direct_tools=direct_tools)
    await call.close()
