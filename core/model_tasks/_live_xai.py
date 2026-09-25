"""xAI Grok Voice wire: the realtime WebSocket with server-relayed audio.

xAI offers no WebRTC endpoint, so the server joins
``wss://api.x.ai/v1/realtime?model=<id>`` itself and relays audio: microphone
PCM from the accessor is appended to the input buffer, and assistant audio
returns as :class:`WireAudio` (PCM16 mono 24 kHz both ways). Server VAD takes
turns and cancels a response the user talks over. The voice model gets one
function Tool: ``vbot_request`` (delegation to the backend model), or the Live
Tools themselves in direct Tools mode, where every function call goes to the
call, which prepares it (other names and argument spellings included).

Wire facts verified live (2026-09-25): ``session.updated`` answers the
``session.update`` sent right after connect; audio arrives faster than real
time; function calls follow the spoken part of the same response; a response
the user talked over may end with ``status: "completed"`` and an empty
output, or never end; user transcription snapshots are cumulative and only a
``.completed`` event with ``status: "completed"`` is final; the transcript in
``conversation.item.truncated`` can belong to another item and is ignored;
``usage`` is empty on subscription sessions.

:class:`_XaiSession` is a sans-IO state machine: it normalizes provider
events, keeps the response gate and the playback estimate, and returns the
client events to send. :class:`XaiLiveWire` drives it with socket frames,
commands, and timer ticks, sending each step's client events under one lock so
they keep their order.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeGuard
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException

from core.model_tasks._live_tools import (
    LIVE_TOOL_REQUEST,
    live_failure,
    live_result_text,
    live_tools,
    request_tool,
)
from core.model_tasks._live_wire import (
    RELAY_BYTES_PER_MS,
    RELAY_SAMPLE_RATE,
    JsonObject,
    WireAudio,
    WireCaption,
    WireClosed,
    WireDelegation,
    WireEvent,
    WirePlaybackClear,
    WireProblem,
    WireSendError,
    WireStarted,
    WireToolCall,
    WireUsage,
    relay_media,
    websocket_url,
)
from core.providers._http_shared import classify_http_status
from core.providers.errors import NetworkError, ProviderError
from core.providers.task_client import ProviderTaskClient
from core.providers.token_getter import OAuthRequestRecovery
from core.providers.tool_schema import render_tool_definitions
from core.tools import called_tool_name
from core.utils.ids import new_id
from core.utils.tls import shared_ssl_context

REALTIME_PATH = "/realtime"
SESSION_LIFETIME_SECONDS = 120 * 60
TURN_DETECTION: JsonObject = {
    "type": "server_vad",
    "threshold": 0.85,
    "prefix_padding_ms": 333,
    "silence_duration_ms": 500,
}
TRANSCRIPTION_MODEL = "grok-transcribe"

_OPEN_TIMEOUT_SECONDS = 10.0
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_ERROR_BODY_CHARS = 500
# A response.create that got no response.created is retried once, then dropped.
_CREATE_PENDING_SECONDS = 5.0
_CREATE_ATTEMPTS = 2
# A response without events for this long no longer blocks the gate.
_RESPONSE_STALL_SECONDS = 30.0
# Backoff after the provider reported another active response.
_ACTIVE_RESPONSE_RETRY_SECONDS = 1.0
# Played audio is estimated; wait a little longer before speaking again.
_PLAYBACK_DRAIN_MARGIN_SECONDS = 0.2
_REMEMBERED_IDS = 256
_REMEMBERED_RESPONSES = 32
_AUDIO_ITEMS = 8
_CLOSE_ERROR_TYPES = ("max_duration", "timeout")
_AUDIO_DELTAS = frozenset({"response.output_audio.delta", "response.audio.delta"})
_AUDIO_DONES = frozenset({"response.output_audio.done", "response.audio.done"})
_TRANSCRIPT_DELTAS = frozenset(
    {
        "response.output_audio_transcript.delta",
        "response.audio_transcript.delta",
        "response.output_text.delta",
        "response.text.delta",
    }
)
_TRANSCRIPT_DONES = frozenset(
    {
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
        "response.output_text.done",
        "response.text.done",
    }
)
_USER_TRANSCRIPTS = frozenset(
    {
        "conversation.item.input_audio_transcription.updated",
        "conversation.item.input_audio_transcription.completed",
    }
)
_KIND_DELEGATION = "delegation"
_KIND_TOOL = "tool"

WebSocketConnector = Callable[..., Awaitable[Any]]
Clock = Callable[[], float]


async def open_xai_live_wire(
    runtime: Any,
    target_ref: Any,
    *,
    instructions: str,
    voice: str | None,
    direct_tools: bool,
    connect: WebSocketConnector | None = None,
    clock: Clock = time.monotonic,
    wall_clock: Clock = time.time,
) -> XaiLiveWire:
    """Join the realtime socket and configure the session.

    *direct_tools* registers the Live app Tools instead of ``vbot_request``.
    Handshake failures raise Provider errors (HTTP 401/403 as
    :class:`~core.providers.errors.ProviderAuthError`, 429 as a rate limit);
    transport failures raise :class:`~core.providers.errors.NetworkError`.
    """

    client = _XaiLiveClient.from_runtime(runtime, target_ref)
    socket = await client.connect(connect or websocket_connect)
    session = _XaiSession(direct_tools=direct_tools, clock=clock, wall_clock=wall_clock)
    try:
        await socket.send(json.dumps(session.configure(instructions, voice), ensure_ascii=False))
    except ConnectionClosed as exc:
        with contextlib.suppress(Exception):
            await socket.close()
        raise NetworkError("The xAI realtime connection closed during setup") from exc
    return XaiLiveWire(call_id=new_id("live"), socket=socket, session=session, clock=clock)


class _XaiLiveClient(ProviderTaskClient):
    """Opens realtime sockets on one resolved xAI Connection."""

    async def connect(self, connect: WebSocketConnector) -> Any:
        url = (
            f"{websocket_url(self._base_url, REALTIME_PATH)}?{urlencode({'model': self._model_id})}"
        )
        recovery = OAuthRequestRecovery(self._token_getter, self._connection.auth)

        async def attempt() -> Any:
            headers = await self._headers()
            options: JsonObject = {
                "additional_headers": headers,
                "open_timeout": _OPEN_TIMEOUT_SECONDS,
                "max_size": _MAX_FRAME_BYTES,
            }
            if url.startswith("wss://"):
                options["ssl"] = shared_ssl_context()
            try:
                return await connect(url, **options)
            except InvalidStatus as exc:
                response = exc.response
                body = _body_text(response.body)
                recovery.record_response(response.status_code, headers, body)
                classify_http_status(
                    response.status_code,
                    idempotent=True,
                    detail=body or str(response.status_code),
                    response_headers=httpx.Headers(list(response.headers.raw_items())),
                )
                raise ProviderError(
                    f"xAI realtime handshake failed with HTTP {response.status_code}",
                    retryable=False,
                ) from exc
            except InvalidURI as exc:
                raise ProviderError(f"Invalid xAI realtime URL: {exc}", retryable=False) from exc
            except (OSError, TimeoutError, WebSocketException) as exc:
                detail = str(exc).strip() or type(exc).__name__
                raise NetworkError(f"xAI realtime connection failed: {detail}") from exc

        return await recovery.run(attempt)


def _body_text(body: bytes | bytearray | None) -> str:
    if not body:
        return ""
    return body.decode("utf-8", errors="replace").strip()[:_ERROR_BODY_CHARS]


class XaiLiveWire:
    """One joined xAI realtime session relaying audio for a Live call."""

    def __init__(
        self,
        *,
        call_id: str,
        socket: Any,
        session: _XaiSession,
        clock: Clock = time.monotonic,
    ) -> None:
        self._call_id = call_id
        self._socket = socket
        self._session = session
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state_changed = asyncio.Event()

    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def media(self) -> JsonObject:
        return relay_media()

    @property
    def announces_as_user_input(self) -> bool:
        return True

    async def events(self) -> AsyncIterator[WireEvent]:
        timer = asyncio.create_task(self._run_timer(), name=f"live-xai-timer:{self._call_id}")
        try:
            async for frame in self._socket:
                event = _decode(frame)
                if event is None:
                    continue
                async with self._lock:
                    step = self._session.receive(event)
                    with contextlib.suppress(WireSendError):
                        await self._send(step.commands)
                self._state_changed.set()
                if step.close:
                    await self.aclose()
                for normalized in step.events:
                    yield normalized
        except ConnectionClosed:
            pass
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
        for normalized in self._session.finish():
            yield normalized

    async def deliver_result(self, delegation_id: str, text: str) -> None:
        async with self._lock:
            await self._send(self._session.deliver(delegation_id, text))
        self._state_changed.set()

    async def announce(self, text: str) -> None:
        async with self._lock:
            await self._send(self._session.announce(text))
        self._state_changed.set()

    async def send_audio(self, pcm: bytes) -> None:
        async with self._lock:
            await self._send(self._session.audio(pcm))

    async def request_close(self) -> None:
        """xAI has no session close event; closing the socket ends the session."""
        await self.aclose()

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._socket.close()

    async def _send(self, commands: list[JsonObject]) -> None:
        try:
            for command in commands:
                await self._socket.send(json.dumps(command, ensure_ascii=False))
        except ConnectionClosed as exc:
            raise WireSendError("xAI realtime connection is closed") from exc

    async def _run_timer(self) -> None:
        """Re-check the response gate when one of its deadlines passes."""

        while True:
            self._state_changed.clear()
            deadline = self._session.next_deadline()
            if deadline is None:
                await self._state_changed.wait()
                continue
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(0.0, deadline - self._clock())):
                    await self._state_changed.wait()
                continue
            async with self._lock:
                try:
                    await self._send(self._session.tick())
                except WireSendError:
                    return


def _decode(frame: Any) -> JsonObject | None:
    if not isinstance(frame, str):
        return None
    try:
        event = json.loads(frame)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


@dataclass
class _Step:
    """The result of one provider event: normalized events and client events."""

    events: list[WireEvent] = field(default_factory=list)
    commands: list[JsonObject] = field(default_factory=list)
    close: bool = False


@dataclass
class _Call:
    call_id: str
    name: str
    arguments: Any


@dataclass
class _Response:
    covers: int
    touched: float
    open_user_items: tuple[str, ...] = ()
    calls: dict[str, _Call] = field(default_factory=dict)
    has_output: bool = False


@dataclass
class _AudioItem:
    item_id: str
    response_id: str | None
    play_start: float
    play_end: float
    produced_bytes: int = 0
    complete: bool = False


class _Recent:
    """The most recent ids with an optional value, bounded."""

    def __init__(self, limit: int = _REMEMBERED_IDS) -> None:
        self._limit = limit
        self._items: OrderedDict[str, str] = OrderedDict()

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def get(self, key: str) -> str | None:
        return self._items.get(key)

    def put(self, key: str, value: str = "") -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)


class _XaiSession:
    """Sans-IO state of one xAI realtime session.

    Response gate: vBot sends ``response.create`` only for its own additions
    (Tool outputs and announcements) that no completed response covered yet,
    and only while no response is pending or active, the user is not
    speaking, every function call of the last completed response has an
    output, and the estimated playback has drained. One create covers every
    pending addition.
    """

    def __init__(
        self,
        *,
        direct_tools: bool,
        clock: Clock = time.monotonic,
        wall_clock: Clock = time.time,
    ) -> None:
        self._direct_tools = direct_tools
        self._clock = clock
        self._wall_clock = wall_clock
        self._started = False
        self._close_reason: str | None = None
        self._usage: dict[str, int | float] = {}
        # Response gate.
        self._responses: OrderedDict[str, _Response] = OrderedDict()
        self._active: str | None = None
        self._create_sent_at: float | None = None
        self._create_event_id: str | None = None
        self._create_covers = 0
        self._create_attempts = 0
        self._creates = 0
        self._retry_after = 0.0
        self._user_speaking = False
        self._added = 0
        self._spoken = 0
        self._awaiting: dict[str, str] = {}
        self._last_completed_calls: frozenset[str] = frozenset()
        self._handled_calls = _Recent()
        self._fenced = _Recent()
        # Playback estimate of forwarded assistant audio.
        self._playback_end = 0.0
        # When vBot may speak again: the playback end plus a margin, or the
        # moment the accessor cleared its playback.
        self._quiet_at = 0.0
        self._audio_items: OrderedDict[str, _AudioItem] = OrderedDict()
        self._audio_carrier = _Recent()
        # Captions.
        self._caption_carrier = _Recent()
        self._assistant_text: dict[str, str] = {}
        self._item_response: dict[str, str | None] = {}
        self._user_open: dict[str, str] = {}
        self._finalized = _Recent()

    # -- commands ---------------------------------------------------------

    def configure(self, instructions: str, voice: str | None) -> JsonObject:
        """Return the ``session.update`` that configures this session."""

        audio_format = {"type": "audio/pcm", "rate": RELAY_SAMPLE_RATE}
        tools = live_tools() if self._direct_tools else [request_tool()]
        session: JsonObject = {
            "instructions": instructions,
            "turn_detection": dict(TURN_DETECTION),
            "audio": {
                "input": {
                    "format": dict(audio_format),
                    "transcription": {"model": TRANSCRIPTION_MODEL},
                },
                "output": {"format": dict(audio_format)},
            },
            "tools": [
                {"type": "function", **tool}
                for tool in render_tool_definitions(tools, profile="omit_strict")
            ],
        }
        if voice:
            session["voice"] = voice
        return {"type": "session.update", "session": session}

    def deliver(self, call_id: str, text: str) -> list[JsonObject]:
        """Answer one delegation or Tool call; unknown or answered calls are ignored."""

        kind = self._awaiting.pop(call_id, None)
        if kind is None:
            return []
        output = (
            json.dumps({"result": text}, ensure_ascii=False) if kind == _KIND_DELEGATION else text
        )
        commands = [_call_output(call_id, output)]
        self._added += 1
        commands.extend(self._flush())
        return commands

    def announce(self, text: str) -> list[JsonObject]:
        """Add application context the voice model should speak about.

        The voice model does not attend to system messages added during the
        conversation, so the context arrives as a user text message.
        """

        if not self._started:
            return []
        commands: list[JsonObject] = [
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ]
        self._added += 1
        commands.extend(self._flush())
        return commands

    def audio(self, pcm: bytes) -> list[JsonObject]:
        """Append microphone audio; dropped until the session is configured."""

        if not self._started or self._close_reason is not None or not pcm:
            return []
        return [
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")}
        ]

    def tick(self) -> list[JsonObject]:
        """Expire stale gate state and flush when the gate opened."""

        now = self._clock()
        if (
            self._create_sent_at is not None
            and now >= self._create_sent_at + _CREATE_PENDING_SECONDS
        ):
            self._create_sent_at = None
            self._create_event_id = None
            if self._create_attempts >= _CREATE_ATTEMPTS:
                # The provider ignores the request; the additions stay in context.
                self._spoken = max(self._spoken, self._create_covers)
                self._create_attempts = 0
        active = self._responses.get(self._active) if self._active is not None else None
        if active is not None and now >= active.touched + _RESPONSE_STALL_SECONDS:
            self._fenced.put(self._active or "")
            self._active = None
        return self._flush()

    def next_deadline(self) -> float | None:
        """Return the next time :meth:`tick` can change anything, if any."""

        now = self._clock()
        deadlines: list[float] = []
        if self._create_sent_at is not None:
            deadlines.append(self._create_sent_at + _CREATE_PENDING_SECONDS)
        active = self._responses.get(self._active) if self._active is not None else None
        if active is not None:
            deadlines.append(active.touched + _RESPONSE_STALL_SECONDS)
        if self._added > self._spoken:
            deadlines.append(self._retry_after)
            deadlines.append(self._quiet_at)
        future = [deadline for deadline in deadlines if deadline > now]
        return min(future) if future else None

    def finish(self) -> list[WireEvent]:
        """Return the final events once the socket ended; the last is :class:`WireClosed`."""

        step = _Step()
        for item_id in list(self._assistant_text):
            self._finish_assistant(step, item_id)
        self._finish_user_items(step)
        step.events.append(
            WireClosed(
                reason=self._close_reason,
                usage=dict(self._usage) or None,
                confirmed=self._close_reason is not None,
            )
        )
        return step.events

    # -- provider events --------------------------------------------------

    def receive(self, event: JsonObject) -> _Step:
        """Apply one provider event."""

        step = _Step()
        kind = event.get("type")
        response_id = _text(event.get("response_id"))
        touched = self._responses.get(response_id) if response_id else None
        if touched is not None:
            touched.touched = self._clock()
        if kind == "session.updated":
            if not self._started:
                self._started = True
                step.events.append(
                    WireStarted(expires_at=self._wall_clock() + SESSION_LIFETIME_SECONDS)
                )
        elif kind == "error":
            self._on_error(step, event)
        elif kind == "input_audio_buffer.speech_started":
            self._on_speech_started(step, event)
        elif kind == "input_audio_buffer.speech_stopped":
            self._user_speaking = False
        elif kind in _USER_TRANSCRIPTS:
            self._on_user_transcript(step, event, kind)
        elif kind == "conversation.item.added":
            item = event.get("item")
            if _is_user_speech(item):
                self._finish_user_items(step, keep=_text(item.get("id")))
        elif kind == "response.created":
            self._on_response_created(event)
        elif kind in _AUDIO_DELTAS:
            self._on_audio(step, event, kind)
        elif kind in _AUDIO_DONES:
            item = self._audio_items.get(_text(event.get("item_id")))
            if item is not None:
                item.complete = True
        elif kind in _TRANSCRIPT_DELTAS:
            self._on_transcript_delta(step, event, kind)
        elif kind in _TRANSCRIPT_DONES:
            self._on_transcript_done(step, event)
        elif kind == "response.function_call_arguments.done":
            self._collect_call(response_id, event)
        elif kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._collect_call(response_id, item)
        elif kind == "response.done":
            self._on_response_done(step, event)
        step.commands.extend(self._flush())
        return step

    def _on_error(self, step: _Step, event: JsonObject) -> None:
        error = event.get("error")
        error = error if isinstance(error, dict) else {}
        error_type = _text(error.get("type"))
        code = _text(error.get("code"))
        message = _text(error.get("message"))
        closing = next((kind for kind in _CLOSE_ERROR_TYPES if kind in {error_type, code}), None)
        if closing is not None:
            # The session ended on the provider side; the socket closes next.
            self._close_reason = closing
            step.close = True
            return
        lowered = message.lower()
        if code == "response_cancel_not_active" or "truncat" in lowered:
            return
        if "already has an active response" in lowered:
            # A response vBot did not see yet is running; ask again shortly.
            self._clear_create()
            self._retry_after = self._clock() + _ACTIVE_RESPONSE_RETRY_SECONDS
            return
        step.events.append(WireProblem(code=code or error_type or "unknown", message=message))
        if not self._started:
            # The session configuration was rejected; the call cannot run.
            step.close = True
        elif self._create_event_id is not None and error.get("event_id") == self._create_event_id:
            # The provider refused the response; the additions stay in context.
            self._spoken = max(self._spoken, self._create_covers)
            self._clear_create()

    def _on_speech_started(self, step: _Step, event: JsonObject) -> None:
        self._user_speaking = True
        self._finish_user_items(step, keep=_text(event.get("item_id")))
        step.events.append(WirePlaybackClear())
        now = self._clock()
        for item in self._audio_items.values():
            produced_ms = item.produced_bytes / RELAY_BYTES_PER_MS
            played_ms = min(produced_ms, max(0.0, (now - item.play_start) * 1000))
            if item.complete and played_ms >= produced_ms:
                continue
            end_ms = int(played_ms)
            if end_ms > 0:
                step.commands.append(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item.item_id,
                        "content_index": 0,
                        "audio_end_ms": end_ms,
                    }
                )
            # The caption keeps the text received so far; the provider's
            # truncated transcript is not reliable.
            self._finish_assistant(step, item.item_id)
            if item.response_id:
                self._fence(item.response_id)
        self._audio_items.clear()
        self._playback_end = now
        self._quiet_at = now
        if self._active is not None:
            # Server VAD cancels the running response; late output is stale.
            active = self._responses.get(self._active)
            self._fence(self._active)
            if active is None or not active.has_output:
                # Such a response may never report done; never wait for it.
                self._active = None

    def _on_user_transcript(self, step: _Step, event: JsonObject, kind: str) -> None:
        item_id = _text(event.get("item_id"))
        transcript = event.get("transcript")
        if not item_id or item_id in self._finalized or not isinstance(transcript, str):
            return
        text = transcript.strip()
        if kind.endswith(".completed") and event.get("status") == "completed":
            shown = self._user_open.pop(item_id, None)
            self._finalized.put(item_id)
            if text or shown:
                step.events.append(WireCaption(role="user", text=text, final=True))
            return
        if self._user_open.get(item_id) != text:
            self._user_open[item_id] = text
            step.events.append(WireCaption(role="user", text=text, final=False))

    def _on_response_created(self, event: JsonObject) -> None:
        response = event.get("response")
        response_id = _text(response.get("id")) if isinstance(response, dict) else ""
        if not response_id:
            return
        self._clear_create()
        self._create_attempts = 0
        self._active = response_id
        self._responses[response_id] = _Response(
            covers=self._added,
            touched=self._clock(),
            open_user_items=tuple(self._user_open),
        )
        while len(self._responses) > _REMEMBERED_RESPONSES:
            self._responses.popitem(last=False)

    def _on_audio(self, step: _Step, event: JsonObject, kind: str) -> None:
        response_id = _text(event.get("response_id")) or None
        item_id = _text(event.get("item_id"))
        delta = event.get("delta")
        if not item_id or not isinstance(delta, str) or response_id in self._fenced:
            return
        if not _carrier_matches(self._audio_carrier, item_id, kind):
            return
        try:
            pcm = base64.b64decode(delta, validate=True)
        except (binascii.Error, ValueError):
            return
        if not pcm:
            return
        self._mark_output(response_id)
        now = self._clock()
        start = max(now, self._playback_end)
        self._playback_end = start + len(pcm) / RELAY_BYTES_PER_MS / 1000
        self._quiet_at = self._playback_end + _PLAYBACK_DRAIN_MARGIN_SECONDS
        item = self._audio_items.get(item_id)
        if item is None:
            item = _AudioItem(
                item_id=item_id, response_id=response_id, play_start=start, play_end=start
            )
            self._audio_items[item_id] = item
            self._forget_played_audio(now)
        item.produced_bytes += len(pcm)
        item.play_end = self._playback_end
        step.events.append(WireAudio(item_id=item_id, pcm=pcm))

    def _on_transcript_delta(self, step: _Step, event: JsonObject, kind: str) -> None:
        response_id = _text(event.get("response_id")) or None
        item_id = _text(event.get("item_id"))
        delta = event.get("delta")
        if not item_id or not isinstance(delta, str) or not delta:
            return
        if response_id in self._fenced or item_id in self._finalized:
            return
        if not _carrier_matches(self._caption_carrier, item_id, kind):
            return
        self._mark_output(response_id)
        text = self._assistant_text.get(item_id, "") + delta
        self._assistant_text[item_id] = text
        self._item_response[item_id] = response_id
        step.events.append(WireCaption(role="assistant", text=text.strip(), final=False))

    def _on_transcript_done(self, step: _Step, event: JsonObject) -> None:
        response_id = _text(event.get("response_id")) or None
        item_id = _text(event.get("item_id"))
        if not item_id or response_id in self._fenced or item_id in self._finalized:
            return
        text = event.get("transcript")
        if not isinstance(text, str):
            text = event.get("text")
        self._finish_assistant(step, item_id, text if isinstance(text, str) else None)

    def _collect_call(self, response_id: str, source: JsonObject) -> None:
        call_id = _text(source.get("call_id"))
        if not call_id or call_id in self._handled_calls:
            return
        response = self._responses.get(response_id) if response_id else None
        if response is None:
            return
        response.has_output = True
        response.calls.setdefault(
            call_id,
            _Call(
                call_id=call_id,
                name=_text(source.get("name")),
                arguments=source.get("arguments"),
            ),
        )

    def _on_response_done(self, step: _Step, event: JsonObject) -> None:
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        response_id = _text(response.get("id")) or _text(event.get("response_id"))
        self._add_usage(step, response.get("usage"))
        output = response.get("output")
        for item in output if isinstance(output, list) else []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._collect_call(response_id, item)
        record = self._responses.pop(response_id, None)
        if self._active == response_id:
            self._active = None
        for item_id, owner in list(self._item_response.items()):
            if owner == response_id:
                self._finish_assistant(step, item_id)
        for item in self._audio_items.values():
            if item.response_id == response_id:
                item.complete = True
        if record is None:
            return
        self._finish_user_items(step, only=record.open_user_items)
        if response.get("status") == "completed":
            self._spoken = max(self._spoken, record.covers)
            self._last_completed_calls = frozenset(record.calls)
            for call in record.calls.values():
                self._dispatch(step, call)
            return
        for call in record.calls.values():
            # The call was cut off with its response; it never runs.
            self._handled_calls.put(call.call_id)
            step.commands.append(
                _call_output(
                    call.call_id,
                    live_result_text(
                        live_failure(
                            "interrupted",
                            "The call was interrupted before it started; nothing was done. "
                            "Call it again if the user still wants it.",
                        )
                    ),
                )
            )

    def _dispatch(self, step: _Step, call: _Call) -> None:
        if call.call_id in self._handled_calls:
            return
        self._handled_calls.put(call.call_id)
        arguments = _decoded_arguments(call.arguments)
        if self._direct_tools:
            # The call prepares every name and argument spelling itself.
            self._awaiting[call.call_id] = _KIND_TOOL
            step.events.append(
                WireToolCall(call_id=call.call_id, name=call.name, arguments=arguments)
            )
            return
        if called_tool_name(call.name, {LIVE_TOOL_REQUEST}) == LIVE_TOOL_REQUEST:
            request = arguments.get("request") if isinstance(arguments, dict) else None
            if isinstance(request, str) and request.strip():
                self._awaiting[call.call_id] = _KIND_DELEGATION
                step.events.append(
                    WireDelegation(delegation_id=call.call_id, request=request.strip())
                )
                return
            error = live_failure(
                "invalid_arguments",
                f"request must be the user's request as text. Call {LIVE_TOOL_REQUEST} again "
                'with {"request": "<the user\'s request>"}.',
            )
        else:
            error = live_failure(
                "unknown_tool",
                f'There is no Tool called "{call.name}". Call {LIVE_TOOL_REQUEST} with the '
                "user's request.",
            )
        step.commands.append(_call_output(call.call_id, live_result_text(error)))
        self._added += 1

    # -- helpers ----------------------------------------------------------

    def _flush(self) -> list[JsonObject]:
        now = self._clock()
        if (
            not self._started
            or self._close_reason is not None
            or self._added <= self._spoken
            or self._create_sent_at is not None
            or self._active is not None
            or self._user_speaking
            or self._last_completed_calls & self._awaiting.keys()
            or now < self._retry_after
            or now < self._quiet_at
        ):
            return []
        self._creates += 1
        self._create_attempts += 1
        self._create_sent_at = now
        self._create_event_id = f"vbot_rc_{self._creates}"
        self._create_covers = self._added
        return [{"type": "response.create", "event_id": self._create_event_id}]

    def _clear_create(self) -> None:
        self._create_sent_at = None
        self._create_event_id = None

    def _fence(self, response_id: str) -> None:
        self._fenced.put(response_id)

    def _mark_output(self, response_id: str | None) -> None:
        response = self._responses.get(response_id) if response_id else None
        if response is not None:
            response.has_output = True

    def _forget_played_audio(self, now: float) -> None:
        for item_id, item in list(self._audio_items.items()):
            if len(self._audio_items) > _AUDIO_ITEMS or (item.complete and item.play_end <= now):
                del self._audio_items[item_id]

    def _finish_assistant(self, step: _Step, item_id: str, text: str | None = None) -> None:
        accumulated = self._assistant_text.pop(item_id, None)
        self._item_response.pop(item_id, None)
        if item_id in self._finalized:
            return
        self._finalized.put(item_id)
        final = (text if text is not None else accumulated or "").strip()
        if final or accumulated:
            step.events.append(WireCaption(role="assistant", text=final, final=True))

    def _finish_user_items(
        self, step: _Step, *, keep: str = "", only: tuple[str, ...] | None = None
    ) -> None:
        for item_id in list(self._user_open):
            if item_id == keep or (only is not None and item_id not in only):
                continue
            text = self._user_open.pop(item_id)
            self._finalized.put(item_id)
            step.events.append(WireCaption(role="user", text=text, final=True))

    def _add_usage(self, step: _Step, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        changed = False
        for key, value in usage.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                self._usage[key] = self._usage.get(key, 0) + value
                changed = True
        if changed:
            step.events.append(WireUsage(usage=dict(self._usage)))


def _is_user_speech(item: Any) -> TypeGuard[JsonObject]:
    """Whether an added item is user speech rather than vBot's own text message."""

    if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "user":
        return False
    content = item.get("content")
    parts = content if isinstance(content, list) else []
    return not any(isinstance(part, dict) and part.get("type") == "input_text" for part in parts)


def _call_output(call_id: str, output: str) -> JsonObject:
    return {
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def _carrier_matches(carriers: _Recent, item_id: str, kind: str) -> bool:
    """Accept one of the duplicate event names per item: the first one seen."""

    carrier = carriers.get(item_id)
    if carrier is None:
        carriers.put(item_id, kind)
        return True
    return carrier == kind


def _decoded_arguments(arguments: Any) -> Any:
    """Decode JSON argument text; text that is not JSON stays as it is."""
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return arguments


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
