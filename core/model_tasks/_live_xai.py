"""xAI Grok Voice wire: the realtime WebSocket with server-relayed audio.

xAI offers no WebRTC endpoint, so the server joins
``wss://api.x.ai/v1/realtime?model=<id>`` itself and relays audio: microphone
PCM from the accessor is appended to the input buffer, and assistant audio
returns as :class:`WireAudio` (PCM16 mono 24 kHz both ways). The voice model
gets the voice Agent's Tools as function Tools; every function call goes to the
call, which runs it in the voice Agent's Session (other names and argument
spellings included).

:class:`XaiLiveWire` drives the sans-IO :class:`XaiSession`
(``_live_xai_session.py``) with socket frames, commands, and timer ticks,
sending each step's client events under one lock so they keep their order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException

from core.model_tasks._live_wire import (
    JsonObject,
    WireEvent,
    WireSendError,
    relay_media,
    websocket_url,
)
from core.model_tasks._live_xai_session import Clock, XaiSession
from core.providers._http_shared import classify_http_status
from core.providers.errors import NetworkError, ProviderError
from core.providers.task_client import ProviderTaskClient
from core.providers.token_getter import OAuthRequestRecovery
from core.utils.ids import new_id
from core.utils.tls import shared_ssl_context

REALTIME_PATH = "/realtime"

_OPEN_TIMEOUT_SECONDS = 10.0
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_ERROR_BODY_CHARS = 500

WebSocketConnector = Callable[..., Awaitable[Any]]


async def open_xai_live_wire(
    runtime: Any,
    target_ref: Any,
    *,
    instructions: str,
    voice: str | None,
    tools: list[JsonObject],
    connect: WebSocketConnector | None = None,
    clock: Clock = time.monotonic,
    wall_clock: Clock = time.time,
) -> XaiLiveWire:
    """Join the realtime socket and configure the session.

    *tools* are the function Tools the voice model gets; every call of them
    is handed on as a :class:`WireToolCall`.
    Handshake failures raise Provider errors (HTTP 401/403 as
    :class:`~core.providers.errors.ProviderAuthError`, 429 as a rate limit);
    transport failures raise :class:`~core.providers.errors.NetworkError`.
    """

    client = _XaiLiveClient.from_runtime(runtime, target_ref)
    socket = await client.connect(connect or websocket_connect)
    session = XaiSession(tools=tools, clock=clock, wall_clock=wall_clock)
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
        session: XaiSession,
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

    async def update_playback(
        self, generation: int, played_samples: int, *, enabled: bool, cleared: bool = False
    ) -> list[WireEvent]:
        async with self._lock:
            step = self._session.playback(
                generation, played_samples, enabled=enabled, cleared=cleared
            )
            await self._send(step.commands)
        self._state_changed.set()
        return step.events

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
