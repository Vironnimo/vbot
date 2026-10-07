"""Live voice service and call lifecycle, offline with a fake wire, voice Run, and backend."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import core.model_tasks._live_call as live_call_module
import core.model_tasks.live as live_module
from core.model_tasks._live_backend import BackendRequest
from core.model_tasks._live_brief import live_tool_guidance, voice_instructions
from core.model_tasks._live_call import LiveCallSession
from core.model_tasks._live_openai import ControlJoinError, OpenAIBackend
from core.model_tasks._live_wire import (
    RELAY_BYTES_PER_MS,
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
from core.model_tasks.live import (
    LiveRunNotice,
    LiveStartRejected,
    LiveVoiceService,
    live_success,
)
from core.model_tasks.model_tasks import TaskModelError, parse_task_model_target_id
from core.model_tasks.task_execution import TaskUsage
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
)
from core.tools.live import LiveToolHosts
from core.usage import UsageRecorder
from core.utils.errors import ConfigError, VBotError
from tests.core.usage.usage_test_support import read_ledger

OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
OPENAI_TARGET = "openai/gpt-live-1-codex::subscription"
XAI_TARGET = "xai/grok-voice-think-fast-2.0::subscription"
VOICE_TOOLS = (
    {"name": "overview", "description": "What vBot shows.", "parameters": {"type": "object"}},
    {"name": "end_call", "description": "Hang up.", "parameters": {"type": "object"}},
)
WAKE_PHRASES = ("hey vbot",)


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

    def results(self) -> dict[str, str]:
        return {sent[1]: sent[2] for sent in self.sent if sent[0] == "result"}


class FakeVoice:
    """The voice Agent's Run: Tool calls reach the call the way the registry routes them."""

    agent_id = "live-voice"

    def __init__(self, hosts: LiveToolHosts, *, tools: Any = VOICE_TOOLS) -> None:
        self.hosts = hosts
        self.session_id = "voice-1"
        self.tool_definitions = [dict(tool) for tool in tools]
        self.records: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str, Any]] = []
        self.finished: list[str | None] = []
        self.discarded = False
        self.on_cancel: Callable[[], None] | None = None

    async def record_user(self, text: str) -> None:
        self.records.append(("user", text))

    async def record_assistant(self, text: str) -> None:
        self.records.append(("assistant", text))

    async def record_note(self, text: str) -> None:
        self.records.append(("note", text))

    async def run_tool(self, call_id: str, name: str, arguments: Any) -> dict[str, Any]:
        self.calls.append((call_id, name, arguments))
        host = self.hosts.get(self.session_id)
        assert host is not None
        if name == "vbot_request":
            request = arguments.get("request") if isinstance(arguments, dict) else None
            return await host.vbot_request(request or None)
        return await host.run_live_tool(name, arguments)

    async def finish(self, *, failure: str | None = None) -> None:
        self.finished.append(failure)

    async def discard(self) -> None:
        self.discarded = True


class FakeBackend:
    """Answers requests one at a time, like the backend Agent's Session."""

    def __init__(self) -> None:
        self.requests: list[BackendRequest] = []
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()
        self.closed = False
        self.session_id: str | None = None
        self.on_session: Callable[[], None] | None = None
        self._turn = asyncio.Lock()

    async def answer(self, prepare: Callable[[], Awaitable[BackendRequest]]) -> str:
        async with self._turn:
            self.requests.append(await prepare())
            if self.session_id is None:
                self.session_id = "backend-1"
                assert self.on_session is not None
                self.on_session()
            self.started.set()
            await self.release.wait()
            return f"answer {len(self.requests)}"

    async def aclose(self) -> None:
        self.closed = True


class FakeHost:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.audio: list[bytes] = []
        self.executed: list[tuple[Any, Any]] = []
        self.tool_result: Any = {"ok": True}
        self.tool_release = asyncio.Event()
        self.tool_release.set()
        self.refs: str | Exception = ""
        self.state = ""

    @property
    def wake_phrases(self) -> tuple[str, ...]:
        return WAKE_PHRASES

    async def run_live_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        self.executed.append((name, arguments))
        await self.tool_release.wait()
        if isinstance(self.tool_result, Exception):
            raise self.tool_result
        return dict(self.tool_result)

    def known_refs(self) -> str:
        if isinstance(self.refs, Exception):
            raise self.refs
        return self.refs

    async def current_state(self) -> str:
        return self.state

    def publish(self, update: dict[str, Any]) -> None:
        self.updates.append(update)

    def publish_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [update for update in self.updates if update["type"] == kind]


