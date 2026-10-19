"""Responses over WebSocket: one request's exchange and the sockets that outlive it.

The ChatGPT Codex backend (``/codex/responses``) and the OpenAI Platform
(``/v1/responses``, its documented WebSocket mode) speak the same protocol.

A request that extends the previous request and its response unchanged
continues it on the same socket with ``previous_response_id`` and only the new
input items. ``store`` is false, so that continuation lives on the connection,
not on the server. A socket remembers the request it continues by its
:class:`ResponsesRequestDigest`, never the request itself, so an idle socket does
not hold a Session's screenshots. :class:`ResponsesTransports` is the Runtime-wide owner that keeps
a Session's socket and its continuation open between Runs (each Run has its own
Adapter), bounds how many stay open and for how long, and remembers per
Connection whether the WebSocket or request compression currently works.
:class:`ResponsesWebSocket` streams one Adapter's requests over those sockets.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import time
import zlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from websockets.exceptions import ProtocolError
from websockets.extensions.base import Extension
from websockets.extensions.permessage_deflate import (
    ClientPerMessageDeflateFactory,
    PerMessageDeflate,
)
from websockets.frames import CONT, CTRL_OPCODES, Frame
from websockets.protocol import State
from websockets.typing import ExtensionParameter

from core.providers._openai_constants import (
    _RESPONSES_WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
    _RESPONSES_WEBSOCKET_STATUS_CODE,
    ResponsesWebSocketConnector,
    ResponsesWebSocketRoute,
)
from core.providers.errors import (
    NetworkError,
    ProviderError,
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    normalize_responses_stream_event,
)
from core.providers.http import _REQUEST_BODY_WORKERS
from core.utils.logging import get_logger
from core.utils.tls import shared_ssl_context

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

_LOGGER = get_logger("providers.responses_websocket")

RESPONSES_SOCKET_IDLE_SECONDS = 15 * 60
"""An idle socket closes after this long. A Run's next step follows its Tool
calls within seconds to minutes and a user's next turn usually within minutes;
an hour-old continuation would expire anyway (``RESPONSES_SOCKET_MAX_AGE_SECONDS``)."""

RESPONSES_MAX_OPEN_SOCKETS = 16
"""Sockets kept open across all Connections. Idle sockets beyond it close, least
recently used first; a request is never refused, so in-use sockets of
concurrent Runs may briefly exceed it."""

RESPONSES_MAX_IDLE_SOCKETS_PER_CONVERSATION = 2
"""Idle sockets kept per Session: its Run's and one for a side request (Session
title, Extension sampling) with other instructions or Tools. The one whose
continuation saves the fewest input items closes first."""

RESPONSES_SOCKET_MAX_AGE_SECONDS = 55 * 60
"""The backend ends a connection after 60 minutes
(``websocket_connection_limit_reached``); an older socket is never reused."""

RESPONSES_WEBSOCKET_FAILURE_LIMIT = 2
"""Consecutive WebSocket transport failures on one Connection before its
requests use SSE for ``RESPONSES_WEBSOCKET_RETRY_AFTER_SECONDS``."""

RESPONSES_WEBSOCKET_RETRY_AFTER_SECONDS = 10 * 60
"""How long a Connection whose WebSocket failed (or answered HTTP 426) uses SSE."""

DEFLATE_LEVEL = 2
"""permessage-deflate level. Base64 screenshots, the bulk of a large Session,
compress as well as at zlib's default level 6 in about 60 % of the time
(53 MB: 0.58 s against 0.99 s, both to 75 %); text compresses to 22 % instead
of 16 %."""

_FRESH_SOCKET_CODES = frozenset(
    {"previous_response_not_found", "websocket_connection_limit_reached"}
)
_NOT_CONTINUED_FIELDS = frozenset({"input", "previous_response_id"})
_AUTH_HANDSHAKE_STATUSES = frozenset({401, 403})
_UPGRADE_REQUIRED_STATUS = 426
_EMPTY_UNCOMPRESSED_BLOCK = b"\x00\x00\xff\xff"

type ResponsesConnectionKey = tuple[str, str]
"""``(provider id, local Connection id)``: the unit of WebSocket availability,
request compression and credential-change closing."""

type _SocketKey = tuple[ResponsesConnectionKey, str]
"""A Connection and a Session's conversation id."""


