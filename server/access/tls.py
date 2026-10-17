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
    instead of waiting for it.
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
        self._handshake: asyncio.Future[None] | None = None
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
        if data[0] != _TLS_HANDSHAKE_RECORD:
            self._server_state.connections.discard(self)
            transport.set_protocol(inner)
            inner.connection_made(transport)
            inner.data_received(data)
            return
        handshake: asyncio.Future[None] = self._loop.create_future()
        handshake.add_done_callback(self._handshake_done)
        self._handshake = handshake
        tls = SSLProtocol(
            self._loop,
            inner,
            self._holder.context,
            handshake,
            server_side=True,
            ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT_SECONDS,
        )
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
        """uvicorn's shutdown: close a connection not yet handed to uvicorn's protocol."""
        self._server_state.connections.discard(self)
        if self._handshake is not None and self._handshake.done():
            return
        self._abort()

    def _handshake_done(self, handshake: asyncio.Future[None]) -> None:
        # After a completed handshake uvicorn's protocol tracks the connection.
        self._server_state.connections.discard(self)
        if not handshake.cancelled():
            handshake.exception()

    def _abort(self) -> None:
        self._cancel_timeout()
        if self._transport is not None:
            self._transport.abort()

    def _cancel_timeout(self) -> None:
        if self._timeout is not None:
            self._timeout.cancel()
            self._timeout = None