def _call(
    wire: FakeWire, host: FakeHost, backend: FakeBackend | None = None, **options: Any
) -> tuple[LiveCallSession, FakeVoice]:
    hosts = LiveToolHosts()
    voice = FakeVoice(hosts)

    def backend_factory(session_host: Any, on_session: Callable[[], None]) -> FakeBackend:
        assert backend is not None
        backend.on_session = on_session
        return backend

    call = LiveCallSession(
        wire=wire,  # type: ignore[arg-type]
        voice=voice,  # type: ignore[arg-type]
        backend_factory=backend_factory if backend is not None else None,  # type: ignore[arg-type]
        host=host,
        hosts=hosts,
        target="openai/gpt-live-1",
        **options,
    )
    call.start()
    return call, voice


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


NOTICE_TEXT = (
    'vBot update: {"run": "completed", "agent": "coder@web", "session": "s3", '
    '"result_excerpt": "All tests pass.", "excerpt_truncated": false}'
)


@pytest.mark.asyncio
async def test_call_goes_live_relays_captions_and_answers_delegations():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    host.refs = "- s1: Session at Coder"
    host.state = "Terminals: none"
    call, voice = _call(wire, host, backend)

    wire.push(
        WireStarted(expires_at=None),
        WireCaption("user", "Start a terminal", final=True),
        WireCaption("assistant", "On it", final=False),
        WireDelegation("item_1", "Start one Codex terminal"),
        WireUsage({"audio_duration_ms": 400}),
    )
    await _until(lambda: wire.results().get("item_1") == "answer 1")
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
    assert backend.requests == [
        BackendRequest(
            request="Start one Codex terminal",
            conversation="User: Start a terminal\nAssistant (still speaking): On it",
            updates="",
            state="Terminals: none",
            refs="- s1: Session at Coder",
        )
    ]
    # The delegation is stored in the voice Session as a vbot_request call.
    assert voice.calls == [("item_1", "vbot_request", {"request": "Start one Codex terminal"})]
    assert host.of_type("activity") == [
        {"type": "activity", "busy": True, "label": "working"},
        {"type": "activity", "busy": False, "label": None},
    ]
    assert wire.sent[-1] == ("close",)
    assert host.of_type("closed") == [
        {"type": "closed", "reason": "client_request", "usage": {"audio_duration_ms": 900}}
    ]
    assert backend.closed
    assert voice.finished == [None]


@pytest.mark.asyncio
async def test_the_voice_session_records_the_call_in_order_and_releases_its_tools():
    wire, host = FakeWire(), FakeHost()
    call, voice = _call(wire, host)
    assert voice.hosts.get(voice.session_id) is call

    wire.push(
        WireStarted(None),
        WireCaption("user", "Hi", final=False),
        WireCaption("user", "Hi there", final=True),
        WireCaption("assistant", "Hello", final=True),
    )
    await _until(lambda: len(host.of_type("caption")) == 3)
    call.announce_run(_notice())
    await call.close()

    # Partial captions are not stored; what was said and the updates keep their order.
    assert voice.records == [("user", "Hi there"), ("assistant", "Hello"), ("note", NOTICE_TEXT)]
    assert voice.finished == [None]
    assert voice.hosts.get(voice.session_id) is None


@pytest.mark.asyncio
async def test_the_accessor_learns_the_sessions_once_each_exists():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    call, _voice = _call(wire, host, backend)
    wire.push(WireStarted(None), WireDelegation("d1", "One"), WireDelegation("d2", "Two"))
    await _until(lambda: len(wire.results()) == 2)
    await call.close()

    voice_session = {"agent_id": "live-voice", "session_id": "voice-1"}
    assert host.of_type("sessions") == [
        {"type": "sessions", "voice": voice_session, "backend": None},
        {
            "type": "sessions",
            "voice": voice_session,
            "backend": {"agent_id": "live-backend", "session_id": "backend-1"},
        },
    ]


@pytest.mark.asyncio
async def test_live_cumulative_usage_is_saved_once_and_survives_lost_control(
    recorder: UsageRecorder,
) -> None:
    wire, host = FakeWire(), FakeHost()
    accounting = TaskUsage(recorder, "live_voice", parse_task_model_target_id(XAI_TARGET))
    call_id = await accounting.start()
    call, voice = _call(wire, host, usage_accounting=accounting, usage_call_id=call_id)
    wire.push(
        WireStarted(None),
        WireUsage({"input_tokens": 4, "output_tokens": 2}),
        WireUsage({"input_tokens": 7, "output_tokens": 5}),
        WireClosed(reason=None, usage=None, confirmed=False),
    )
    await call.wait_closed()
    _, records = read_ledger(recorder)
    assert len(records) == 1
    assert (records[0].kind, records[0].status) == ("live_voice", "failed")
    assert records[0].usage["input_tokens"] == 7
    assert records[0].usage["output_tokens"] == 5
    assert len(host.of_type("closed")) == 1
    # A lost connection fails the voice Run with the reason.
    assert voice.finished == ["The connection to the voice model was lost."]


