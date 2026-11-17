"""Gemini Live wire: the ``BidiGenerateContent`` WebSocket with server-relayed audio.

Gemini Live offers no WebRTC endpoint, so the server joins
``wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.<version>.GenerativeService.BidiGenerateContent``
with the Connection's API key header and relays audio: microphone PCM from the
accessor goes out as ``realtimeInput`` audio, and assistant audio returns as
:class:`WireAudio` (PCM16 mono 24 kHz both ways). The voice model gets the
voice Agent's Tools as function declarations; every function call goes to the
call, which runs it in the voice Agent's Session.

A connection lasts about ten minutes (aistudio.google.com/docs/live-api/
session-management). The server announces its end with ``goAway``; the wire
then opens a new connection with the latest session resumption handle once
the conversation is idle, and swaps it in without the call noticing. A
connection that drops on its own is resumed the same way, reported as
:class:`WireConnection`. Setup errors arrive as WebSocket closes after the
handshake (an invalid key as 1007), which opening classifies with the
Adapter's task status policy.

:class:`GoogleLiveWire` drives the sans-IO :class:`GoogleSession`
(``_live_google_session.py``) with socket frames, commands, and timer ticks,
sending each step's client messages under one lock so they keep their order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException

from core.model_tasks._live_google_session import Clock, GoogleSession
from core.model_tasks._live_wire import (
    JsonObject,
    PlaybackReceipt,
    WireEvent,
    WireSendError,
    relay_media,
    websocket_url,
)
from core.providers.errors import NetworkError, ProviderError
from core.providers.task_client import ProviderTaskClient
from core.utils.errors import VBotError
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.tls import shared_ssl_context
from core.utils.workers import cancel_and_wait, finish_despite_cancel

_LOGGER = get_logger(__name__)

_OPEN_TIMEOUT_SECONDS = 10.0
_SETUP_TIMEOUT_SECONDS = 10.0
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_ERROR_BODY_CHARS = 500
# Resumptions after a dropped connection without any server message between.
_MAX_UNPLANNED_RESUMES = 2
_API_VERSION = re.compile(r"^v\d+\w*$")
_BIDI_PATH = "/ws/google.ai.generativelanguage.{version}.GenerativeService.BidiGenerateContent"

WebSocketConnector = Callable[..., Awaitable[Any]]


async def open_google_live_wire(
    runtime: Any,
    target_ref: Any,
    *,
    instructions: str,
    voice: str | None,
    tools: list[JsonObject],
    thinking_level: str | None = None,
    non_blocking_tools: bool = False,
    connect: WebSocketConnector | None = None,
    clock: Clock = time.monotonic,
) -> GoogleLiveWire:
    """Join a Live session and return once its setup completed.

    *tools* are the function Tools the voice model gets; every call of them
    is handed on as a :class:`WireToolCall`. Handshake failures and setup
    closes raise Provider errors through the Adapter's task status policy (an
    invalid key as :class:`~core.providers.errors.ProviderAuthError`, an
    exhausted quota as a rate limit or a non-retryable error); transport
    failures raise :class:`~core.providers.errors.NetworkError`.
    """

    client = await _GoogleLiveClient.from_runtime_async(runtime, target_ref)
    connector = connect or websocket_connect
    session = GoogleSession(
        model_id=target_ref.model_id,
        instructions=instructions,
        voice=voice,
        tools=tools,
        thinking_level=thinking_level,
        non_blocking_tools=non_blocking_tools,
        clock=clock,
    )
    socket = await client.connect(connector)
    try:
        await client.set_up(socket, session.setup())
    except BaseException:
        with contextlib.suppress(Exception):
            await finish_despite_cancel(socket.close())
        raise
    return GoogleLiveWire(
        call_id=new_id("live"),
        client=client,
        connector=connector,
        socket=socket,
        session=session,
        clock=clock,
    )


class _GoogleLiveClient(ProviderTaskClient):
    """Opens and sets up Live sockets on one resolved Google Connection."""

    async def connect(self, connect: WebSocketConnector) -> Any:
        url = _bidi_url(self._base_url)
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
            self._classify_status(
                response.status_code,
                idempotent=True,
                extra_retryable=None,
                detail=body or str(response.status_code),
                response_headers=httpx.Headers(list(response.headers.raw_items())),
            )
            raise ProviderError(
                f"Gemini Live handshake failed with HTTP {response.status_code}",
                retryable=False,
            ) from exc
        except InvalidURI as exc:
            raise ProviderError(f"Invalid Gemini Live URL: {exc}", retryable=False) from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise NetworkError(f"Gemini Live connection failed: {detail}") from exc

    async def set_up(self, socket: Any, setup: JsonObject) -> None:
        """Send *setup* and wait for ``setupComplete``; nothing else comes first."""

        try:
            await socket.send(json.dumps(setup, ensure_ascii=False))
            async with asyncio.timeout(_SETUP_TIMEOUT_SECONDS):
                while True:
                    message = _decode(await socket.recv())
                    if message is not None and "setupComplete" in message:
                        return
        except ConnectionClosed as exc:
            code, reason = _close_of(socket, exc)
            raise self._close_error(code, reason) from exc
        except TimeoutError as exc:
            raise NetworkError("Gemini Live did not complete the session setup in time") from exc

    def _close_error(self, code: int | None, reason: str) -> Exception:
        detail = reason or f"WebSocket close {code}"
        status = _close_status(code, reason)
        if status is None:
            return NetworkError(f"Gemini Live closed the connection during setup: {detail}")
        try:
            self._classify_status(
                status,
                idempotent=True,
                extra_retryable=None,
                detail=detail,
                response_headers=httpx.Headers(),
            )
        except VBotError as exc:
            return exc
        return ProviderError(f"Gemini Live rejected the session setup: {detail}", retryable=False)


class GoogleLiveWire:
    """One Gemini Live session relaying audio for a Live call."""

    def __init__(
        self,
        *,
        call_id: str,
        client: _GoogleLiveClient,
        connector: WebSocketConnector,
        socket: Any,
        session: GoogleSession,
        clock: Clock = time.monotonic,
    ) -> None:
        self._call_id = call_id
        self._client = client
        self._connector = connector
        self._socket = socket
        self._session = session
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state_changed = asyncio.Event()
        self._pending: list[WireEvent] = list(session.start())
        self._resumer: asyncio.Task[None] | None = None
        self._closing = False

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
        timer = asyncio.create_task(self._run_timer(), name=f"live-google-timer:{self._call_id}")
        socket = self._socket
        code: int | None = None
        reason = ""
        unplanned = 0
        try:
            while True:
                for event in self._take_pending():
                    yield event
                try:
                    async for frame in socket:
                        message = _decode(frame)
                        if message is None:
                            continue
                        unplanned = 0
                        async with self._lock:
                            step = self._session.receive(message)
                            with contextlib.suppress(WireSendError):
                                await self._send(step.commands)
                        self._state_changed.set()
                        if step.resume:
                            self._start_resume()
                        for event in step.events:
                            yield event
                    code, reason = _close_of(socket)
                except ConnectionClosed as exc:
                    code, reason = _close_of(socket, exc)
                if self._resumer is not None:
                    # A planned resume may still be swapping connections.
                    await asyncio.shield(self._resumer)
                for event in self._take_pending():
                    yield event
                if self._socket is not socket:
                    socket, code, reason = self._socket, None, ""
                    continue
                if (
                    self._closing
                    or unplanned >= _MAX_UNPLANNED_RESUMES
                    or not self._session.resumes_after(code)
                ):
                    break
                unplanned += 1
                async with self._lock:
                    detached = self._session.detach()
                for event in detached:
                    yield event
                if not await self._resume():
                    break
                socket, code, reason = self._socket, None, ""
        finally:
            await cancel_and_wait(timer)
            if self._resumer is not None:
                await cancel_and_wait(self._resumer)
        for event in self._take_pending():
            yield event
        for event in self._session.finish(code=code, reason=reason):
            yield event

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

    async def update_playback(self, receipt: PlaybackReceipt) -> list[WireEvent]:
        async with self._lock:
            step = self._session.playback(receipt)
            await self._send(step.commands)
        self._state_changed.set()
        if step.resume:
            self._start_resume()
        return step.events

    async def request_close(self) -> None:
        """Gemini Live has no session close message; closing the socket ends the session."""
        self._closing = True
        await self.aclose()

    async def aclose(self) -> None:
        self._closing = True
        with contextlib.suppress(Exception):
            await self._socket.close()

    async def _send(self, commands: list[JsonObject]) -> None:
        try:
            for command in commands:
                await self._socket.send(json.dumps(command, ensure_ascii=False))
        except ConnectionClosed as exc:
            raise WireSendError("Gemini Live connection is closed") from exc

    def _take_pending(self) -> list[WireEvent]:
        pending, self._pending = self._pending, []
        return pending

    def _start_resume(self) -> None:
        if self._closing or (self._resumer is not None and not self._resumer.done()):
            return
        self._resumer = asyncio.create_task(
            self._resume_planned(), name=f"live-google-resume:{self._call_id}"
        )

    async def _resume_planned(self) -> None:
        if not await self._resume():
            # The current connection serves until the server drops it.
            async with self._lock:
                self._session.resume_failed()

    async def _resume(self) -> bool:
        """Open a connection that continues the session and swap it in."""

        try:
            socket = await self._client.connect(self._connector)
        except Exception as exc:
            _LOGGER.warning(
                "Gemini Live could not reconnect (call=%s error_type=%s)",
                self._call_id,
                type(exc).__name__,
            )
            return False
        try:
            await self._client.set_up(socket, self._session.setup())
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await finish_despite_cancel(socket.close())
            if not isinstance(exc, Exception):
                raise
            _LOGGER.warning(
                "Gemini Live could not resume the session (call=%s error_type=%s)",
                self._call_id,
                type(exc).__name__,
            )
            return False
        async with self._lock:
            if self._closing:
                old = socket
            else:
                old, self._socket = self._socket, socket
                step = self._session.attach()
                with contextlib.suppress(WireSendError):
                    await self._send(step.commands)
                self._pending.extend(step.events)
        self._state_changed.set()
        with contextlib.suppress(Exception):
            await old.close()
        return old is not socket

    async def _run_timer(self) -> None:
        """Re-check the gate and the resume deadline when one of them passes."""

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
                step = self._session.tick()
                try:
                    await self._send(step.commands)
                except WireSendError:
                    return
            if step.resume:
                self._start_resume()


def _bidi_url(base_url: str) -> str:
    """Return the Live socket URL for a native Gemini API base such as ``.../v1beta``."""

    parts = urlsplit(base_url.rstrip("/"))
    last = parts.path.rsplit("/", 1)[-1]
    version = last if _API_VERSION.match(last) else "v1beta"
    return websocket_url(f"{parts.scheme}://{parts.netloc}", _BIDI_PATH.format(version=version))


def _close_status(code: int | None, reason: str) -> int | None:
    """The HTTP status a setup close stands for; ``None`` when the connection just ended.

    Live closes carry the gRPC status of the failure: invalid argument (an
    invalid key included) as 1007, not found or denied as 1008, internal or
    exhausted as 1011.
    """

    lowered = reason.lower()
    if "quota" in lowered or "exhausted" in lowered or "rate limit" in lowered:
        return 429
    if code == 1007:
        return 400
    if code == 1008:
        return 404 if "not found" in lowered or "not supported" in lowered else 403
    if code == 1011:
        return 500
    if code in {1012, 1013}:
        return 503
    return None


def _close_of(socket: Any, error: ConnectionClosed | None = None) -> tuple[int | None, str]:
    received = error.rcvd if error is not None else None
    if received is not None:
        return received.code, received.reason or ""
    code = getattr(socket, "close_code", None)
    reason = getattr(socket, "close_reason", None)
    return (code if isinstance(code, int) else None), (reason if isinstance(reason, str) else "")


def _body_text(body: bytes | bytearray | None) -> str:
    if not body:
        return ""
    return body.decode("utf-8", errors="replace").strip()[:_ERROR_BODY_CHARS]


def _decode(frame: Any) -> JsonObject | None:
    """Decode a server message; Gemini sends JSON in binary frames."""
    if isinstance(frame, bytes | bytearray):
        try:
            frame = frame.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(frame, str):
        return None
    try:
        message = json.loads(frame)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None
