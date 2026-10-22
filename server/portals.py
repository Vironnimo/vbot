"""Portal listeners: one port of the server per Portal, forwarding to an app on localhost.

Each Portal gets a listener of its own on the server's bind host (port chosen
by the system), so the app runs in an origin of its own and never in the
WebUI's. The listener serves uvicorn's HTTP protocol directly (the TLS-sniffing
one under direct TLS), without a ``uvicorn.Server``: no lifespan, signals or log
lines. Every request passes the access guard's peer, transport and Host rules,
then needs the Portal's credential: the signed Portal capability the WebUI card
received, from the ``vbot_portal`` query parameter (answered by a redirect that
moves it into a cookie of this port) or from that cookie. Without it the
Portal answers "private".

Forwarding keeps the app unaware of vBot: Host and same-origin Origin/Referer
name ``localhost:<target>``, vBot's and other Portals' cookies never reach the
app, and the app's cookies carry a per-Portal prefix in the browser, because
cookies ignore ports. Responses stream through as they arrive; WebSockets are
bridged message by message. While nothing listens on the target, page loads get
a page that reloads itself (``.vorch/domain-maps/tools/portal.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

import httpx

from core.tools.portal_registry import PortalRegistry
from core.utils.logging import get_logger
from server.access._service import AccessError, AccessService

if TYPE_CHECKING:
    from server.access.tls import TlsContextHolder
    from server.file_delivery import FileDelivery

_LOGGER = get_logger("server.portals")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

PORTAL_QUERY = "vbot_portal"
PORTAL_COOKIE_PREFIX = "vbot_portal_"
#: WebSocket close code for a Portal request without its credential.
WEBSOCKET_PRIVATE = 4401
_CONNECT_TIMEOUT_SECONDS = 5.0
_WAITING_REFRESH_SECONDS = 2
_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
# Headers the forwarder sets itself, and the client's own forwarding claims.
_REPLACED_REQUEST_HEADERS = frozenset({b"host", b"cookie", b"origin", b"referer", b"forwarded"})
# Response headers the app may not send through: cookies ignore ports, so the
# app's Clear-Site-Data would clear vBot's Device cookie and other Portals' cookies.
_DROPPED_RESPONSE_HEADERS = _HOP_BY_HOP | {b"clear-site-data"}


def app_cookie_prefix(portal_id: str) -> str:
    """The prefix the app's cookies carry in the browser."""
    return f"vp_{portal_id}_"