@pytest.mark.asyncio
async def test_live_usage_failure_still_publishes_closed(caplog: Any) -> None:
    from unittest.mock import AsyncMock

    wire, host = FakeWire(), FakeHost()
    accounting = SimpleNamespace(finish=AsyncMock(side_effect=RuntimeError("test disk failure")))
    call, voice = _call(wire, host, usage_accounting=accounting, usage_call_id="test-call")
    wire.push(WireClosed(reason="client_request", usage=None, confirmed=True))
    await call.wait_closed()
    assert host.of_type("state")[-1]["phase"] == "closed"
    assert len(host.of_type("closed")) == 1
    assert any(record.exc_info for record in caplog.records)
    assert voice.finished == [None]


@pytest.mark.asyncio
async def test_a_failed_usage_update_keeps_the_call_live(caplog: Any) -> None:
    from unittest.mock import AsyncMock

    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    accounting = SimpleNamespace(
        update=AsyncMock(side_effect=RuntimeError("test disk failure")), finish=AsyncMock()
    )
    call, _voice = _call(wire, host, backend, usage_accounting=accounting, usage_call_id="c")
    wire.push(WireStarted(None), WireUsage({"input_tokens": 4}), WireDelegation("i", "x"))
    await _until(lambda: bool(wire.results()))
    assert host.of_type("closed") == []
    # A confirmed close that names no reason is an ordinary close.
    wire.push(WireClosed(reason=None, usage=None, confirmed=True))
    await call.wait_closed()
    assert host.of_type("closed") == [
        {"type": "closed", "reason": "closed", "usage": {"input_tokens": 4}}
    ]
    assert accounting.update.await_count == 1


@pytest.mark.asyncio
async def test_a_cancelled_close_still_ends_the_call_and_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
):
    wire, host = FakeWire(confirm_close=False), FakeHost()
    cleanup_started = asyncio.Event()

    async def close_transport() -> None:
        cleanup_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(wire, "aclose", close_transport)
    monkeypatch.setattr(live_call_module, "_TEARDOWN_TIMEOUT_SECONDS", 5)
    call, _voice = _call(wire, host, close_timeout=0.01)
    wire.push(WireStarted(None))
    closing = asyncio.create_task(call.close())
    await asyncio.wait_for(cleanup_started.wait(), 1)
    monkeypatch.setattr(live_call_module, "_TEARDOWN_TIMEOUT_SECONDS", 0.01)
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 1)
    assert host.of_type("closed") == [{"type": "closed", "reason": "closed", "usage": None}]


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
    call, _voice = _call(wire, host)
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
    call, _voice = _call(wire, host)
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
    call, _voice = _call(wire, host, usage_accounting=accounting, usage_call_id=call_id)
    wire.push(
        WireClosed(
            reason="client_request", usage={"input_tokens": 7, "output_tokens": 5}, confirmed=True
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), 1)
    assert call._reader is not None
    call._reader.cancel()

    await asyncio.wait_for(call.wait_closed(), 1)

    _, records = read_ledger(recorder)
    assert len(records) == 1
    assert records[0].status == "completed"
    assert records[0].usage["input_tokens"] == 7
    assert records[0].usage["output_tokens"] == 5
    assert len(host.of_type("closed")) == 1


@pytest.mark.asyncio
async def test_requests_run_one_after_another_while_the_conversation_continues():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    backend.release.clear()
    call, _voice = _call(wire, host, backend)

    wire.push(
        WireStarted(None),
        WireCaption("user", "Read the chat", final=True),
        WireDelegation("item_1", "Read the chat"),
    )
    await _until(lambda: len(backend.requests) == 1)
    host.state = "Terminals: t1"
    wire.push(
        WireCaption("user", "Also, start Codex", final=True),
        WireDelegation("item_2", "Start Codex"),
    )
    await _until(lambda: host.of_type("caption")[-1]["text"] == "Also, start Codex")
    await asyncio.sleep(0.02)

    # The second request waits for the first and then starts from the state it left.
    assert len(backend.requests) == 1
    assert host.of_type("activity") == [{"type": "activity", "busy": True, "label": "working"}]
    backend.release.set()
    await _until(lambda: len(wire.results()) == 2)
    assert [request.request for request in backend.requests] == ["Read the chat", "Start Codex"]
    # Each request carries only what was said since the previous one.
    assert [request.conversation for request in backend.requests] == [
        "User: Read the chat",
        "User: Also, start Codex",
    ]
    assert backend.requests[1].state == "Terminals: t1"
    assert host.of_type("activity")[-1] == {"type": "activity", "busy": False, "label": None}
    await call.close()