def responses_conversation_id(agent_id: str, session_id: str) -> str:
    """The id that scopes a Session's Responses sockets and continuations."""
    return f"{agent_id}:{session_id}"


@dataclass(frozen=True, slots=True)
class ResponsesRequestDigest:
    """What a continuation compares of one Responses request.

    ``fields`` digests every request field but ``input`` and
    ``previous_response_id``; ``items`` holds one digest per input item, or is
    ``None`` when the input is not a list. Two values share a digest only when
    they are the same JSON value: any change at any depth changes it, strings,
    numbers, booleans and ``null`` keep their type, and only an object's key
    order does not count, as in a dict comparison.
    """

    fields: bytes
    items: tuple[bytes, ...] | None


def responses_request_digest(payload: Mapping[str, Any]) -> ResponsesRequestDigest:
    """Digest one request in its preparation hop, off the Event Loop.

    Every item is hashed once per request, however often the request is sent;
    a 55 MB request with 100 screenshots takes about 27 ms.
    """
    items = payload.get("input")
    return ResponsesRequestDigest(
        fields=_json_digest(
            {key: value for key, value in payload.items() if key not in _NOT_CONTINUED_FIELDS}
        ),
        items=tuple(_json_digest(item) for item in items) if isinstance(items, list) else None,
    )


def _json_digest(value: Any) -> bytes:
    digest = hashlib.sha256()
    _feed_json(digest.update, value)
    return digest.digest()


def _feed_json(feed: Callable[[bytes], object], value: Any) -> None:
    """Feed one JSON value, every part tagged and strings length-prefixed.

    No two JSON values produce the same stream. Strings go in as their UTF-8
    bytes without escaping, the bulk of which SHA-256 hashes outside the GIL.
    """
    if isinstance(value, str):
        data = value.encode("utf-8", "surrogatepass")
        feed(b"s%d:" % len(data))
        feed(data)
    elif value is None:
        feed(b"n")
    elif value is True:
        feed(b"t")
    elif value is False:
        feed(b"f")
    elif isinstance(value, int):
        feed(b"i%d;" % value)
    elif isinstance(value, float):
        feed(b"d%s;" % float.__repr__(value).encode("ascii"))
    elif isinstance(value, dict):
        feed(b"{%d:" % len(value))
        for key in sorted(value, key=str):
            _feed_json(feed, key)
            _feed_json(feed, value[key])
    elif isinstance(value, list | tuple):
        feed(b"[%d:" % len(value))
        for element in value:
            _feed_json(feed, element)
    else:
        raise TypeError(f"Responses request value of type {type(value).__name__} is not JSON")


@dataclass(frozen=True)
class _ResponsesContinuation:
    """What the next request on a socket may chain to with ``previous_response_id``.

    It keeps the previous request's digests, never its items: ``fields`` and
    ``input_items`` come from that request's :class:`ResponsesRequestDigest`.
    ``response_items`` are the Provider's own final output items, few and
    small, which the next request must replay unchanged.
    """

    fields: bytes
    input_items: tuple[bytes, ...]
    response_id: str
    # Original final Provider output, never normalized or rendered for replay.
    response_items: list[dict[str, Any]]

    def saved_items(self) -> int:
        """How many input items a continuation of this one need not resend."""
        return len(self.input_items) + len(self.response_items)


@dataclass(eq=False)
class _PooledSocket:
    """One open socket of one Session on one route, with its continuation."""

    key: _SocketKey
    route: ResponsesWebSocketRoute
    handshake: str
    websocket: Any
    deflate_window_bits: int | None
    opened_at: float
    idle_since: float = 0.0
    continuation: _ResponsesContinuation | None = None
    retired: bool = False
    idle_timer: asyncio.TimerHandle | None = None

    def is_open(self) -> bool:
        return getattr(self.websocket, "state", State.OPEN) is State.OPEN

    def saved_items(self) -> int:
        return self.continuation.saved_items() if self.continuation is not None else 0