class PortalServer:
    """The server's ``PortalListeners``: bind, serve and close one listener per Portal."""

    def __init__(
        self,
        *,
        registry: PortalRegistry,
        access: AccessService,
        file_delivery: FileDelivery,
        listen_host: str,
        own_port: int,
        tls: TlsContextHolder | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``transport`` carries the forwarded HTTP requests; the default connects to the app."""
        self._registry = registry
        self._access = access
        self._file_delivery = file_delivery
        self._listen_host = listen_host
        self._tls = tls
        self.own_ports = frozenset({own_port})
        self._listeners: dict[str, _Listener] = {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=_CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        # The browser's headers go through as they are; no client defaults.
        self._client.headers.clear()

    async def open(self, portal_id: str, target_port: int) -> int:
        app = _PortalApp(self, portal_id, target_port)
        listener = await _Listener.start(app, self._listen_host, tls=self._tls)
        app.listen_port = listener.port
        self._listeners[portal_id] = listener
        return listener.port

    def close(self, portal_id: str) -> None:
        listener = self._listeners.pop(portal_id, None)
        if listener is not None:
            listener.close()

    async def aclose(self) -> None:
        for portal_id in list(self._listeners):
            self.close(portal_id)
        await self._client.aclose()


class _Listener:
    """One bound port served by uvicorn's protocol classes."""

    def __init__(self, servers: list[asyncio.Server], state: Any, port: int) -> None:
        self._servers = servers
        self._state = state
        self.port = port

    @classmethod
    async def start(cls, app: Any, host: str, *, tls: TlsContextHolder | None) -> _Listener:
        import uvicorn
        from uvicorn.server import ServerState

        protocol: dict[str, Any] = {}
        if tls is not None:
            from server.access.tls import sniffing_protocol_class

            protocol["http"] = sniffing_protocol_class(tls)
        # log_config=None: building a Config must not reconfigure the process's logging.
        config = uvicorn.Config(
            app,
            host=host,
            port=0,
            lifespan="off",
            proxy_headers=False,
            server_header=False,
            date_header=False,
            ws_per_message_deflate=False,
            access_log=False,
            log_config=None,
            **protocol,
        )
        config.load()
        state = ServerState()

        def create_protocol(_loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Protocol:
            return config.http_protocol_class(  # type: ignore[call-arg]
                config=config, server_state=state, app_state={}, _loop=_loop
            )

        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(
            host, 0, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
        servers: list[asyncio.Server] = []
        port = 0
        try:
            for _family, _type, _proto, _canonical, sockaddr in dict.fromkeys(addresses):
                try:
                    server = await loop.create_server(
                        create_protocol, host=str(sockaddr[0]), port=port
                    )
                except OSError:
                    if not servers:
                        raise
                    # A second address of the name (IPv6 beside IPv4) is a convenience.
                    continue
                servers.append(server)
                if not port:
                    port = server.sockets[0].getsockname()[1]
        except BaseException:
            for server in servers:
                server.close()
            raise
        return cls(servers, state, port)

    def close(self) -> None:
        for server in self._servers:
            server.close()
        for connection in list(self._state.connections):
            connection.shutdown()
        for task in list(self._state.tasks):
            task.cancel()


class _PortalApp:
    """The ASGI application of one Portal's listener."""

    def __init__(self, server: PortalServer, portal_id: str, target_port: int) -> None:
        self._server = server
        self._portal_id = portal_id
        self._target_port = target_port
        self._cookie_prefix = app_cookie_prefix(portal_id)
        self.listen_port = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in {"http", "websocket"}:
            return
        access = self._server._access
        facts = access.inspect(scope)
        path = str(scope.get("path") or "/")
        if facts.refusal is not None:
            access.refused(facts.refusal, facts, f"portal:{self._portal_id}{path}")
            await _deny(scope, receive, send, 403, "Access denied.")
            return
        query_token, query = _split_query(scope)
        cookie_token = _cookie(scope, self._auth_cookie)
        token = query_token or cookie_token
        try:
            authorized = token is not None and self._authorized(token)
        except AccessError as exc:
            await _deny(scope, receive, send, exc.status, exc.message)
            return
        if not authorized:
            await _deny(
                scope,
                receive,
                send,
                401,
                "This portal is private. Open it from its card in the vBot chat.",
            )
            return
        if query_token is not None and kind == "http" and scope.get("method") in {"GET", "HEAD"}:
            await self._redirect_into_cookie(scope, send, query_token, path, query)
            return
        if kind == "websocket":
            await self._forward_websocket(scope, receive, send, query)
        else:
            await self._forward_http(scope, receive, send, query)

    # Credential ----------------------------------------------------------------

    @property
    def _auth_cookie(self) -> str:
        return f"{PORTAL_COOKIE_PREFIX}{self.listen_port}"

    def _authorized(self, token: str) -> bool:
        claims = self._server._file_delivery.portal_claims(token)
        if claims is None or claims[0] != self._portal_id:
            return False
        access = self._server._access
        if access.config.mode != "authenticated":
            return True
        actor = claims[1]
        return actor is not None and access.principal_for_capability(actor) is not None

    async def _redirect_into_cookie(
        self, scope: Scope, send: Send, token: str, path: str, query: str
    ) -> None:
        secure = scope.get("scheme") == "https"
        cookie = f"{self._auth_cookie}={token}; Path=/; HttpOnly; SameSite=Lax" + (
            "; Secure" if secure else ""
        )
        location = path + (f"?{query}" if query else "")
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [
                    (b"location", location.encode("latin-1", "replace")),
                    (b"set-cookie", cookie.encode("latin-1")),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    # Forwarding ----------------------------------------------------------------

    def _upstream_headers(self, scope: Scope) -> list[tuple[bytes, bytes]]:
        own_origin = self._own_origin(scope)
        target_origin = f"http://localhost:{self._target_port}".encode("latin-1")
        headers: list[tuple[bytes, bytes]] = []
        for name, value in scope.get("headers") or ():
            if (
                name in _HOP_BY_HOP
                or name in _REPLACED_REQUEST_HEADERS
                or name.startswith(b"x-forwarded-")
                or name.startswith(b"sec-websocket-")
            ):
                continue
            headers.append((name, value))
        for name in (b"origin", b"referer"):
            for value in _header_values(scope, name):
                # Only the Portal's own pages speak for the app's origin.
                if value == own_origin or value.startswith(own_origin + b"/"):
                    value = target_origin + value[len(own_origin) :]
                headers.append((name, value))
        cookies = self._app_cookies(scope)
        if cookies:
            headers.append((b"cookie", cookies.encode("latin-1")))
        host = _header_values(scope, b"host")
        if host:
            headers.append((b"x-forwarded-host", host[0]))
        headers.append((b"x-forwarded-proto", str(scope.get("scheme") or "http").encode()))
        client = scope.get("client")
        if client:
            headers.append((b"x-forwarded-for", str(client[0]).encode("latin-1")))
        return headers

    def _own_origin(self, scope: Scope) -> bytes:
        scheme = "https" if scope.get("scheme") in {"https", "wss"} else "http"
        host = _header_values(scope, b"host")
        return f"{scheme}://".encode() + (host[0] if host else b"")

    def _app_cookies(self, scope: Scope) -> str:
        pairs = []
        for name, value in _cookie_pairs(scope):
            if name.startswith(self._cookie_prefix):
                pairs.append(f"{name[len(self._cookie_prefix) :]}={value}")
        return "; ".join(pairs)

    def _response_headers(self, headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
        result: list[tuple[bytes, bytes]] = []
        target_origins = (
            f"http://localhost:{self._target_port}",
            f"http://127.0.0.1:{self._target_port}",
            f"http://[::1]:{self._target_port}",
        )
        for raw_name, raw_value in headers.raw:
            name = raw_name.lower()
            if name in _DROPPED_RESPONSE_HEADERS:
                continue
            value = raw_value
            if name == b"set-cookie":
                value = self._renamed_cookie(raw_value.decode("latin-1")).encode("latin-1")
            elif name == b"location":
                text = raw_value.decode("latin-1")
                for origin in target_origins:
                    if text == origin or text.startswith(origin + "/"):
                        text = text[len(origin) :] or "/"
                        break
                value = text.encode("latin-1")
            result.append((name, value))
        return result

    def _renamed_cookie(self, header: str) -> str:
        cookie, _separator, attributes = header.partition(";")
        name, equals, value = cookie.partition("=")
        kept = [
            attribute
            for attribute in attributes.split(";")
            if attribute.strip() and attribute.strip().split("=", 1)[0].casefold() != "domain"
        ]
        renamed = f"{self._cookie_prefix}{name.strip()}{equals}{value}"
        return ";".join([renamed, *kept])

    def _upstream_url(self, scope: Scope, query: str, *, scheme: str = "http") -> str:
        raw_path = scope.get("raw_path")
        path = raw_path.decode("latin-1") if raw_path else str(scope.get("path") or "/")
        path = path.split("?", 1)[0] or "/"
        return f"{scheme}://localhost:{self._target_port}{path}" + (f"?{query}" if query else "")

    async def _forward_http(self, scope: Scope, receive: Receive, send: Send, query: str) -> None:
        client = self._server._client
        has_body = bool(_header_values(scope, b"transfer-encoding")) or any(
            value.strip() not in {b"", b"0"} for value in _header_values(scope, b"content-length")
        )
        request = client.build_request(
            str(scope.get("method") or "GET"),
            self._upstream_url(scope, query),
            headers=self._upstream_headers(scope),
            content=_request_body(receive) if has_body else None,
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.ConnectError, httpx.ConnectTimeout:
            self._server._registry.report_listening(self._portal_id, False)
            await self._waiting(scope, send)
            return
        except httpx.HTTPError as exc:
            _LOGGER.info(
                "Portal request failed (id=%s error=%s)", self._portal_id, type(exc).__name__
            )
            await _plain(send, 502, "The app did not answer this request.")
            return
        self._server._registry.report_listening(self._portal_id, True)
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": self._response_headers(response.headers),
                }
            )
            async for chunk in response.aiter_raw():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        except httpx.HTTPError:
            # The app broke off its answer; the browser sees a cut-off response.
            await send({"type": "http.response.body", "body": b""})
        finally:
            await response.aclose()

    async def _waiting(self, scope: Scope, send: Send) -> None:
        if not _wants_page(scope):
            await _plain(send, 502, f"Nothing answers on port {self._target_port} yet.")
            return
        body = _WAITING_PAGE.format(
            port=self._target_port, seconds=_WAITING_REFRESH_SECONDS
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 502,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _forward_websocket(
        self, scope: Scope, receive: Receive, send: Send, query: str
    ) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        message = await receive()
        if message.get("type") != "websocket.connect":
            return
        upstream_headers = self._upstream_headers(scope)
        headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in upstream_headers
            if name not in {b"origin", b"content-length"}
        ]
        origins = [value.decode("latin-1") for name, value in upstream_headers if name == b"origin"]
        try:
            upstream = await connect(
                self._upstream_url(scope, query, scheme="ws"),
                origin=origins[0] if origins else None,  # type: ignore[arg-type]
                subprotocols=list(scope.get("subprotocols") or ()) or None,  # type: ignore[arg-type]
                additional_headers=headers,
                user_agent_header=None,
                compression=None,
                proxy=None,
                # The app listens on this computer: plain ws:// without TLS.
                ssl=None,
                open_timeout=_CONNECT_TIMEOUT_SECONDS,
                ping_interval=None,
                max_size=None,
                happy_eyeballs_delay=0.25,
            )
        except (OSError, TimeoutError, InvalidHandshake) as exc:
            if isinstance(exc, (OSError, TimeoutError)):
                self._server._registry.report_listening(self._portal_id, False)
            await send({"type": "websocket.close", "code": 1011})
            return
        self._server._registry.report_listening(self._portal_id, True)
        await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})

        async def browser_to_app() -> None:
            while True:
                incoming = await receive()
                if incoming["type"] == "websocket.disconnect":
                    await upstream.close(_closable(incoming.get("code", 1000)))
                    return
                text = incoming.get("text")
                await upstream.send(text if text is not None else incoming.get("bytes") or b"")

        async def app_to_browser() -> None:
            try:
                async for data in upstream:
                    if isinstance(data, str):
                        await send({"type": "websocket.send", "text": data})
                    else:
                        await send({"type": "websocket.send", "bytes": data})
            except ConnectionClosed:
                pass
            code = upstream.close_code or 1000
            with contextlib.suppress(Exception):
                await send(
                    {
                        "type": "websocket.close",
                        "code": _closable(code),
                        "reason": upstream.close_reason or "",
                    }
                )

        tasks = [asyncio.ensure_future(browser_to_app()), asyncio.ensure_future(app_to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await upstream.close()


_PAGE_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<style>body{{font:15px system-ui,sans-serif;background:#111;color:#bbb;display:flex;"
    "align-items:center;justify-content:center;height:100vh;margin:0}}</style>"
)
_WAITING_PAGE = (
    _PAGE_HEAD + '<meta http-equiv="refresh" content="{seconds}">'
    "<title>Waiting for the app</title></head>"
    "<body><p>Waiting for the app on port {port}&hellip;</p></body></html>"
)
_PRIVATE_PAGE = (
    _PAGE_HEAD + "<title>Private portal</title></head><body><p>{message}</p></body></html>"
)


_SENDABLE_CLOSE_CODES = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1011, 1012, 1013})


def _closable(code: Any) -> int:
    """A close code an endpoint may send; reserved ones such as 1005 become 1000."""
    if isinstance(code, int) and (code in _SENDABLE_CLOSE_CODES or 3000 <= code <= 4999):
        return code
    return 1000


async def _deny(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
    if scope.get("type") == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        await send(
            {
                "type": "websocket.close",
                "code": WEBSOCKET_PRIVATE if status == 401 else 4403,
                "reason": message,
            }
        )
        return
    if _wants_page(scope):
        body = _PRIVATE_PAGE.format(message=html.escape(message)).encode("utf-8")
        content_type = b"text/html; charset=utf-8"
    else:
        body = message.encode("utf-8")
        content_type = b"text/plain; charset=utf-8"
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"referrer-policy", b"no-referrer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _plain(send: Send, status: int, message: str) -> None:
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _request_body(receive: Receive) -> AsyncIterator[bytes]:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        body = message.get("body") or b""
        if body:
            yield body
        if not message.get("more_body", False):
            return


def _wants_page(scope: Scope) -> bool:
    """Whether the browser loads a page here (a document or frame), not a resource."""
    destination = _header_values(scope, b"sec-fetch-dest")
    if destination:
        return destination[0] in {b"document", b"iframe", b"frame"}
    accept = b",".join(_header_values(scope, b"accept"))
    return b"text/html" in accept


def _split_query(scope: Scope) -> tuple[str | None, str]:
    """The Portal credential from the query, and the query without it."""
    raw = scope.get("query_string") or b""
    text = raw.decode("latin-1")
    if PORTAL_QUERY not in text:
        return None, text
    pairs = parse_qsl(text, keep_blank_values=True)
    tokens = [value for name, value in pairs if name == PORTAL_QUERY]
    rest = urlencode([(name, value) for name, value in pairs if name != PORTAL_QUERY])
    return (tokens[0] if len(tokens) == 1 and tokens[0] else None), rest


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers") or () if key == name]


def _cookie_pairs(scope: Scope) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for header in _header_values(scope, b"cookie"):
        for item in header.decode("latin-1").split(";"):
            name, equals, value = item.strip().partition("=")
            if equals and name:
                pairs.append((name, value))
    return pairs


def _cookie(scope: Scope, name: str) -> str | None:
    values = [value for key, value in _cookie_pairs(scope) if key == name]
    return values[0] if len(values) == 1 and values[0] else None