@pytest.mark.asyncio
async def test_a_request_without_text_waits_for_user_speech_to_settle():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    call, _voice = _call(wire, host, backend, user_settle_timeout=30)

    wire.push(
        WireStarted(None),
        WireCaption("user", "Start two", final=False),
        WireDelegation("del_1", None),
    )
    await asyncio.sleep(0.05)
    wire.push(WireCaption("user", "Start two terminals", final=True))
    await _until(lambda: len(backend.requests) == 1)

    assert backend.requests[0].request is None
    assert backend.requests[0].conversation == "User: Start two terminals"
    await call.close()


@pytest.mark.asyncio
async def test_a_request_with_text_starts_immediately():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    call, _voice = _call(wire, host, backend, user_settle_timeout=30)

    wire.push(
        WireStarted(None), WireCaption("user", "Stop", final=False), WireDelegation("i", "Stop it")
    )
    await asyncio.wait_for(backend.started.wait(), 1)
    await call.close()


@pytest.mark.asyncio
async def test_run_notices_are_spoken_once_live_and_reach_later_requests():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    call, _voice = _call(wire, host, backend)

    call.announce_run(_notice("early"))
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))
    call.announce_run(_notice("run-1"))
    call.announce_run(_notice("run-1"))
    await _until(lambda: sum(s[0] == "announce" for s in wire.sent) == 2)
    wire.push(WireDelegation("i", "What did coder say?"))
    await _until(lambda: len(backend.requests) == 1)
    await call.close()

    # The notice from before the call was live is spoken once it is.
    assert [s[1] for s in wire.sent if s[0] == "announce"] == [NOTICE_TEXT] * 2
    assert backend.requests[0].updates == f"{NOTICE_TEXT}\n{NOTICE_TEXT}"


@pytest.mark.asyncio
async def test_run_notices_omit_the_excerpt_where_announcements_count_as_user_input():
    wire, host, backend = FakeWire(announces_as_user_input=True), FakeHost(), FakeBackend()
    call, _voice = _call(wire, host, backend)
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))

    call.announce_run(_notice("run-1"))
    await _until(lambda: any(s[0] == "announce" for s in wire.sent))
    wire.push(WireDelegation("i", "What did coder say?"))
    await _until(lambda: len(backend.requests) == 1)
    await call.close()

    assert [s[1] for s in wire.sent if s[0] == "announce"] == [
        'vBot update: {"run": "completed", "agent": "coder@web", "session": "s3"}'
    ]
    assert '"result_excerpt": "All tests pass."' in backend.requests[0].updates


@pytest.mark.asyncio
async def test_long_run_excerpts_are_shortened_for_speech():
    wire, host = FakeWire(), FakeHost()
    call, _voice = _call(wire, host)
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
    wire, host, backend = FakeWire(confirm_close=False), FakeHost(), FakeBackend()
    call, voice = _call(wire, host, backend)
    wire.push(WireStarted(None))

    await call.abort()
    await call.abort()

    assert ("close",) in wire.sent
    assert wire.closed
    assert host.of_type("closed") == [{"type": "closed", "reason": "aborted", "usage": None}]
    assert backend.closed
    assert voice.finished == [None]


@pytest.mark.asyncio
async def test_abort_soon_ends_the_call_for_a_caller_that_cannot_wait():
    wire, host = FakeWire(confirm_close=False), FakeHost()
    call, _voice = _call(wire, host)
    wire.push(WireStarted(None))

    call.abort_soon()
    await asyncio.wait_for(call.wait_closed(), 2)

    assert host.of_type("closed") == [{"type": "closed", "reason": "aborted", "usage": None}]