class ResponsesTransports:
    """Runtime-wide Responses transport state that outlives one Run's Adapter.

    Sockets are checked out per request, so two concurrent requests never
    share one; a completed exchange returns its socket with the continuation
    it established. An idle socket closes after ``idle_seconds``; at most
    ``max_sockets`` stay open (``RESPONSES_MAX_OPEN_SOCKETS``). Per Connection it
    remembers consecutive WebSocket failures (after
    ``RESPONSES_WEBSOCKET_FAILURE_LIMIT`` the Connection uses SSE for
    ``RESPONSES_WEBSOCKET_RETRY_AFTER_SECONDS``) and whether the backend rejected
    a compressed request body. Correctness never depends on the explicit
    closing calls: a socket is reused only on its route and handshake.
    """

    def __init__(
        self,
        *,
        idle_seconds: float = RESPONSES_SOCKET_IDLE_SECONDS,
        max_sockets: int = RESPONSES_MAX_OPEN_SOCKETS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_seconds = idle_seconds
        self._max_sockets = max_sockets
        self._clock = clock
        self._idle: dict[_SocketKey, list[_PooledSocket]] = {}
        self._in_use: set[_PooledSocket] = set()
        self._closing: set[asyncio.Task[None]] = set()
        self._failures: dict[ResponsesConnectionKey, int] = {}
        self._websocket_retry_at: dict[ResponsesConnectionKey, float] = {}
        self._uncompressed: set[ResponsesConnectionKey] = set()

    # -- WebSocket availability and request compression ------------------------

    def websocket_available(self, connection: ResponsesConnectionKey) -> bool:
        """Whether ``connection``'s requests try the WebSocket (not SSE) now."""
        retry_at = self._websocket_retry_at.get(connection)
        if retry_at is None:
            return True
        if self._clock() < retry_at:
            return False
        del self._websocket_retry_at[connection]
        return True

    def record_websocket_failure(self, connection: ResponsesConnectionKey) -> bool:
        """Count one transport failure; return whether the Connection now uses SSE."""
        failures = self._failures.get(connection, 0) + 1
        if failures < RESPONSES_WEBSOCKET_FAILURE_LIMIT:
            self._failures[connection] = failures
            return False
        self.mark_websocket_unavailable(connection)
        return True

    def mark_websocket_unavailable(self, connection: ResponsesConnectionKey) -> None:
        """Send ``connection``'s requests over SSE for the retry window."""
        self._failures.pop(connection, None)
        self._websocket_retry_at[connection] = (
            self._clock() + RESPONSES_WEBSOCKET_RETRY_AFTER_SECONDS
        )
        _LOGGER.warning(
            "Responses WebSocket unavailable on %s:%s; using SSE for %d minutes",
            *connection,
            RESPONSES_WEBSOCKET_RETRY_AFTER_SECONDS // 60,
        )

    def record_websocket_success(self, connection: ResponsesConnectionKey) -> None:
        """A completed exchange ends a run of consecutive failures."""
        self._failures.pop(connection, None)

    def compresses_requests(self, connection: ResponsesConnectionKey) -> bool:
        """Whether ``connection``'s HTTP request bodies are sent zstd-compressed."""
        return connection not in self._uncompressed

    def stop_compressing_requests(self, connection: ResponsesConnectionKey) -> None:
        """Send ``connection``'s request bodies uncompressed from now on."""
        self._uncompressed.add(connection)

    # -- Sockets ----------------------------------------------------------------

    def take(
        self,
        key: _SocketKey,
        *,
        route: ResponsesWebSocketRoute,
        handshake: str,
        digest: ResponsesRequestDigest,
    ) -> _PooledSocket | None:
        """Check out an idle socket for the request ``digest`` describes, or ``None``.

        Only a socket on the same route and handshake qualifies: preferably one
        whose continuation has the request's instructions, Tools and other
        request fields, otherwise one without a continuation. A socket with
        another request kind's continuation stays for that kind. A closed,
        idle-expired or too old socket, or one on this route whose handshake
        changed (refreshed credentials), closes.
        """

        idle = self._idle.get(key)
        if not idle:
            return None
        now = self._clock()
        usable: list[_PooledSocket] = []
        for pooled in list(idle):
            same_route = pooled.route == route
            if (
                not pooled.is_open()
                or now - pooled.opened_at >= RESPONSES_SOCKET_MAX_AGE_SECONDS
                or now - pooled.idle_since >= self._idle_seconds
                or (same_route and pooled.handshake != handshake)
            ):
                self._retire(pooled)
            elif same_route:
                usable.append(pooled)
        chosen = next(
            (
                pooled
                for pooled in reversed(usable)
                if pooled.continuation is not None and pooled.continuation.fields == digest.fields
            ),
            None,
        ) or next((pooled for pooled in reversed(usable) if pooled.continuation is None), None)
        if chosen is None:
            return None
        self._unidle(chosen)
        self._in_use.add(chosen)
        return chosen

    async def open(
        self,
        key: _SocketKey,
        *,
        route: ResponsesWebSocketRoute,
        handshake: str,
        connect: Callable[[], Awaitable[Any]],
    ) -> _PooledSocket:
        """Open and check out a new socket, closing idle ones over the limit first."""
        self._evict_idle(self._max_sockets - 1)
        websocket = await connect()
        pooled = _PooledSocket(
            key=key,
            route=route,
            handshake=handshake,
            websocket=websocket,
            deflate_window_bits=_negotiated_deflate_window_bits(websocket),
            opened_at=self._clock(),
        )
        self._in_use.add(pooled)
        return pooled

    async def release(self, pooled: _PooledSocket, *, reusable: bool) -> None:
        """Return a checked-out socket after a completed exchange, or close it."""
        self._in_use.discard(pooled)
        if not reusable or pooled.retired or not pooled.is_open():
            pooled.continuation = None
            await _close_websocket(pooled.websocket)
            return
        pooled.idle_since = self._clock()
        sockets = self._idle.setdefault(pooled.key, [])
        sockets.append(pooled)
        pooled.idle_timer = asyncio.get_running_loop().call_later(
            self._idle_seconds, self._expire, pooled
        )
        if len(sockets) > RESPONSES_MAX_IDLE_SOCKETS_PER_CONVERSATION:
            self._retire(min(sockets, key=lambda idle: (idle.saved_items(), idle.idle_since)))
        self._evict_idle(self._max_sockets)

    # -- Closing ----------------------------------------------------------------

    async def close_conversations(self, conversation_ids: Collection[str]) -> None:
        """Close the sockets of these Sessions (archived or deleted)."""
        await self._close_matching(lambda key: key[1] in conversation_ids)

    async def close_connection(self, provider_id: str, connection_id: str | None = None) -> None:
        """Close a Connection's sockets (all of a Provider's without ``connection_id``).

        Called when its credentials change; it also gets a fresh WebSocket
        attempt.
        """

        def matches(connection: ResponsesConnectionKey) -> bool:
            return connection[0] == provider_id and connection_id in {None, connection[1]}

        for connection in [*self._failures, *self._websocket_retry_at]:
            if matches(connection):
                self._failures.pop(connection, None)
                self._websocket_retry_at.pop(connection, None)
        await self._close_matching(lambda key: matches(key[0]))

    async def aclose(self) -> None:
        """Close every socket; sockets in use close when their request ends."""
        await self._close_matching(lambda _key: True)

    def abort(self) -> None:
        """Drop every socket without a closing handshake (synchronous shutdown)."""
        sockets = [*self._in_use, *(pooled for idle in self._idle.values() for pooled in idle)]
        self._idle.clear()
        for pooled in sockets:
            pooled.retired = True
            pooled.continuation = None
            if pooled.idle_timer is not None:
                pooled.idle_timer.cancel()
                pooled.idle_timer = None
            _abort_websocket(pooled.websocket)

    async def _close_matching(self, matches: Callable[[_SocketKey], bool]) -> None:
        for pooled in self._in_use:
            if matches(pooled.key):
                pooled.retired = True
        for pooled in [pooled for idle in self._idle.values() for pooled in idle]:
            if matches(pooled.key):
                self._retire(pooled)
        pending = {task for task in self._closing if not task.done()}
        if pending:
            await asyncio.wait(pending)

    def _evict_idle(self, limit: int) -> None:
        idle = sorted(
            (pooled for sockets in self._idle.values() for pooled in sockets),
            key=lambda pooled: pooled.idle_since,
        )
        excess = len(self._in_use) + len(idle) - limit
        for pooled in idle[: max(excess, 0)]:
            self._retire(pooled)

    def _expire(self, pooled: _PooledSocket) -> None:
        pooled.idle_timer = None
        if pooled in self._idle.get(pooled.key, ()):
            self._retire(pooled)

    def _retire(self, pooled: _PooledSocket) -> None:
        self._unidle(pooled)
        pooled.retired = True
        pooled.continuation = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _abort_websocket(pooled.websocket)
            return
        task = loop.create_task(_close_websocket(pooled.websocket))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _unidle(self, pooled: _PooledSocket) -> None:
        sockets = self._idle.get(pooled.key)
        if sockets is not None and pooled in sockets:
            sockets.remove(pooled)
            if not sockets:
                del self._idle[pooled.key]
        if pooled.idle_timer is not None:
            pooled.idle_timer.cancel()
            pooled.idle_timer = None


async def _close_websocket(websocket: Any) -> None:
    try:
        close_result = websocket.close()
        if inspect.isawaitable(close_result):
            await close_result
    except Exception:
        pass


def _abort_websocket(websocket: Any) -> None:
    transport = getattr(websocket, "transport", None)
    abort = getattr(transport, "abort", None)
    if callable(abort):
        with suppress(Exception):
            abort()


# ---------------------------------------------------------------------------
# permessage-deflate off the Event Loop
# ---------------------------------------------------------------------------


class _DeflatedText(bytes):
    """A text message's UTF-8 payload, already deflated for the negotiated window."""


class _PrecompressedDeflate(PerMessageDeflate):
    """permessage-deflate whose outgoing messages arrive compressed.

    websockets compresses in ``send()``, on the Event Loop; a full Session
    request would block it for about a second. Requests are deflated on the
    request-body worker instead (:func:`_deflate_message`) and pass here
    unchanged with RSV1 set. Every message is compressed on its own, which a
    receiver that allows context takeover decodes all the same; anything not
    precompressed is compressed here the same way. Incoming messages decode
    as usual.
    """

    @override
    def encode(self, frame: Frame) -> Frame:
        if frame.opcode in CTRL_OPCODES:
            return frame
        if frame.opcode is CONT or not frame.fin:
            raise ProtocolError("Responses WebSocket messages are never fragmented")
        data = (
            frame.data
            if isinstance(frame.data, _DeflatedText)
            else _deflate_message(bytes(frame.data), self.local_max_window_bits)
        )
        return Frame(frame.opcode, data, frame.fin, True, frame.rsv2, frame.rsv3)


class _PrecompressingDeflateFactory(ClientPerMessageDeflateFactory):
    """Offers permessage-deflate like websockets' default.

    The negotiated extension is a :class:`_PrecompressedDeflate`.
    """

    @override
    def process_response_params(
        self,
        params: Sequence[ExtensionParameter],
        accepted_extensions: Sequence[Extension],
    ) -> PerMessageDeflate:
        negotiated = super().process_response_params(params, accepted_extensions)
        return _PrecompressedDeflate(
            negotiated.remote_no_context_takeover,
            negotiated.local_no_context_takeover,
            negotiated.remote_max_window_bits,
            negotiated.local_max_window_bits,
        )


def _deflate_message(data: bytes, window_bits: int) -> _DeflatedText:
    """One complete message deflated on its own, as permessage-deflate frames it."""
    compressor = zlib.compressobj(DEFLATE_LEVEL, zlib.DEFLATED, -window_bits)
    compressed = compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)
    return _DeflatedText(compressed.removesuffix(_EMPTY_UNCOMPRESSED_BLOCK))


