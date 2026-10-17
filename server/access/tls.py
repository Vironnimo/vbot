"""Direct TLS: one listener that serves HTTPS and loopback HTTP, with certificate reload.

With ``--tls-cert``/``--tls-key`` the server does not hand TLS to uvicorn. Each new
connection starts with a sniffing protocol that looks at the first byte: a TLS
ClientHello (``0x16``) is wrapped in a server TLS session with the current
certificate, anything else is plain HTTP. The access guard then refuses plain
HTTP from every peer that is not loopback, so local tools (CLI, tray, Desktop)
keep using ``http://127.0.0.1:<port>`` on the same port while everyone else
must use HTTPS.

The certificate files are re-read when they change (a Let's Encrypt renewal), so
new connections use the new certificate without a restart; open ones keep theirs.

A server shutdown closes a TLS connection like a plain one: it sends close_notify
and closes the socket once the pending data is written, without waiting for the
client's close_notify, which a client that does not read (an idle pooled
connection) never sends.
"""

from __future__ import annotations

import asyncio
import os
import ssl
from asyncio.sslproto import SSLProtocol
from typing import Any, override

from core.access.config import AccessConfigError, load_tls_context
from core.utils.logging import get_logger

_LOGGER = get_logger("server.access")

#: A TLS record of type handshake starts every ClientHello.
_TLS_HANDSHAKE_RECORD = 0x16
#: A connection that sends nothing for this long is closed before it is classified.
FIRST_BYTE_TIMEOUT_SECONDS = 10.0
TLS_HANDSHAKE_TIMEOUT_SECONDS = 30
CERTIFICATE_CHECK_INTERVAL_SECONDS = 60.0

_Stamp = tuple[tuple[int, int], tuple[int, int]]


class TlsContextHolder:
    """The current server TLS context, reloaded when its files change."""

    def __init__(self, cert: str, key: str) -> None:
        """Load the certificate; raises ``AccessConfigError`` when it cannot be used."""
        self._cert = cert
        self._key = key
        self._stamp = self._file_stamp()
        self.context: ssl.SSLContext = load_tls_context(cert, key)
        self._failing = False

    def reload_if_changed(self) -> bool:
        """Load the files again if they changed; returns whether the context changed.

        A renewal that is half written or broken keeps the previous certificate:
        the failure is logged once, and the next change is tried again.
        """
        stamp = self._file_stamp()
        if stamp is None or stamp == self._stamp:
            return False
        self._stamp = stamp
        try:
            context = load_tls_context(self._cert, self._key)
        except AccessConfigError as exc:
            if not self._failing:
                _LOGGER.warning(
                    "TLS certificate reload failed; serving the previous certificate (error=%s)",
                    exc,
                )
            self._failing = True
            return False
        self.context = context
        if self._failing:
            _LOGGER.info("TLS certificate reload recovered (cert=%s)", self._cert)
        else:
            _LOGGER.info("TLS certificate reloaded (cert=%s)", self._cert)
        self._failing = False
        return True

    async def watch(self, interval: float = CERTIFICATE_CHECK_INTERVAL_SECONDS) -> None:
        """Check the files every ``interval`` seconds until cancelled."""
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(self.reload_if_changed)

    def _file_stamp(self) -> _Stamp | None:
        try:
            cert, key = os.stat(self._cert), os.stat(self._key)
        except OSError:
            return None
        return (cert.st_mtime_ns, cert.st_size), (key.st_mtime_ns, key.st_size)


def sniffing_protocol_class(holder: TlsContextHolder) -> type[asyncio.Protocol]:
    """The uvicorn ``http`` protocol class for a direct-TLS listener."""
    from uvicorn.config import HTTP_PROTOCOLS
    from uvicorn.importer import import_from_string

    inner_class = import_from_string(HTTP_PROTOCOLS["auto"])

    class _BoundSniffingProtocol(TlsSniffingProtocol):
        _holder = holder
        _inner_class = inner_class

    return _BoundSniffingProtocol