@pytest.mark.asyncio
async def test_lost_control_channel_fails_the_call_and_drops_pending_work():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    backend.release.clear()
    call, voice = _call(wire, host, backend)
    wire.push(WireStarted(None), WireUsage({"audio_duration_ms": 10}), WireDelegation("i", "x"))
    await _until(lambda: len(backend.requests) == 1)

    wire.push(None)
    await call.wait_closed()

    assert host.of_type("state")[-1] == {"type": "state", "phase": "failed"}
    assert host.of_type("closed") == [
        {"type": "closed", "reason": "connection_lost", "usage": {"audio_duration_ms": 10}}
    ]
    assert not wire.results()
    assert backend.closed
    assert voice.finished == ["The connection to the voice model was lost."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("elapsed", "closed", "reason"),
    [
        (3590.0, WireClosed(reason="session_ended", usage=None, confirmed=True), "expired"),
        (3590.0, WireClosed(reason=None, usage=None, confirmed=False), "expired"),
        (60.0, WireClosed(reason="max_duration", usage=None, confirmed=True), "expired"),
        (60.0, WireClosed(reason=None, usage=None, confirmed=False), "connection_lost"),
    ],
    ids=["closed-at-limit", "lost-at-limit", "limit-reason", "lost-early"],
)
async def test_a_provider_close_at_the_session_limit_is_reported_as_expired(
    elapsed: float, closed: WireClosed, reason: str
) -> None:
    now = [0.0]
    wire, host = FakeWire(), FakeHost()
    call, _voice = _call(wire, host, clock=lambda: now[0], wall_clock=lambda: 1000.0)
    wire.push(WireStarted(expires_at=4600.0))
    await _until(lambda: host.of_type("expiry"))
    # The accessor learns how long the provider keeps the session.
    assert host.of_type("expiry") == [{"type": "expiry", "seconds": 3600.0}]

    now[0] = elapsed
    wire.push(closed)
    await call.wait_closed()
    assert [update["reason"] for update in host.of_type("closed")] == [reason]


@pytest.mark.asyncio
async def test_media_that_never_connects_ends_the_call():
    wire, host = FakeWire(), FakeHost()
    call, voice = _call(wire, host, start_timeout=0.05)

    await asyncio.wait_for(call.wait_closed(), 2)

    assert ("close",) in wire.sent
    assert host.of_type("closed") == [
        {"type": "closed", "reason": "start_timeout", "usage": {"audio_duration_ms": 900}}
    ]
    assert host.of_type("state")[-1] == {"type": "state", "phase": "failed"}
    assert voice.finished == ["The voice model's audio did not connect in time."]


@pytest.mark.asyncio
async def test_unconfirmed_close_tears_down_after_timeout():
    wire, host = FakeWire(confirm_close=False), FakeHost()
    call, _voice = _call(wire, host, close_timeout=0.05)
    wire.push(WireStarted(None))

    await call.close()

    assert wire.closed
    assert host.of_type("closed") == [{"type": "closed", "reason": "closed", "usage": None}]


@pytest.mark.asyncio
async def test_a_request_that_fails_inside_vbot_is_still_answered():
    wire, host, backend = FakeWire(), FakeHost(), FakeBackend()
    host.refs = RuntimeError("test failure")
    call, _voice = _call(wire, host, backend)
    wire.push(WireStarted(None), WireDelegation("i", "Open the terminals"))
    await _until(lambda: bool(wire.results()))
    await call.close()
    assert backend.requests == []
    assert wire.results()["i"].startswith("vBot could not process this request")


@pytest.mark.asyncio
async def test_a_slow_request_returns_a_timeout_note(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live_call_module, "_REQUEST_EXTRA_SECONDS", 0)
    wire, host, backend = FakeWire(relay=True), FakeHost(), FakeBackend()
    backend.release.clear()
    call, _voice = _call(wire, host, backend, delegation_timeout=0.05)

    wire.push(
        WireStarted(None),
        WireDelegation("d", "Do something slow"),
        WireToolCall("c", "vbot_request", {"request": "Do something else"}),
    )
    await _until(lambda: len(wire.results()) == 2)
    await call.close()

    results = wire.results()
    assert results["d"].startswith("The request took too long and was stopped.")
    assert results["c"].startswith("Error (timeout): The request took too long")


@pytest.mark.asyncio
async def test_a_request_without_a_backend_is_answered_without_running():
    wire, host = FakeWire(relay=True), FakeHost()
    call, _voice = _call(wire, host)

    wire.push(
        WireStarted(None),
        WireDelegation("d", "Open the terminals"),
        WireToolCall("c", "vbot_request", {"request": "Open the terminals"}),
    )
    await _until(lambda: len(wire.results()) == 2)
    await call.close()

    results = wire.results()
    assert results["d"] == "Nothing answers requests in this call, so nothing was done."
    assert results["c"].startswith("Error (no_backend): Nothing answers requests")
    assert host.executed == []


@pytest.mark.asyncio
async def test_relay_audio_flows_only_while_live():
    wire, host = FakeWire(relay=True), FakeHost()
    call, _voice = _call(wire, host)

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
async def test_speech_finishes_when_the_turn_is_final_and_its_relayed_audio_played():
    now = [100.0]
    wire, host = FakeWire(relay=True), FakeHost()
    call, _voice = _call(wire, host, clock=lambda: now[0])
    wire.push(WireStarted(None), WireCaption("assistant", "Bye", final=False))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))
    finished = asyncio.create_task(call.speech_finished())

    # 100 ms of audio is still playing when the turn ends.
    wire.push(WireAudio("item_1", b"\x00" * RELAY_BYTES_PER_MS * 100))
    await _until(lambda: bool(host.audio))
    assert not finished.done()
    wire.push(WireCaption("assistant", "Bye!", final=True))
    await asyncio.sleep(0.02)
    assert not finished.done()
    await asyncio.wait_for(finished, 1)
    # Without speech, nothing holds the end.
    now[0] += 1.0
    await asyncio.wait_for(call.speech_finished(), 0.05)
    await call.close()