def _negotiated_deflate_window_bits(websocket: Any) -> int | None:
    extensions = getattr(getattr(websocket, "protocol", None), "extensions", None) or ()
    for extension in extensions:
        if isinstance(extension, _PrecompressedDeflate):
            return extension.local_max_window_bits
    return None


# ---------------------------------------------------------------------------
# One request
# ---------------------------------------------------------------------------


class _FreshSocketRequiredError(Exception):
    """The socket's continuation or connection ended before any model output."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ResponsesWebSocketTransportError(NetworkError):
    """Responses WebSocket failed before or after receiving a provider event.

    ``handshake_status`` is the HTTP status that refused the upgrade, if any.
    """

    def __init__(
        self, message: str, *, events_received: bool, handshake_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.events_received = events_received
        self.handshake_status = handshake_status


@dataclass(frozen=True, slots=True)
class _ResponsesMessage:
    """One rendered ``response.create``: the request, its UTF-8 text and the bytes sent."""

    request: dict[str, Any]
    text: bytes
    data: bytes


def _websocket_response_head(websocket: Any) -> tuple[int, dict[str, str]]:
    response = getattr(websocket, "response", None)
    raw_status = getattr(response, "status_code", _RESPONSES_WEBSOCKET_STATUS_CODE)
    status_code = (
        raw_status
        if isinstance(raw_status, int) and not isinstance(raw_status, bool)
        else _RESPONSES_WEBSOCKET_STATUS_CODE
    )
    raw_headers = getattr(response, "headers", None)
    # websockets' Headers refuses dict() for a repeated name (chatgpt.com sends
    # several Set-Cookie headers), so read every value and join repeats.
    raw_items = getattr(raw_headers, "raw_items", None)
    try:
        items = list(raw_items() if callable(raw_items) else dict(raw_headers or {}).items())
    except TypeError, ValueError:
        items = []
    headers: dict[str, str] = {}
    for name, value in items:
        key = str(name)
        headers[key] = f"{headers[key]}, {value}" if key in headers else str(value)
    return status_code, headers


def _handshake_status(error: BaseException) -> int | None:
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _responses_error_code(event: Mapping[str, Any]) -> str | None:
    response = event.get("response")
    payload = response if isinstance(response, Mapping) else event
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    code = payload.get("code")
    return code if isinstance(code, str) and code else None


def _render_websocket_message(
    payload: dict[str, Any],
    digest: ResponsesRequestDigest,
    continuation: _ResponsesContinuation | None,
    deflate_window_bits: int | None,
) -> _ResponsesMessage:
    """Chain ``payload`` to ``continuation``, encode it and deflate it if negotiated.

    The request is ``payload`` itself unless it extends ``continuation``'s
    request and output unchanged; then it carries only the new input items
    after ``previous_response_id``. The encoding and the compression grow with
    the Session, so this runs on the request-body worker.
    """

    request = _continued_request(payload, digest, continuation)
    text = json.dumps(
        {"type": "response.create", **request}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    data = text if deflate_window_bits is None else _deflate_message(text, deflate_window_bits)
    return _ResponsesMessage(request, text, data)


def _continued_request(
    payload: dict[str, Any],
    digest: ResponsesRequestDigest,
    continuation: _ResponsesContinuation | None,
) -> dict[str, Any]:
    """``payload`` after ``previous_response_id``, if it extends ``continuation`` unchanged.

    Its fields and leading input items must match the previous request's
    digests, and the items after them the previous response's output.
    """
    if continuation is None or digest.items is None or digest.fields != continuation.fields:
        return payload
    previous = len(continuation.input_items)
    baseline = previous + len(continuation.response_items)
    current_input = payload["input"]
    if (
        len(digest.items) < baseline
        or digest.items[:previous] != continuation.input_items
        or current_input[previous:baseline] != continuation.response_items
    ):
        return payload
    return {
        **payload,
        "previous_response_id": continuation.response_id,
        "input": current_input[baseline:],
    }


def _completed_continuation(
    digest: ResponsesRequestDigest,
    state: ResponsesStreamState,
    output_items: dict[int | None, dict[str, Any] | None],
) -> _ResponsesContinuation | None:
    """The continuation a completed exchange establishes, if its output is proven."""

    completed_response = state.completed_response
    if digest.items is None or not isinstance(completed_response, Mapping):
        return None
    response_id = completed_response.get("id")
    response_items = completed_response.get("output")
    if not response_items:
        indices = sorted(index for index in output_items if index is not None)
        if (
            not output_items
            or any(item is None for item in output_items.values())
            or indices != list(range(len(output_items)))
        ):
            return None
        response_items = [output_items[index] for index in indices]
    if (
        not isinstance(response_id, str)
        or not response_id
        or not isinstance(response_items, list)
        or not all(isinstance(item, dict) for item in response_items)
    ):
        return None
    return _ResponsesContinuation(
        fields=digest.fields,
        input_items=digest.items,
        response_id=response_id,
        response_items=copy.deepcopy(response_items),
    )


def _handshake_fingerprint(url: str, headers: Mapping[str, str]) -> str:
    """Identify a socket's handshake; changed credentials or headers need a new socket."""
    digest = hashlib.sha256(url.encode("utf-8"))
    for name, value in sorted((name.lower(), value) for name, value in headers.items()):
        digest.update(b"\0" + name.encode("utf-8") + b"\0" + value.encode("utf-8"))
    return digest.hexdigest()