class TlsSniffingProtocol(asyncio.Protocol):
    """Classify one connection by its first byte and hand it to uvicorn's protocol.

    Until then it counts as one of uvicorn's connections, so a shutdown closes it
    instead of waiting for it; a TLS connection is then counted by its TLS session.
    """

    _holder: TlsContextHolder
    _inner_class: Any

    def __init__(
        self,
        config: Any,
        server_state: Any,
        app_state: dict[str, Any],
        _loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._config = config
        self._server_state = server_state
        self._app_state = app_state
        self._loop = _loop or asyncio.get_running_loop()
        self._transport: asyncio.Transport | None = None
        self._timeout: asyncio.TimerHandle | None = None
        self._decided = False

    @override
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self._transport = transport
        self._server_state.connections.add(self)
        self._timeout = self._loop.call_later(FIRST_BYTE_TIMEOUT_SECONDS, self._abort)

    @override
    def data_received(self, data: bytes) -> None:
        transport = self._transport
        if self._decided or transport is None:
            return
        self._decided = True
        self._cancel_timeout()
        inner = self._inner_class(
            config=self._config,
            server_state=self._server_state,
            app_state=self._app_state,
            _loop=self._loop,
        )
        self._server_state.connections.discard(self)
        if data[0] != _TLS_HANDSHAKE_RECORD:
            transport.set_protocol(inner)
            inner.connection_made(transport)
            inner.data_received(data)
            return
        tls = _ServerTlsProtocol(self._loop, inner, self._holder.context, self._server_state)
        transport.set_protocol(tls)
        tls.connection_made(transport)
        view = memoryview(data)
        while view:
            buffer = tls.get_buffer(len(view))
            size = min(len(buffer), len(view))
            buffer[:size] = view[:size]
            tls.buffer_updated(size)
            view = view[size:]

    @override
    def eof_received(self) -> bool | None:
        return False

    @override
    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_timeout()
        self._server_state.connections.discard(self)

    def shutdown(self) -> None:
        """uvicorn's shutdown: close a connection that has not been classified yet."""
        self._server_state.connections.discard(self)
        self._abort()

    def _abort(self) -> None:
        self._cancel_timeout()
        if self._transport is not None:
            self._transport.abort()

    def _cancel_timeout(self) -> None:
        if self._timeout is not None:
            self._timeout.cancel()
            self._timeout = None


class _ServerTlsProtocol(SSLProtocol):
    """One server TLS session, counted as one of uvicorn's connections.

    uvicorn's shutdown reaches it next to the HTTP or WebSocket protocol on top:
    a handshake in progress is aborted; on an established session, the close the
    protocol on top makes now (an idle connection) or after its response stops
    waiting for the client's close_notify. Any other close keeps waiting for it
    (``ssl_shutdown_timeout``), so a slow client still receives what was sent.
    Counting it next to the protocol on top would matter only to uvicorn's
    ``limit_concurrency``, which the server does not set.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        app_protocol: asyncio.BaseProtocol,
        context: ssl.SSLContext,
        server_state: Any,
    ) -> None:
        self._handshake: asyncio.Future[None] = loop.create_future()
        self._handshake.add_done_callback(_retrieve_failure)
        super().__init__(
            loop,
            app_protocol,
            context,
            self._handshake,
            server_side=True,
            ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT_SECONDS,
        )
        self._server_state = server_state
        self._socket: asyncio.Transport | None = None
        self._close_started = False
        self._server_closing = False

    @override
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self._socket = transport
        self._server_state.connections.add(self)
        super().connection_made(transport)

    @override
    def connection_lost(self, exc: BaseException | None) -> None:
        self._socket = None
        self._server_state.connections.discard(self)
        super().connection_lost(exc)

    def shutdown(self) -> None:
        """uvicorn's shutdown; uvicorn then waits only for the protocol on top."""
        self._server_state.connections.discard(self)
        if not self._handshake.done():
            if self._socket is not None:
                self._socket.abort()
            return
        self._server_closing = True
        self._close_after_close_notify()

    @override
    def _start_shutdown(self) -> None:
        # The protocol on top closed its transport, or the client sent close_notify.
        self._close_started = True
        super()._start_shutdown()
        if self._server_closing:
            self._close_after_close_notify()

    def _close_after_close_notify(self) -> None:
        # close_notify is written; closing the socket still writes everything buffered.
        if self._close_started and self._socket is not None and not self._socket.is_closing():
            self._socket.close()


def _retrieve_failure(handshake: asyncio.Future[None]) -> None:
    # A failed handshake ends the connection; the failure needs no further report.
    if not handshake.cancelled():
        handshake.exception()
