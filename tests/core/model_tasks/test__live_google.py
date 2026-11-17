"""Offline verification of the Gemini Live wire against the verified wire facts."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from types import SimpleNamespace
from typing import Any, override

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from core.model_tasks._live_google import GoogleLiveWire, open_google_live_wire
from core.model_tasks._live_google_session import GoogleSession
from core.model_tasks._live_wire import (
    PlaybackReceipt,
    WireAudio,
    WireCaption,
    WireClosed,
    WireConnection,
    WirePlaybackClear,
    WireProblem,
    WireStarted,
    WireToolCall,
    WireUsage,
    relay_media,
)
from core.model_tasks.model_tasks import parse_task_model_target_id
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import StaticTokenGetter

TARGET = "google/gemini-3.8-live::api-key"
INSTRUCTIONS = "Talk with the user."
TOOLS = [
    {
        "name": "open",
        "description": "Show something.",
        "parameters": {
            "type": "object",
            "properties": {"targets": {"type": "array", "items": {"type": "string"}}},
            "required": ["targets"],
            "additionalProperties": False,
        },
    }
]
SAMPLES = 2400  # one tenth of a second at 24 kHz


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _session(*, clock: Clock | None = None, **kwargs: Any) -> GoogleSession:
    session = GoogleSession(
        model_id="gemini-3.8-live",
        instructions=INSTRUCTIONS,
        voice=kwargs.pop("voice", "kore"),
        tools=kwargs.pop("tools", TOOLS),
        clock=clock or Clock(),
        **kwargs,
    )
    session.start()
    session.playback(PlaybackReceipt(1, 0, enabled=True))
    return session


def _audio(samples: int = SAMPLES, rate: int = 24000) -> dict:
    data = base64.b64encode(bytes(samples * 2)).decode("ascii")
    part = {"inlineData": {"mimeType": f"audio/pcm;rate={rate}", "data": data}}
    return {"serverContent": {"modelTurn": {"parts": [part]}}}


def _said(text: str) -> dict:
    return {"serverContent": {"outputTranscription": {"text": text}}}


def _heard(text: str) -> dict:
    return {"serverContent": {"inputTranscription": {"text": text}}}


def _turn_complete(**extra: Any) -> dict:
    return {"serverContent": {"turnComplete": True, **extra}}


def _call(call_id: str, name: str = "open", args: Any = None) -> dict:
    call = {
        "id": call_id,
        "name": name,
        "args": {"targets": ["terminals"]} if args is None else args,
    }
    return {"toolCall": {"functionCalls": [call]}}


def _handle(handle: str = "h1") -> dict:
    return {"sessionResumptionUpdate": {"newHandle": handle, "resumable": True}}


def _announcements(commands: list[dict]) -> list[list[str]]:
    return [
        [part["text"] for part in command["clientContent"]["turns"][0]["parts"]]
        for command in commands
        if "clientContent" in command
    ]


# -- session ------------------------------------------------------------------


def test_setup_names_the_model_voice_thinking_transcripts_and_json_schema_tools():
    setup = _session(thinking_level="low", non_blocking_tools=True).setup()["setup"]

    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["generationConfig"] == {
        "responseModalities": ["AUDIO"],
        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "kore"}}},
        "thinkingConfig": {"thinkingLevel": "low"},
    }
    assert setup["systemInstruction"] == {"parts": [{"text": INSTRUCTIONS}]}
    assert setup["inputAudioTranscription"] == setup["outputAudioTranscription"] == {}
    assert setup["contextWindowCompression"] == {"slidingWindow": {}}
    assert setup["sessionResumption"] == {}
    # The typed ``parameters`` field rejects keywords the canonical schema uses.
    assert setup["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "open",
                    "description": "Show something.",
                    "parametersJsonSchema": TOOLS[0]["parameters"],
                    "behavior": "NON_BLOCKING",
                }
            ]
        }
    ]
    plain = _session(voice=None, tools=[]).setup()["setup"]
    assert "speechConfig" not in plain["generationConfig"]
    assert "thinkingConfig" not in plain["generationConfig"]
    assert "tools" not in plain


def test_a_model_turn_streams_audio_and_captions_and_finishes_them_once():
    session = _session()
    session.receive(_heard("What is"))
    heard = session.receive(_heard(" the time?"))
    first = session.receive(_audio())
    said = session.receive(_said("It is"))
    second = session.receive(
        {"serverContent": {**_audio()["serverContent"], "outputTranscription": {"text": " noon."}}}
    )
    done = session.receive(_turn_complete())

    assert heard.events == [WireCaption("user", "What is the time?", final=False)]
    # The model answers, so the user's turn is over.
    assert first.events[0] == WireCaption("user", "What is the time?", final=True)
    assert first.events[1] == WireAudio("turn_1", bytes(SAMPLES * 2), 1, 0)
    assert said.events == [WireCaption("assistant", "It is", final=False)]
    assert second.events == [
        WireAudio("turn_1", bytes(SAMPLES * 2), 1, SAMPLES),
        WireCaption("assistant", "It is noon.", final=False),
    ]
    assert done.events == [WireCaption("assistant", "It is noon.", final=True)]
    assert session.receive(_turn_complete()).events == []
    # Audio at another rate is not relayed.
    assert session.receive(_audio(rate=16000)).events == []


def test_microphone_audio_goes_out_at_the_relay_rate_only_while_attached():
    session = _session()

    assert session.audio(b"\x01\x00") == [
        {"realtimeInput": {"audio": {"data": "AQA=", "mimeType": "audio/pcm;rate=24000"}}}
    ]
    session.receive(_handle())
    session.detach()
    assert session.audio(b"\x01\x00") == []


def test_barge_in_clears_playback_and_later_audio_starts_a_new_generation():
    session = _session()
    session.receive(_audio())
    session.receive(_said("Once upon"))

    interrupted = session.receive({"serverContent": {"interrupted": True}})
    after = session.receive(_audio())

    assert interrupted.events == [
        WireCaption("assistant", "Once upon", final=True),
        WirePlaybackClear(2),
    ]
    assert after.events == [WireAudio("turn_2", bytes(SAMPLES * 2), 2, 0)]
    # A receipt for the cleared generation cannot hold the gate.
    session.receive(_turn_complete())
    session.playback(PlaybackReceipt(2, SAMPLES, enabled=True))
    assert _announcements(session.announce("vBot update")) == [["vBot update"]]


def test_function_calls_run_once_and_results_return_as_tool_responses():
    session = _session()

    called = session.receive(_call("c1"))
    repeated = session.receive(_call("c1"))
    answered = session.deliver("c1", "Opened.")

    assert called.events == [WireToolCall("c1", "open", {"targets": ["terminals"]})]
    assert repeated.events == []
    assert answered == [
        {
            "toolResponse": {
                "functionResponses": [
                    {"id": "c1", "name": "open", "response": {"output": "Opened."}}
                ]
            }
        }
    ]
    assert session.deliver("c1", "Again.") == []
    # A call the model cancelled gets no late result.
    session.receive(_call("c2"))
    session.receive({"toolCallCancellation": {"ids": ["c2"]}})
    assert session.deliver("c2", "Too late.") == []


@pytest.mark.parametrize(
    ("busy", "release"),
    [
        ([_said("Hi.")], [_turn_complete()]),
        (
            [{"voiceActivity": {"type": "ACTIVITY_START"}}],
            [{"voiceActivity": {"type": "ACTIVITY_END"}}],
        ),
        # The answer to the result is a model turn of its own.
        ([_call("c1")], ["deliver", _turn_complete()]),
        ([_audio(), _turn_complete()], ["drained"]),
    ],
    ids=["model-speaking", "user-speaking", "call-waiting", "playback-undrained"],
)
def test_announcements_wait_for_an_idle_conversation_and_go_out_as_one_user_turn(busy, release):
    session = _session()
    for message in busy:
        session.receive(message)

    assert session.announce("First.") == []
    assert session.announce("Second.") == []

    commands: list[dict] = []
    for message in release:
        if message == "deliver":
            commands += session.deliver("c1", "Done.")
        elif message == "drained":
            commands += session.playback(PlaybackReceipt(1, SAMPLES, enabled=True)).commands
        else:
            commands += session.receive(message).commands
    assert _announcements(commands) == [["First.", "Second."]]
    assert commands[-1]["clientContent"]["turnComplete"] is True


def test_a_stalled_turn_stops_blocking_and_extended_thinking_keeps_its_turn_open():
    clock = Clock()
    session = _session(clock=clock)
    session.receive(_said("Let me check."))
    filler = session.receive(_turn_complete(interactionStatus="IN_PROGRESS"))
    assert filler.events == [WireCaption("assistant", "Let me check.", final=True)]
    assert session.announce("vBot update") == []
    assert session.next_deadline() == 160.0

    clock.now = 159.0
    assert session.tick().commands == []
    clock.now = 160.0
    assert _announcements(session.tick().commands) == [["vBot update"]]


def test_usage_sums_every_turn_and_counts_thoughts_as_output():
    session = _session()

    first = session.receive(
        {
            "usageMetadata": {
                "promptTokenCount": 500,
                "responseTokenCount": 50,
                "thoughtsTokenCount": 40,
            }
        }
    )
    second = session.receive(
        {
            "usageMetadata": {
                "promptTokenCount": 600,
                "responseTokenCount": 10,
                "cachedContentTokenCount": 0,
            }
        }
    )

    assert first.events == [
        WireUsage({"input_tokens": 500, "output_tokens": 90, "reasoning_tokens": 40})
    ]
    assert second.events == [
        WireUsage({"input_tokens": 1100, "output_tokens": 100, "reasoning_tokens": 40})
    ]
    assert session.receive({"usageMetadata": {}}).events == []
    assert session.finish()[-1].usage == {
        "input_tokens": 1100,
        "output_tokens": 100,
        "reasoning_tokens": 40,
    }


@pytest.mark.parametrize(
    ("messages", "code", "resumes", "closed"),
    [
        ([_handle()], 1006, True, WireClosed(None, None, confirmed=False)),
        ([_handle()], 1011, True, None),
        ([], 1006, False, WireClosed(None, None, confirmed=False)),
        ([_handle()], 1000, False, WireClosed(None, None, confirmed=False)),
        (
            [_handle(), {"goAway": {"timeLeft": "50s"}}],
            1000,
            True,
            WireClosed("max_duration", None, confirmed=True),
        ),
        ([_handle()], 1007, False, None),
    ],
    ids=["dropped", "internal-error", "no-handle", "normal-close", "go-away", "setup-error"],
)
def test_connection_ends_decide_between_resuming_and_closing(messages, code, resumes, closed):
    session = _session()
    for message in messages:
        session.receive(message)

    assert session.resumes_after(code) is resumes
    events = session.finish(code=code, reason="Internal error encountered.")
    if closed is None:
        # Error closes report the server's reason and end the call.
        assert events == [
            WireProblem(f"close_{code}", "Internal error encountered.", fatal=True),
            WireClosed("provider_error", None, confirmed=True),
        ]
    else:
        assert events == [closed]


# -- socket wire ----------------------------------------------------------------


class FakeSocket:
    """A Live WebSocket fed from a queue: frames, close errors, ``None`` for a bare end."""

    def __init__(self, *frames: Any, setup_complete: bool = True) -> None:
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        if setup_complete:
            self.push({"setupComplete": {}})
        for frame in frames:
            self.push(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def push(self, frame: Any) -> None:
        self.frames.put_nowait(frame)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> Any:
        return await self.recv()

    async def recv(self) -> Any:
        frame = await self.frames.get()
        if isinstance(frame, BaseException):
            raise frame
        if frame is None or self.closed:
            raise ConnectionClosedError(None, None)
        # Gemini sends its JSON in binary frames.
        return frame if isinstance(frame, str | bytes) else json.dumps(frame).encode()

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedError(None, None)
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True
        self.frames.put_nowait(None)


class FakeConnect:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **options: Any) -> FakeSocket:
        self.calls.append((url, options))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, FakeSocket)
        return outcome


def _runtime() -> SimpleNamespace:
    provider = ProviderConfig(
        id="google",
        name="Google AI Studio",
        adapter="google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(header="x-goog-api-key", prefix="", credential_key=""),
            )
        ],
    )
    return SimpleNamespace(
        providers=SimpleNamespace(get=lambda provider_id: provider),
        prepare_provider_http=lambda connection: (StaticTokenGetter("google-key"), None),
    )


def _closed(code: int, reason: str) -> ConnectionClosedError:
    return ConnectionClosedError(Close(code, reason), None)


def _rejected(status: int, body: bytes = b'{"error":{"message":"denied"}}') -> InvalidStatus:
    return InvalidStatus(Response(status, "Rejected", Headers(), body))


async def _open(connect: FakeConnect, **kwargs: Any) -> GoogleLiveWire:
    return await open_google_live_wire(
        _runtime(),
        parse_task_model_target_id(TARGET),
        instructions=INSTRUCTIONS,
        voice=kwargs.pop("voice", "kore"),
        tools=kwargs.pop("tools", TOOLS),
        connect=connect,
        **kwargs,
    )


async def _until(events: list[Any], kind: type, count: int = 1) -> None:
    async with asyncio.timeout(2):
        while sum(isinstance(event, kind) for event in events) < count:
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_open_joins_with_the_api_key_header_and_returns_after_setup_completes():
    socket = FakeSocket()
    connect = FakeConnect(socket)

    wire = await _open(connect, thinking_level="low")

    url, options = connect.calls[0]
    assert url == (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )
    assert options["additional_headers"]["x-goog-api-key"] == "google-key"
    assert options["max_size"] == 16 * 1024 * 1024
    assert options["ssl"] is not None
    assert socket.sent[0]["setup"]["model"] == "models/gemini-3.8-live"
    assert socket.sent[0]["setup"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert re.fullmatch(r"live_[a-z0-9]{16}", wire.call_id)
    assert wire.media == relay_media()
    assert wire.announces_as_user_input is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error", "retryable"),
    [
        (
            _closed(1007, "API key not valid. Please pass a valid API key."),
            ProviderAuthError,
            False,
        ),
        (_closed(1008, "models/x is not found for API version v1beta"), ProviderError, False),
        (_closed(1007, "Thinking level is not supported for this model."), ProviderError, False),
        (_closed(1011, "Quota exceeded for metric ..., limit: 0, model: x"), ProviderError, False),
        (
            _closed(1011, "You exceeded your current quota. Please retry in 7s."),
            ProviderRateLimitError,
            True,
        ),
        (_closed(1011, "Internal error encountered."), ProviderError, True),
        (ConnectionClosedError(None, None), NetworkError, True),
    ],
    ids=[
        "invalid-key",
        "unknown-model",
        "invalid-setup",
        "zero-limit",
        "throttled",
        "internal",
        "dropped",
    ],
)
async def test_setup_closes_are_classified_with_the_adapter_status_policy(
    failure, error, retryable
):
    socket = FakeSocket(failure, setup_complete=False)

    with pytest.raises(error) as caught:
        await _open(FakeConnect(socket))

    assert type(caught.value) is error
    assert caught.value.retryable is retryable
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (_rejected(403), ProviderAuthError),
        (_rejected(429), ProviderRateLimitError),
        (_rejected(404), ProviderError),
        (OSError("refused"), NetworkError),
        (TimeoutError(), NetworkError),
    ],
    ids=["403", "429", "404", "refused", "timeout"],
)
async def test_handshake_failures_map_to_provider_errors(failure, error):
    with pytest.raises(error):
        await _open(FakeConnect(failure))


@pytest.mark.asyncio
async def test_socket_messages_are_normalized_and_the_wire_answers_on_the_socket():
    socket = FakeSocket()
    wire = await _open(FakeConnect(socket))
    events: list[Any] = []

    async def read() -> None:
        async for event in wire.events():
            events.append(event)

    reader = asyncio.create_task(read())
    socket.push("not json")
    socket.push(b"\xff")
    socket.push(_call("c1"))
    await _until(events, WireToolCall)
    await wire.send_audio(b"\x01\x00")
    await wire.deliver_result("c1", "Done.")
    await wire.request_close()
    async with asyncio.timeout(2):
        await reader

    assert events == [
        WireStarted(expires_at=None),
        WireToolCall("c1", "open", {"targets": ["terminals"]}),
        WireClosed(reason=None, usage=None, confirmed=False),
    ]
    assert [next(iter(command)) for command in socket.sent[1:]] == ["realtimeInput", "toolResponse"]


@pytest.mark.asyncio
async def test_go_away_moves_the_idle_session_to_a_resumed_connection_unnoticed():
    first, second = FakeSocket(), FakeSocket()
    wire = await _open(FakeConnect(first, second))
    await wire.update_playback(PlaybackReceipt(1, 0, enabled=True))
    events: list[Any] = []

    async def read() -> None:
        async for event in wire.events():
            events.append(event)

    reader = asyncio.create_task(read())
    first.push(_handle("h1"))
    first.push({"goAway": {"timeLeft": "50s"}})
    async with asyncio.timeout(2):
        while not first.closed:
            await asyncio.sleep(0.005)
    second.push(_said("Still here."))
    second.push(_turn_complete())
    await _until(events, WireCaption)
    await wire.announce("vBot update")
    await wire.request_close()
    async with asyncio.timeout(2):
        await reader

    assert second.sent[0]["setup"]["sessionResumption"] == {"handle": "h1"}
    assert _announcements(second.sent) == [["vBot update"]]
    assert not any(isinstance(event, WireConnection) for event in events)
    assert events[-1] == WireClosed(reason=None, usage=None, confirmed=False)


@pytest.mark.asyncio
async def test_a_dropped_connection_resumes_and_sends_results_held_while_detached():
    first = FakeSocket()
    blocked = asyncio.Event()

    class SlowSocket(FakeSocket):
        @override
        async def send(self, message: str) -> None:
            await blocked.wait()
            await super().send(message)

    second = SlowSocket()
    wire = await _open(FakeConnect(first, second))
    events: list[Any] = []

    async def read() -> None:
        async for event in wire.events():
            events.append(event)

    reader = asyncio.create_task(read())
    first.push(_handle("h1"))
    first.push(_call("c1"))
    await _until(events, WireToolCall)
    first.push(_closed(1006, ""))
    await _until(events, WireConnection)
    await wire.deliver_result("c1", "Opened.")
    blocked.set()
    await _until(events, WireConnection, 2)
    await wire.request_close()
    async with asyncio.timeout(2):
        await reader

    assert [event for event in events if isinstance(event, WireConnection)] == [
        WireConnection(reconnecting=True),
        WireConnection(reconnecting=False),
    ]
    assert first.sent[1:] == []
    assert second.sent[0]["setup"]["sessionResumption"] == {"handle": "h1"}
    assert second.sent[1]["toolResponse"]["functionResponses"][0]["id"] == "c1"


@pytest.mark.asyncio
async def test_an_error_close_during_the_call_ends_it_without_resuming():
    socket = FakeSocket()
    connect = FakeConnect(socket)
    wire = await _open(connect)
    events: list[Any] = []
    socket.push(_handle())
    socket.push(_closed(1008, "Policy violation"))

    async with asyncio.timeout(2):
        async for event in wire.events():
            events.append(event)

    assert len(connect.calls) == 1
    assert events[-2:] == [
        WireProblem("close_1008", "Policy violation", fatal=True),
        WireClosed("provider_error", None, confirmed=True),
    ]