class ResponsesWebSocket:
    """Stream one Adapter's Responses requests over its Connection's pooled sockets."""

    def __init__(
        self,
        *,
        base_url: str,
        endpoint_path: str,
        connect: ResponsesWebSocketConnector,
        debug_recorder: ProviderDebugRecorder | None,
        transports: ResponsesTransports,
        connection: ResponsesConnectionKey,
    ) -> None:
        self._base_url = base_url
        self._endpoint_path = endpoint_path
        self._websocket_connect = connect
        self._debug_recorder = debug_recorder
        self._transports = transports
        self._connection = connection

    async def stream(
        self,
        payload: dict[str, Any],
        *,
        digest: ResponsesRequestDigest,
        headers: dict[str, str],
        route: ResponsesWebSocketRoute,
        state: ResponsesStreamState,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream ``payload``'s response, continuing the Session's socket where it can.

        ``digest`` is ``payload``'s :func:`responses_request_digest`. Before any
        Provider event, a lost continuation, an ended connection or
        a transport failure retries once in full on a fresh socket. A
        :class:`_ResponsesWebSocketTransportError` without events means the caller
        may send the same request another way (SSE): the Connection failed
        repeatedly, answered HTTP 426, or refused the credentials.
        """

        url = self._websocket_url()
        key = (self._connection, route[0])
        handshake = _handshake_fingerprint(url, headers)
        fresh = False
        # Only final Provider snapshots can prove what previous_response_id
        # restores. The decoder also retains incomplete/modified snapshots.
        output_items: dict[int | None, dict[str, Any] | None] = {}
        while True:
            try:
                async with aclosing(
                    self._exchange(
                        payload,
                        digest=digest,
                        url=url,
                        key=key,
                        headers=headers,
                        route=route,
                        handshake=handshake,
                        fresh=fresh,
                        state=state,
                        output_items=output_items,
                    )
                ) as deltas:
                    async for delta in deltas:
                        yield delta
            except _FreshSocketRequiredError as missing:
                if fresh:
                    raise ProviderError(
                        f"Responses WebSocket failed on a fresh connection: {missing.code}",
                        retryable=True,
                    ) from None
            except _ResponsesWebSocketTransportError as error:
                if error.handshake_status == _UPGRADE_REQUIRED_STATUS:
                    self._transports.mark_websocket_unavailable(self._connection)
                    raise
                if error.handshake_status in _AUTH_HANDSHAKE_STATUSES:
                    # The SSE request owns credential recovery.
                    raise
                unavailable = self._transports.record_websocket_failure(self._connection)
                if error.events_received or unavailable or fresh:
                    raise
            else:
                self._transports.record_websocket_success(self._connection)
                return
            fresh = True
            output_items.clear()

    async def _exchange(
        self,
        payload: dict[str, Any],
        *,
        digest: ResponsesRequestDigest,
        url: str,
        key: _SocketKey,
        headers: dict[str, str],
        route: ResponsesWebSocketRoute,
        handshake: str,
        fresh: bool,
        state: ResponsesStreamState,
        output_items: dict[int | None, dict[str, Any] | None],
    ) -> AsyncGenerator[dict[str, Any]]:
        pooled = (
            None
            if fresh
            else self._transports.take(key, route=route, handshake=handshake, digest=digest)
        )
        if pooled is None:
            pooled = await self._open(url, key, headers=headers, route=route, handshake=handshake)
        capture = None
        events_received = False
        model_delta_received = False
        completed = False
        try:
            message = await _REQUEST_BODY_WORKERS.run(
                _render_websocket_message,
                payload,
                digest,
                pooled.continuation,
                pooled.deflate_window_bits,
            )
            continued = "previous_response_id" in message.request
            if self._debug_recorder is not None:
                capture = self._debug_recorder.begin_capture(
                    method="WEBSOCKET", url=url, headers=headers, body=message.text
                )
                capture.record_response_head(*_websocket_response_head(pooled.websocket))
            # Bytes go out as one text frame: no UTF-8 encoding on the Event Loop.
            send_result = pooled.websocket.send(message.data, text=True)
            if inspect.isawaitable(send_result):
                await send_result
            while True:
                raw_frame = await pooled.websocket.recv()
                if isinstance(raw_frame, bytes):
                    frame_bytes = raw_frame
                    frame_text = raw_frame.decode("utf-8", errors="replace")
                elif isinstance(raw_frame, str):
                    frame_text = raw_frame
                    frame_bytes = raw_frame.encode("utf-8")
                else:
                    raise TypeError("Responses WebSocket returned a non-text frame")
                events_received = True
                if capture is not None:
                    capture.feed_body(frame_bytes + b"\n")
                event = json.loads(frame_text)
                if not isinstance(event, Mapping):
                    raise ValueError("Responses WebSocket event must be an object")
                event_data = dict(event)
                code = _responses_error_code(event_data)
                if (
                    code in _FRESH_SOCKET_CODES
                    and not model_delta_received
                    and (continued or code != "previous_response_not_found")
                ):
                    raise _FreshSocketRequiredError(code)
                event_type = event_data.get("type")
                event_name = event_type if isinstance(event_type, str) else ""
                raw_item = event_data.get("item")
                final_item = (
                    copy.deepcopy(raw_item)
                    if event_name == "response.output_item.done" and isinstance(raw_item, dict)
                    else None
                )
                deltas = normalize_responses_stream_event(event_name, event_data, state)
                if (
                    "output_index" in event_data
                    or event_name in {"response.output_item.added", "response.output_item.done"}
                    or any(
                        delta.get("type") in {"content_delta", "reasoning_delta", "tool_call_delta"}
                        for delta in deltas
                    )
                ):
                    raw_index = event_data.get("output_index")
                    index = (
                        raw_index
                        if isinstance(raw_index, int)
                        and not isinstance(raw_index, bool)
                        and raw_index >= 0
                        else None
                    )
                    output_items.setdefault(index, None)
                    if index is not None and final_item is not None:
                        output_items[index] = final_item
                if any(delta.get("type") == "finish" for delta in deltas):
                    pooled.continuation = _completed_continuation(digest, state, output_items)
                    completed = True
                for delta in deltas:
                    if delta.get("type") in {
                        "content_delta",
                        "reasoning_delta",
                        "tool_call_delta",
                    }:
                        model_delta_received = True
                    yield delta
                if completed:
                    return
        except _FreshSocketRequiredError:
            raise
        except ProviderError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            transport_error = _ResponsesWebSocketTransportError(
                f"Responses WebSocket failed: {exc}", events_received=events_received
            )
            if capture is not None:
                capture.record_error(transport_error)
            raise transport_error from exc
        finally:
            if capture is not None:
                capture.finalize()
            await self._transports.release(pooled, reusable=completed)

    async def _open(
        self,
        url: str,
        key: _SocketKey,
        *,
        headers: dict[str, str],
        route: ResponsesWebSocketRoute,
        handshake: str,
    ) -> _PooledSocket:
        async def connect() -> Any:
            connection = self._websocket_connect(
                url,
                additional_headers=headers,
                open_timeout=_RESPONSES_WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
                max_size=None,
                # Without a context, websockets parses the CA bundle per connection.
                ssl=shared_ssl_context() if url.startswith("wss://") else None,
                # The default offer, compressing off the Event Loop.
                compression=None,
                extensions=[_PrecompressingDeflateFactory()],
            )
            return await connection if inspect.isawaitable(connection) else connection

        try:
            return await self._transports.open(
                key, route=route, handshake=handshake, connect=connect
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            transport_error = _ResponsesWebSocketTransportError(
                f"Responses WebSocket failed: {exc}",
                events_received=False,
                handshake_status=_handshake_status(exc),
            )
            if self._debug_recorder is not None:
                capture = self._debug_recorder.begin_capture(
                    method="WEBSOCKET", url=url, headers=headers, body=None
                )
                capture.record_error(transport_error)
                capture.finalize()
            raise transport_error from exc

    def _websocket_url(self) -> str:
        base_url = self._base_url.rstrip("/")
        if base_url.startswith("https://"):
            websocket_base = f"wss://{base_url.removeprefix('https://')}"
        elif base_url.startswith("http://"):
            websocket_base = f"ws://{base_url.removeprefix('http://')}"
        else:
            raise ProviderError(
                f"Responses WebSocket requires an HTTP(S) base URL, got {base_url!r}",
                retryable=False,
            )
        return f"{websocket_base}{self._endpoint_path}"
