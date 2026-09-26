"""Offline verification of the two GPT-Live wires against the verified wire facts."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from websockets.exceptions import ConnectionClosedError

from core.model_tasks._live_call import LiveCallSession
from core.model_tasks._live_openai import (
    CODEX_LIVE_HEADERS,
    ControlJoinError,
    OpenAILiveWire,
    chunk_text,
    open_openai_live_wire,
)
from core.model_tasks._live_wire import (
    WireCaption,
    WireClosed,
    WireDelegation,
    WireProblem,
    WireSendError,
    WireStarted,
    WireUsage,
)
from core.model_tasks.model_tasks import parse_task_model_target_id
from core.providers.errors import ProviderAuthError, ProviderOutcomeUnknownError
from core.providers.openai_subscription_auth import OPENAI_AUTH_CLAIM
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import StaticTokenGetter

CODEX_CALLS_URL = (
    "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
)
PUBLIC_SESSIONS_URL = "https://api.openai.com/v1/live/sessions"
OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
ANSWER = "v=0\r\no=- answer\r\n"


def _subscription_token(account_id: str = "account-123") -> str:
    def encode(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    claims = {OPENAI_AUTH_CLAIM: {"chatgpt_account_id": account_id}}
    return f"{encode({'alg': 'none'})}.{encode(claims)}."


def _runtime(token: str) -> SimpleNamespace:
    provider = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization", prefix="Bearer ", credential_key="OPENAI_API_KEY"
                ),
            ),
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key=""),
                base_url="https://chatgpt.com/backend-api",
                mode="codex_responses",
            ),
        ],
    )
    return SimpleNamespace(
        providers=SimpleNamespace(get=lambda provider_id: provider),
        get_connection_token_getter=lambda connection: StaticTokenGetter(token),
    )


class FakeSocket:
    """A control WebSocket fed from a queue of frames; ``None`` ends it abruptly."""

    def __init__(self, frames: list[Any] | None = None) -> None:
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        for frame in frames or []:
            self.frames.put_nowait(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> Any:
        frame = await self.frames.get()
        if frame is None or self.closed:
            raise ConnectionClosedError(None, None)
        return frame if isinstance(frame, str) else json.dumps(frame)

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedError(None, None)
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True
        self.frames.put_nowait(None)


class FakeConnect:
    def __init__(self, socket: FakeSocket | None = None, error: Exception | None = None) -> None:
        self.socket = socket or FakeSocket()
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **options: Any) -> FakeSocket:
        self.calls.append((url, options))
        if self.error is not None:
            raise self.error
        return self.socket


async def _collect(wire: OpenAILiveWire) -> list[Any]:
    return [event async for event in wire.events()]


@pytest.mark.asyncio
@respx.mock
async def test_codex_call_uses_verified_route_and_joins_control_with_same_headers():
    route = respx.post(CODEX_CALLS_URL).respond(
        201,
        text=ANSWER,
        headers={"content-type": "text/plain", "location": "/v1/realtime/calls/rtc_abc-123"},
    )
    connect = FakeConnect()
    token = _subscription_token()

    wire = await open_openai_live_wire(
        _runtime(token),
        parse_task_model_target_id("openai/gpt-live-1-codex::subscription"),
        offer_sdp=OFFER,
        instructions="be brief",
        voice="cove",
        connect=connect,
    )

    request = route.calls[0].request
    assert json.loads(request.content) == {
        "sdp": OFFER,
        "session": {
            "model": "gpt-live-1-codex",
            "instructions": "be brief",
            "delegation": {"type": "client"},
            "audio": {"output": {"voice": "cove"}},
        },
    }
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["chatgpt-account-id"] == "account-123"
    for name, value in CODEX_LIVE_HEADERS.items():
        assert request.headers[name] == value
    assert {"session-id", "thread-id", "x-session-id"} <= set(request.headers)
    assert wire.call_id == "rtc_abc-123"
    assert wire.media == {"type": "webrtc", "sdp": ANSWER}
    assert wire.announces_as_user_input is False
    url, options = connect.calls[0]
    assert url == "wss://api.openai.com/v1/live/rtc_abc-123"
    control_headers = {key.lower(): value for key, value in options["additional_headers"].items()}
    assert control_headers["authorization"] == f"Bearer {token}"
    assert control_headers["chatgpt-account-id"] == "account-123"
    assert control_headers["session-id"] == request.headers["session-id"]
    assert options["ssl"] is not None


@pytest.mark.asyncio
@respx.mock
async def test_public_session_locks_the_page_data_channel_and_attaches_sideband():
    route = respx.post(PUBLIC_SESSIONS_URL).respond(
        201,
        json={
            "session": {"id": "live_123", "private": "hidden"},
            "transport": {"type": "webrtc", "sdp": ANSWER},
        },
    )
    connect = FakeConnect()

    wire = await open_openai_live_wire(
        _runtime("sk-test"),
        parse_task_model_target_id("openai/gpt-live-1::api-key"),
        offer_sdp=OFFER,
        instructions="be brief",
        voice=None,
        connect=connect,
    )

    body = json.loads(route.calls[0].request.content)
    assert body == {
        "session": {
            "model": "gpt-live-1",
            "instructions": "be brief",
            "delegation": {"type": "client"},
            "client": {"data_channel": {"allowed_client_events": [], "allowed_server_events": []}},
        },
        "transport": {"type": "webrtc", "sdp": OFFER},
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"
    assert wire.call_id == "live_123"
    url, options = connect.calls[0]
    assert url == "wss://api.openai.com/v1/live/sessions/live_123/attach"
    assert options["additional_headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            httpx.Response(201, text="not sdp", headers={"location": "/calls/rtc_1"}),
            ProviderOutcomeUnknownError,
        ),
        (httpx.Response(201, text=ANSWER), ProviderOutcomeUnknownError),
        (httpx.Response(401, json={"error": {"message": "denied"}}), ProviderAuthError),
    ],
    ids=["no-sdp", "no-call-id", "denied"],
)
@respx.mock
async def test_codex_creation_failures_are_never_replayed(response, error):
    route = respx.post(CODEX_CALLS_URL).mock(return_value=response)
    connect = FakeConnect()

    with pytest.raises(error):
        await open_openai_live_wire(
            _runtime(_subscription_token()),
            parse_task_model_target_id("openai/gpt-live-1-codex::subscription"),
            offer_sdp=OFFER,
            instructions="",
            voice=None,
            connect=connect,
        )

    assert route.call_count == 1
    assert connect.calls == []


@pytest.mark.asyncio
@respx.mock
async def test_subscription_token_without_account_is_rejected_before_creation():
    route = respx.post(CODEX_CALLS_URL).respond(201, text=ANSWER)

    with pytest.raises(ProviderAuthError):
        await open_openai_live_wire(
            _runtime("opaque-token"),
            parse_task_model_target_id("openai/gpt-live-1-codex::subscription"),
            offer_sdp=OFFER,
            instructions="",
            voice=None,
            connect=FakeConnect(),
        )
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_control_join_failure_reports_the_created_call():
    respx.post(PUBLIC_SESSIONS_URL).respond(
        201, json={"session": {"id": "live_9"}, "transport": {"sdp": ANSWER}}
    )

    with pytest.raises(ControlJoinError) as caught:
        await open_openai_live_wire(
            _runtime("sk-test"),
            parse_task_model_target_id("openai/gpt-live-1::api-key"),
            offer_sdp=OFFER,
            instructions="",
            voice=None,
            connect=FakeConnect(error=OSError("refused")),
        )
    assert caught.value.call_id == "live_9"


@pytest.mark.asyncio
async def test_codex_events_become_turn_captions_delegations_and_close():
    socket = FakeSocket(
        [
            {"type": "session.started", "session": {"id": "rtc_1", "expires_at": 1790287137}},
            {"type": "session.input_audio.append", "audio": "AAAA"},
            {"type": "turn.created", "turn": {"id": "t1", "role": "user", "transcript": " Please"}},
            {"type": "input_transcript.added", "item": {"text": " check"}},
            {"type": "turn.delta", "turn_id": "t1", "delta": " check"},
            {"type": "turn.delta", "turn_id": "unknown", "delta": "x"},
            {
                "type": "delegation.created",
                "item": {
                    "id": "item_1",
                    "type": "delegation",
                    "content": [{"type": "input_text", "text": "Check whether the build passed"}],
                    "target": "client",
                },
            },
            {
                "type": "turn.done",
                "turn": {"id": "t1", "role": "user", "transcript": " Please check"},
            },
            {"type": "session.usage.updated", "usage": {"audio_duration_ms": 1200}},
            {"type": "error", "error": {"code": "invalid_value", "message": "bad"}},
            "not json",
            {
                "type": "session.closed",
                "reason": "client_request",
                "usage": {"audio_duration_ms": 1700},
            },
            {"type": "turn.created", "turn": {"id": "late", "role": "user", "transcript": "x"}},
        ]
    )
    wire = OpenAILiveWire(call_id="rtc_1", answer_sdp=ANSWER, socket=socket, dialect="codex")

    assert await _collect(wire) == [
        WireStarted(expires_at=1790287137.0),
        WireCaption(role="user", text="Please", final=False),
        WireCaption(role="user", text="Please check", final=False),
        WireDelegation(delegation_id="item_1", request="Check whether the build passed"),
        WireCaption(role="user", text="Please check", final=True),
        WireUsage({"audio_duration_ms": 1200}),
        WireProblem(code="invalid_value", message="bad"),
        WireClosed(reason="client_request", usage={"audio_duration_ms": 1700}, confirmed=True),
    ]


@pytest.mark.asyncio
async def test_public_events_split_captions_by_role_and_carry_no_request_text():
    socket = FakeSocket(
        [
            {"type": "session.input_transcript.delta", "delta": "Start two"},
            {"type": "session.input_transcript.delta", "delta": " terminals"},
            {
                "type": "session.delegation.created",
                "delegation": {"id": "del_1", "target": "client"},
            },
            {
                "type": "session.delegation.created",
                "delegation": {"id": "del_2", "target": "responses"},
            },
            {"type": "session.output_transcript.delta", "delta": "On it."},
            {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 12}},
        ]
    )
    wire = OpenAILiveWire(call_id="live_1", answer_sdp=ANSWER, socket=socket, dialect="public")

    assert await _collect(wire) == [
        WireCaption(role="user", text="Start two", final=False),
        WireCaption(role="user", text="Start two terminals", final=False),
        WireDelegation(delegation_id="del_1", request=None),
        WireCaption(role="user", text="Start two terminals", final=True),
        WireCaption(role="assistant", text="On it.", final=False),
        WireCaption(role="assistant", text="On it.", final=True),
        WireClosed(reason="close_requested", usage={"seconds": 12}, confirmed=True),
    ]


@pytest.mark.asyncio
async def test_lost_control_channel_ends_with_unconfirmed_close():
    wire = OpenAILiveWire(
        call_id="rtc_1", answer_sdp=ANSWER, socket=FakeSocket([None]), dialect="codex"
    )

    assert await _collect(wire) == [WireClosed(reason=None, usage=None, confirmed=False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["codex", "public"])
async def test_confirmed_live_call_closure_retires_its_control_socket(dialect: str):
    socket = FakeSocket(
        [{"type": "session.closed", "reason": "client_request", "usage": {"seconds": 12}}]
    )
    wire = OpenAILiveWire(call_id="rtc_1", answer_sdp=ANSWER, socket=socket, dialect=dialect)
    updates: list[dict[str, Any]] = []
    host: Any = SimpleNamespace(publish=updates.append)
    call = LiveCallSession(wire=wire, brain=None, host=host, target="openai/test-live")
    call.start()

    await asyncio.wait_for(call.wait_closed(), 1)
    await call.close()
    await call.abort()

    assert socket.closed
    assert [update for update in updates if update["type"] == "closed"] == [
        {"type": "closed", "reason": "client_request", "usage": {"seconds": 12}}
    ]


@pytest.mark.asyncio
async def test_codex_results_and_announcements_use_bounded_speakable_appends():
    socket = FakeSocket()
    wire = OpenAILiveWire(call_id="rtc_1", answer_sdp=ANSWER, socket=socket, dialect="codex")
    long_text = "Grüße " * 150

    await wire.deliver_result("item_1", long_text)
    await wire.announce("vBot update: done")
    await wire.request_close()

    results = [event for event in socket.sent if event["type"] == "delegation.context.append"]
    assert len(results) > 1
    for event in results:
        assert event["delegation_item_id"] == "item_1"
        assert event["channel"] == "speakable"
        assert len(event["content"][0]["text"].encode("utf-8")) <= 500
    assert " ".join(event["content"][0]["text"] for event in results) == long_text.strip()
    assert socket.sent[-2] == {
        "type": "session.context.append",
        "channel": "speakable",
        "content": [{"type": "input_text", "text": "vBot update: done"}],
    }
    assert socket.sent[-1] == {"type": "session.close"}


@pytest.mark.asyncio
async def test_public_results_use_commentary_and_closed_socket_raises_send_error():
    socket = FakeSocket()
    wire = OpenAILiveWire(call_id="live_1", answer_sdp=ANSWER, socket=socket, dialect="public")

    await wire.deliver_result("del_1", "Two terminals started.")
    await wire.announce("vBot update: done")

    assert socket.sent == [
        {
            "type": "session.commentary.append",
            "delegation_id": "del_1",
            "content": "Two terminals started.",
        },
        {
            "type": "session.commentary.append",
            "delegation_id": None,
            "content": "vBot update: done",
        },
    ]
    await wire.aclose()
    with pytest.raises(WireSendError):
        await wire.request_close()


def test_chunk_text_respects_byte_budget_without_splitting_characters():
    text = "ab " + "é" * 400 + " tail"
    chunks = chunk_text(text, 100)

    assert all(len(chunk.encode("utf-8")) <= 100 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")
    assert chunk_text("   ", 100) == []