@pytest.mark.asyncio
async def test_webrtc_calls_ignore_pushed_audio():
    wire, host = FakeWire(), FakeHost()
    call, _voice = _call(wire, host)
    wire.push(WireStarted(None))
    await _until(lambda: any(u.get("phase") == "live" for u in host.updates))

    call.push_audio(b"\x01\x00")
    await asyncio.sleep(0.01)

    assert wire.audio == []
    await call.close()


@pytest.mark.asyncio
async def test_relay_audio_backlog_drops_the_oldest_audio():
    wire, host = FakeWire(relay=True), FakeHost()
    call, _voice = _call(wire, host)
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
    wire, host = FakeWire(relay=True), FakeHost()
    wire.audio_error = WireSendError("closed")
    call, _voice = _call(wire, host)
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
async def test_tool_calls_run_in_the_voice_session_and_return_plain_text_results():
    wire, host = FakeWire(relay=True), FakeHost()
    host.tool_release.clear()
    host.tool_result = live_success("Sent to s1 (Coder).")
    call, voice = _call(wire, host)

    wire.push(
        WireStarted(None),
        WireToolCall("call_1", "send_message", '{"target": "s1", "text": "yes"}'),
    )
    await _until(lambda: len(host.executed) == 1)
    assert host.of_type("activity") == [{"type": "activity", "busy": True, "label": "working"}]
    host.tool_release.set()
    await _until(lambda: bool(wire.results()))
    await call.close()

    # The voice Session runs the call; the host gets the name and arguments unchanged.
    assert voice.calls == [("call_1", "send_message", '{"target": "s1", "text": "yes"}')]
    assert host.executed == [("send_message", '{"target": "s1", "text": "yes"}')]
    assert wire.results() == {"call_1": "Sent to s1 (Coder)."}
    assert host.of_type("activity")[-1] == {"type": "activity", "busy": False, "label": None}


@pytest.mark.asyncio
async def test_failing_or_slow_tool_calls_answer_with_an_error_and_never_retry():
    wire, host = FakeWire(relay=True), FakeHost()
    host.tool_result = RuntimeError("boom")
    call, _voice = _call(wire, host, delegation_timeout=0.05)

    wire.push(
        WireStarted(None), WireToolCall("fail", "start_coding_terminal", {"program": "codex"})
    )
    await _until(lambda: "fail" in wire.results())
    host.tool_result = live_success("Nothing runs.")
    host.tool_release.clear()
    wire.push(WireToolCall("slow", "overview", {}))
    await _until(lambda: "slow" in wire.results())
    await call.close()

    results = wire.results()
    assert results["fail"].startswith("Error (tool_failed):")
    assert results["slow"].startswith("Error (timeout): The Tool call took too long")
    assert [name for name, _arguments in host.executed] == ["start_coding_terminal", "overview"]


class FakeChat:
    """The Chat Loop seams a call start uses: the voice Run and the backend's Tools."""

    def __init__(self, hosts: LiveToolHosts) -> None:
        self.hosts = hosts
        self.started: list[dict[str, Any]] = []
        self.voices: list[FakeVoice] = []
        self.error: Exception | None = None

    async def start_external_run(
        self,
        agent_id: str,
        *,
        model: str,
        title: str,
        extra_tools: list[str],
        on_cancel: Callable[[], None],
    ) -> FakeVoice:
        if self.error is not None:
            raise self.error
        self.started.append(
            {"agent_id": agent_id, "model": model, "title": title, "extra_tools": extra_tools}
        )
        voice = FakeVoice(self.hosts, tools=[*VOICE_TOOLS, *({"name": n} for n in extra_tools)])
        voice.on_cancel = on_cancel
        self.voices.append(voice)
        return voice


class FakeModelTasks:
    def __init__(
        self,
        target: str = OPENAI_TARGET,
        options: dict[str, Any] | None = None,
        configured: bool = True,
    ) -> None:
        self.target = target
        self.options = options if options is not None else {"voice": "cove"}
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

    def model_for_target(self, target_ref: Any) -> None:
        return None


def _service(model_tasks: FakeModelTasks | None = None) -> tuple[LiveVoiceService, FakeChat]:
    hosts = LiveToolHosts()
    chat = FakeChat(hosts)
    runtime = SimpleNamespace(models=object(), chat_loop=chat, chat_sessions=object())
    service = LiveVoiceService(
        model_tasks or FakeModelTasks(),
        runtime,  # type: ignore[arg-type]
        hosts=hosts,
        clock=lambda: datetime(2026, 10, 7, 12, 30),
    )
    return service, chat


def _opening(monkeypatch: pytest.MonkeyPatch, name: str, wire: FakeWire) -> list[dict[str, Any]]:
    opened: list[dict[str, Any]] = []

    async def open_wire(runtime: Any, target_ref: Any, **kwargs: Any) -> FakeWire:
        opened.append({"target": target_ref.target, **kwargs})
        return wire

    monkeypatch.setattr(live_module, name, open_wire)
    return opened


def test_status_reports_binding_and_media_without_starting_anything():
    assert _service(FakeModelTasks(configured=False))[0].status() == {
        "configured": False,
        "usable": False,
        "target": None,
        "media": None,
    }
    assert _service()[0].status() == {
        "configured": True,
        "usable": True,
        "target": OPENAI_TARGET,
        "media": "webrtc",
    }
    assert _service(FakeModelTasks(target=XAI_TARGET))[0].status()["media"] == "relay"
    assert _service(FakeModelTasks(target="mistral/voxtral::api-key"))[0].status()["media"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offer",
    ["", "not-sdp", "v=0\r\nm=video", "v=0 m=audio" + "x" * 65536],
    ids=["empty", "not-sdp", "no-audio", "oversized"],
)
async def test_invalid_offers_are_rejected_before_resolution(offer: str):
    service, chat = _service(FakeModelTasks(configured=False))
    with pytest.raises(LiveStartRejected) as caught:
        await service.start_call(media="webrtc", offer_sdp=offer, host=FakeHost())
    assert caught.value.code == "invalid_offer"
    assert chat.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_tasks", "offered", "code"),
    [
        (FakeModelTasks(configured=False), (), "not_configured"),
        (FakeModelTasks(target="mistral/voxtral::api-key"), (), "not_configured"),
        (FakeModelTasks(options={"backend": "openai"}), (), "backend_unavailable"),
        (FakeModelTasks(options={"backend": "none"}), (), "backend_unavailable"),
        (
            FakeModelTasks(target=XAI_TARGET, options={"backend": "openai"}),
            (),
            "backend_unavailable",
        ),
        (
            FakeModelTasks(options={"backend": "openai", "openai_backend_model": "gpt-5.6-terra"}),
            ("vbot",),
            "backend_unavailable",
        ),
    ],
    ids=[
        "unbound",
        "unsupported-provider",
        "openai-backend-without-model",
        "openai-without-backend",
        "xai-with-openai-backend",
        "backend-the-model-does-not-offer",
    ],
)
async def test_an_unusable_configuration_names_what_is_missing(
    model_tasks: FakeModelTasks,
    offered: tuple[str, ...],
    code: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(live_module, "live_backend_choices", lambda model: offered)
    service, chat = _service(model_tasks)
    media = "relay" if model_tasks.target == XAI_TARGET else "webrtc"
    with pytest.raises(LiveStartRejected) as caught:
        await service.start_call(media=media, offer_sdp=OFFER, host=FakeHost())
    assert caught.value.code == code
    assert chat.started == []


@pytest.mark.asyncio
async def test_a_voice_session_that_cannot_start_rejects_the_call(
    monkeypatch: pytest.MonkeyPatch,
):
    opened = _opening(monkeypatch, "open_openai_live_wire", FakeWire())
    service, chat = _service()
    chat.error = VBotError("the voice Agent has no Model")

    with pytest.raises(LiveStartRejected) as caught:
        await service.start_call(media="webrtc", offer_sdp=OFFER, host=FakeHost())

    assert caught.value.code == "not_configured"
    assert opened == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderAuthError("denied"), "access_denied"),
        (ProviderRateLimitError("slow"), "rate_limited"),
        (ProviderOutcomeUnknownError("unknown", operation_key="k"), "outcome_unknown"),
        (NetworkError("down"), "provider_error"),
        (ConfigError("no credential"), "provider_unavailable"),
        (ControlJoinError("rtc_1", "OSError"), "control_failed"),
    ],
)
async def test_creation_failures_map_to_stable_codes_and_drop_the_voice_session(
    error: Exception, code: str, monkeypatch: pytest.MonkeyPatch, caplog: Any
):
    async def failing_wire(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(live_module, "open_openai_live_wire", failing_wire)
    caplog.set_level(logging.DEBUG)
    service, chat = _service()
    with pytest.raises(LiveStartRejected) as caught:
        await service.start_call(media="webrtc", offer_sdp=OFFER, host=FakeHost())
    assert caught.value.code == code
    # A call that never started leaves no Session behind.
    assert [voice.discarded for voice in chat.voices] == [True]
    # A call the Provider created but vBot could not join keeps its id out of the log.
    assert "rtc_1" not in caplog.text


@pytest.mark.asyncio
async def test_start_call_opens_the_bound_target_and_returns_a_running_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    opened = _opening(monkeypatch, "open_openai_live_wire", FakeWire())
    host = FakeHost()
    caplog.set_level(logging.DEBUG)
    service, chat = _service()

    call = await service.start_call(media="webrtc", offer_sdp=OFFER, host=host)

    assert chat.started == [
        {
            "agent_id": "live-voice",
            "model": OPENAI_TARGET,
            "title": "Live call · 2026-10-07 12:30",
            "extra_tools": ["vbot_request"],
        }
    ]
    assert opened[0]["target"] == OPENAI_TARGET
    assert opened[0]["voice"] == "cove"
    assert opened[0]["offer_sdp"] == OFFER
    assert call.id == "rtc_1"
    assert host.updates == [
        {"type": "state", "phase": "connecting"},
        {
            "type": "sessions",
            "voice": {"agent_id": "live-voice", "session_id": "voice-1"},
            "backend": None,
        },
    ]
    # Stopping the voice Run in the app ends the call.
    voice = chat.voices[0]
    assert voice.on_cancel is not None
    voice.on_cancel()
    await asyncio.wait_for(call.wait_closed(), 2)
    assert host.of_type("closed")[-1]["reason"] == "aborted"
    assert voice.finished == [None]
    # Logs name the call by its vBot-owned id; the Provider's call id never enters them.
    assert call.log_id.startswith("live_")
    assert call.log_id in caplog.text
    assert "rtc_1" not in caplog.text


_VOICE_NAMES = ["overview", "end_call"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "options", "extra_tools", "expected"),
    [
        (
            OPENAI_TARGET,
            {"backend": "vbot"},
            ["vbot_request"],
            {
                "backend": None,
                "instructions": voice_instructions(
                    tools=[], delegates=True, wake_phrases=WAKE_PHRASES
                ),
            },
        ),
        (
            OPENAI_TARGET,
            {"backend": "openai", "openai_backend_model": "gpt-5.6-terra"},
            [],
            {
                "backend": OpenAIBackend(
                    model="gpt-5.6-terra",
                    instructions=live_tool_guidance(set(_VOICE_NAMES).__contains__),
                    tools=VOICE_TOOLS,
                ),
                "instructions": voice_instructions(
                    tools=[], delegates=True, wake_phrases=WAKE_PHRASES
                ),
            },
        ),
        (
            XAI_TARGET,
            {},
            [],
            {
                "tools": list(VOICE_TOOLS),
                "instructions": voice_instructions(tools=_VOICE_NAMES, wake_phrases=WAKE_PHRASES),
            },
        ),
        (
            XAI_TARGET,
            {"backend": "vbot"},
            ["vbot_request"],
            {
                "tools": [*VOICE_TOOLS, {"name": "vbot_request"}],
                "instructions": voice_instructions(
                    tools=[*_VOICE_NAMES, "vbot_request"], wake_phrases=WAKE_PHRASES
                ),
            },
        ),
    ],
    ids=["openai-vbot", "openai-hosted", "xai-own-tools", "xai-vbot"],
)
async def test_the_backend_choice_decides_what_the_voice_model_gets(
    target: str,
    options: dict[str, Any],
    extra_tools: list[str],
    expected: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xai = target == XAI_TARGET
    wire = FakeWire(relay=xai)
    opened = _opening(monkeypatch, "open_xai_live_wire" if xai else "open_openai_live_wire", wire)
    service, chat = _service(FakeModelTasks(target=target, options=options))

    call = await service.start_call(
        media="relay" if xai else "webrtc", offer_sdp=None if xai else OFFER, host=FakeHost()
    )

    assert [started["extra_tools"] for started in chat.started] == [extra_tools]
    assert {key: opened[0][key] for key in expected} == expected
    await call.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_tasks", "media"),
    [
        (FakeModelTasks(), "relay"),
        (FakeModelTasks(target=XAI_TARGET, options={"voice": "eve"}), "webrtc"),
    ],
    ids=["openai-needs-webrtc", "xai-needs-relay"],
)
async def test_a_start_with_the_other_media_kind_is_a_media_mismatch(
    model_tasks: FakeModelTasks, media: str
) -> None:
    service, _chat = _service(model_tasks)
    with pytest.raises(LiveStartRejected) as caught:
        await service.start_call(media=media, offer_sdp=OFFER, host=FakeHost())
    assert caught.value.code == "media_mismatch"
