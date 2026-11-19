"""Portals end to end: opened by the Agent, listed for the card, forwarded under access rules."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Iterator, MutableMapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast, override

import httpx
import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]
from websockets.asyncio.server import ServerConnection, serve

from core.access.config import access_config
from core.sessions import SessionAddress
from core.tools.portal import register_portal_tool
from core.tools.portal_registry import PortalRegistry
from server.access._principal import HOST, HOST_PRINCIPAL
from server.access._service import AccessService
from server.access.tls import ABORT_HTTP_CONNECTION
from server.app import create_app
from server.file_delivery import FileDelivery
from server.portals import PortalServer, _PortalApp
from tests.server.rpc_test_support import StubAdapter, StubRuntime


class _App(BaseHTTPRequestHandler):
    """The Agent's app: records what reaches it and answers with a cookie."""

    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:
        headers = {name.casefold(): value for name, value in self.headers.items()}
        type(self).seen.append({**headers, "path": self.path})
        body = b"app page"
        self.send_response(200)
        self.send_header("Set-Cookie", "sid=42; Domain=localhost; Path=/; HttpOnly")
        self.send_header("Clear-Site-Data", '"cookies", "storage"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture
def app_port() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _App.seen = []
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _rpc(client: TestClient, method: str, **params: Any) -> dict[str, Any]:
    return cast(
        dict[str, Any], client.post("/api/rpc", json={"method": method, "params": params}).json()
    )


def _portal_cards(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        portal = value.get("portal")
        if isinstance(portal, dict) and "id" in portal:
            yield portal
        for item in value.values():
            yield from _portal_cards(item)
    elif isinstance(value, list):
        for item in value:
            yield from _portal_cards(item)


def test_an_agent_portal_reaches_the_app_only_with_the_cards_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_port: int
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    call = {"action": "open", "port": app_port, "title": "Shop"}
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_portal", "name": "portal", "arguments": call}],
            },
            {"content": "Open the card.", "tool_calls": None},
        ]
    )
    runtime = StubRuntime(tmp_path, adapter)
    register_portal_tool(runtime.tools, runtime.portals)
    app = create_app(runtime=cast(Any, runtime))
    session = {"agent_id": "coder", "session_id": "session-one"}

    with TestClient(app) as client:
        _rpc(client, "session.create", **session)
        _rpc(client, "chat.send", **session, content="Show me the shop")
        [card] = list(_portal_cards(_rpc(client, "chat.history", **session)["result"]))
        listed = _rpc(client, "portal.list", **session)["result"]
        [portal] = listed["portals"]
        base = f"http://127.0.0.1:{portal['listen_port']}"
        with httpx.Client() as browser:
            private = browser.get(f"{base}/cart", headers={"accept": "text/html"})
            entered = browser.get(f"{base}/cart?step=2&vbot_portal={portal['token']}")
            cookie = f"vbot_portal_{portal['listen_port']}={portal['token']}"
            page = browser.get(
                f"{base}/cart?step=2",
                headers={
                    "cookie": f"{cookie}; vbot_device=secret; vp_{portal['id']}_sid=41",
                    "origin": base,
                },
            )
        closed = _rpc(client, "portal.close", id=portal["id"])["result"]
        after = _rpc(client, "portal.list", **session)["result"]["portals"]

    assert card == {"id": portal["id"], "title": "Shop", "port": app_port}
    assert listed["reverse_proxy"] is False
    assert portal["target_port"] == app_port and portal["listening"] is True
    assert private.status_code == 401 and "private" in private.text
    assert entered.status_code == 302
    assert entered.headers["location"] == "/cart?step=2"
    assert entered.headers["set-cookie"].startswith(cookie + "; Path=/; HttpOnly")
    assert page.status_code == 200 and page.text == "app page"
    # The app's cookie is namespaced for this Portal; vBot's never reach the app.
    assert page.headers["set-cookie"] == f"vp_{portal['id']}_sid=42; Path=/; HttpOnly"
    # Its Clear-Site-Data would clear them all, port or not.
    assert "clear-site-data" not in page.headers
    [seen] = _App.seen
    assert seen["path"] == "/cart?step=2"
    assert seen["host"] == f"localhost:{app_port}"
    assert seen["origin"] == f"http://localhost:{app_port}"
    assert seen["cookie"] == "sid=41"
    assert closed == {"id": portal["id"], "closed": True}
    assert after == []


def _portal_server(
    access: AccessService, delivery: FileDelivery, **options: Any
) -> tuple[PortalRegistry, PortalServer]:
    registry = PortalRegistry(probe=lambda _port: asyncio.sleep(0, result=True))
    server = PortalServer(
        registry=registry,
        access=access,
        file_delivery=delivery,
        listen_host="127.0.0.1",
        own_port=1,
        **options,
    )
    registry.attach(server)
    return registry, server


_OWNER = SessionAddress(None, "coder", "session-one")


@pytest.mark.asyncio
async def test_a_portal_bridges_websockets_and_waits_for_an_app_that_is_not_up() -> None:
    async def echo(connection: ServerConnection) -> None:
        assert connection.request is not None
        host = connection.request.headers["Host"]
        async for message in connection:
            await connection.send(f"echo {message!s} via {host}")

    def refused(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    delivery = FileDelivery()
    access = AccessService(access_config("local"), local_token=None, bind_host="127.0.0.1")
    registry, server = _portal_server(access, delivery, transport=httpx.MockTransport(refused))
    async with serve(echo, "127.0.0.1", 0) as app:
        app_port = app.sockets[0].getsockname()[1]
        portal, _opened = await registry.open(_OWNER, target_port=app_port, title="t", path="/")
        cookie = f"vbot_portal_{portal.listen_port}={delivery.portal_token(portal.id)}"
        from websockets.asyncio.client import connect

        async with connect(
            f"ws://127.0.0.1:{portal.listen_port}/hmr", additional_headers={"cookie": cookie}
        ) as socket:
            await socket.send("ping")
            echoed = await socket.recv()
        async with httpx.AsyncClient() as browser:
            waiting = await browser.get(
                f"http://127.0.0.1:{portal.listen_port}/",
                headers={"cookie": cookie, "sec-fetch-dest": "iframe"},
            )
    listening = registry.get(portal.id)
    await registry.aclose()
    await server.aclose()

    assert echoed == f"echo ping via localhost:{app_port}"
    assert waiting.status_code == 502
    assert '<meta http-equiv="refresh"' in waiting.text and str(app_port) in waiting.text
    assert listening is not None and listening.listening is False


@pytest.mark.asyncio
async def test_an_app_that_breaks_off_requests_is_logged_once_until_it_answers_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def app(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/broken":
            raise httpx.RemoteProtocolError("server disconnected without a response")
        return httpx.Response(200, stream=httpx.ByteStream(b"app page"))

    delivery = FileDelivery()
    access = AccessService(access_config("local"), local_token=None, bind_host="127.0.0.1")
    registry, server = _portal_server(access, delivery, transport=httpx.MockTransport(app))
    portal, _opened = await registry.open(_OWNER, target_port=5999, title="t", path="/")
    cookie = f"vbot_portal_{portal.listen_port}={delivery.portal_token(portal.id)}"
    caplog.set_level(logging.DEBUG, logger="vbot.server.portals")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{portal.listen_port}") as browser:
        statuses = [
            (await browser.get(path, headers={"cookie": cookie})).status_code
            for path in ("/broken", "/broken", "/", "/", "/broken")
        ]
    await registry.aclose()
    await server.aclose()

    assert statuses == [502, 502, 200, 200, 502]
    # A failure is no INFO, and a failing app logs on each change, not per request.
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "vbot.server.portals"
    ] == [
        (logging.WARNING, f"Portal request failed (id={portal.id} error=RemoteProtocolError)"),
        (logging.INFO, f"Portal requests answered again (id={portal.id})"),
        (logging.WARNING, f"Portal request failed (id={portal.id} error=RemoteProtocolError)"),
    ]


@pytest.mark.asyncio
async def test_an_endless_answer_ends_when_the_browser_leaves_or_the_portal_closes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ended: asyncio.Queue[None] = asyncio.Queue()

    class _Events(httpx.AsyncByteStream):
        """An event stream that sends one event and then stays open."""

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            try:
                yield b"data: first\n\n"
                await asyncio.get_running_loop().create_future()
            finally:
                ended.put_nowait(None)

    def events(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Events())

    delivery = FileDelivery()
    access = AccessService(access_config("local"), local_token=None, bind_host="127.0.0.1")
    registry, server = _portal_server(access, delivery, transport=httpx.MockTransport(events))
    portal, _opened = await registry.open(_OWNER, target_port=5999, title="t", path="/")
    request = (
        f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{portal.listen_port}\r\n"
        f"Cookie: vbot_portal_{portal.listen_port}={delivery.portal_token(portal.id)}\r\n\r\n"
    ).encode()

    async def first_event() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection("127.0.0.1", portal.listen_port)
        writer.write(request)
        await reader.readuntil(b"data: first")
        return reader, writer

    async with asyncio.timeout(5):
        # The browser leaves: the app's stream, and its connection, end with it.
        _reader, writer = await first_event()
        writer.close()
        await ended.get()
        # The Portal closes while the browser still reads.
        reader, writer = await first_event()
        server.close(portal.id)
        await ended.get()
        # Its connection ends as well.
        await reader.read()
        assert reader.at_eof()
        writer.close()
    await registry.aclose()
    await server.aclose()

    # Neither end reads as a failed application.
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_the_credential_redirect_keeps_the_address_the_browser_sent() -> None:
    delivery = FileDelivery()
    access = AccessService(access_config("local"), local_token=None, bind_host="127.0.0.1")
    registry, server = _portal_server(access, delivery)
    portal, _opened = await registry.open(_OWNER, target_port=5173, title="t", path="/")
    credential = f"vbot_portal={delivery.portal_token(portal.id)}"
    locations = {}
    async with httpx.AsyncClient() as browser:
        for target in (
            f"/files/a%3Fb/caf%C3%A9/x%2Fy/100%25?{credential}",
            f"/search?flag&q=a+b%26c&{credential}&page=2",
            # Not a protocol-relative address of another host.
            f"//other.host/x?{credential}",
        ):
            response = await browser.get(f"http://127.0.0.1:{portal.listen_port}{target}")
            locations[target.split("?")[0]] = response.headers["location"]
    await registry.aclose()
    await server.aclose()

    assert locations == {
        "/files/a%3Fb/caf%C3%A9/x%2Fy/100%25": "/files/a%3Fb/caf%C3%A9/x%2Fy/100%25",
        "/search": "/search?flag&q=a+b%26c&page=2",
        "//other.host/x": "/.//other.host/x",
    }


@pytest.mark.asyncio
async def test_signed_in_access_needs_a_capability_bound_to_a_live_principal(
    tmp_path: Path, app_port: int
) -> None:
    delivery = FileDelivery()
    access = AccessService(
        access_config(
            "authenticated", public_url="https://vbot.example.com", trusted_proxies=["10.0.0.1"]
        ),
        local_token=None,
        bind_host="127.0.0.1",
    )
    await access.start(tmp_path)

    registry, server = _portal_server(access, delivery)
    portal, _opened = await registry.open(_OWNER, target_port=app_port, title="t", path="/")
    token = delivery.portal_token(portal.id)
    other, _opened = await registry.open(_OWNER, target_port=5174, title="t", path="/")
    statuses = {}
    async with httpx.AsyncClient() as browser:
        for name, credential in {
            "unbound": token,
            "unknown device": delivery.bind_payload(token, "dev_unknown"),
            "other portal": delivery.bind_payload(delivery.portal_token(other.id), HOST_PRINCIPAL),
            "host": delivery.bind_payload(token, HOST_PRINCIPAL),
        }.items():
            response = await browser.get(
                f"http://127.0.0.1:{portal.listen_port}/",
                headers={"cookie": f"vbot_portal_{portal.listen_port}={credential}"},
            )
            statuses[name] = response.status_code
    await registry.aclose()
    await server.aclose()
    await access.close()

    assert statuses == {"unbound": 401, "unknown device": 401, "other portal": 401, "host": 200}


@pytest.mark.asyncio
async def test_only_the_browsers_own_portal_cookie_reaches_the_app() -> None:
    def app(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sign-in":
            return httpx.Response(
                200,
                headers={"set-cookie": "sid=first-browser; Domain=localhost; Path=/; HttpOnly"},
                stream=httpx.ByteStream(b"signed in"),
            )
        sid = request.headers.get("cookie", "anonymous")
        return httpx.Response(200, stream=httpx.ByteStream(sid.encode()))

    delivery = FileDelivery()
    access = AccessService(access_config("local"), local_token=None, bind_host="127.0.0.1")
    registry, server = _portal_server(access, delivery, transport=httpx.MockTransport(app))
    shop = _PortalApp(server, "shop", 5173)
    shop.listen_port = 7000
    other = _PortalApp(server, "other", 5174)
    other.listen_port = 7001

    def browser(portal: _PortalApp, portal_id: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=portal),
            base_url=f"http://127.0.0.1:{portal.listen_port}",
            cookies={f"vbot_portal_{portal.listen_port}": delivery.portal_token(portal_id)},
        )

    try:
        async with browser(shop, "shop") as first:
            signed_in = await first.get("/sign-in")
            assert signed_in.headers["set-cookie"] == "vp_shop_sid=first-browser; Path=/; HttpOnly"
            assert (await first.get("/")).text == "sid=first-browser"
        async with browser(shop, "shop") as second:
            assert (await second.get("/")).text == "anonymous"
        async with browser(other, "other") as separate_portal:
            assert (await separate_portal.get("/")).text == "anonymous"
    finally:
        await registry.aclose()
        await server.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending_http", "streaming_http", "fixed_http", "websocket"])
async def test_device_revocation_ends_open_portal_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    delivery = FileDelivery()
    access = AccessService(
        access_config(
            "authenticated", public_url="https://vbot.example", trusted_proxies=["10.0.0.1"]
        ),
        local_token=None,
        bind_host="127.0.0.1",
    )
    await access.start(tmp_path)
    websocket = kind == "websocket"
    scope: dict[str, Any] = {
        "type": "websocket" if websocket else "http",
        "scheme": "wss" if websocket else "https",
        "method": "GET",
        "path": "/events",
        "client": ("203.0.113.7", 40000),
        "headers": [(b"host", b"vbot.example")],
    }
    facts = access.inspect(scope)
    pending = access.pair_start({"kind": "desktop", "name": "Desktop"}, facts)
    access.decide_pairing(pending["code"], HOST, approved=True)
    paired, _cookie = await access.pair_poll(pending, facts)
    device_id = paired["device"]["id"]
    token = delivery.bind_payload(delivery.portal_token("portal"), device_id)
    scope["headers"].append((b"cookie", f"vbot_portal_0={token}".encode()))
    first_message = asyncio.Event()
    upstream_closed = asyncio.Event()
    browser_waiting = asyncio.Event()
    browser_read_ended = asyncio.Event()
    browser_aborted = asyncio.Event()
    upstream_read_ended = asyncio.Event()
    sent: list[MutableMapping[str, Any]] = []
    scope["state"] = {ABORT_HTTP_CONNECTION: browser_aborted.set}

    class Events(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            try:
                yield b"first"
                await asyncio.Future()
            finally:
                upstream_read_ended.set()

        @override
        async def aclose(self) -> None:
            upstream_closed.set()

    async def respond(_request: httpx.Request) -> httpx.Response:
        if kind == "pending_http":
            # The response becomes available during revocation. Its body has
            # never been relayed, but its stream still needs closing.
            assert access.connections.revoke({device_id}) == 1
        headers = {"content-length": "10"} if kind == "fixed_http" else {}
        return httpx.Response(200, headers=headers, stream=Events())

    class Upstream:
        subprotocol = None

        async def __aiter__(self) -> AsyncIterator[str]:
            try:
                yield "first"
                await asyncio.Future()
            finally:
                upstream_read_ended.set()

        async def close(self) -> None:
            upstream_closed.set()

    async def connect(*args: Any, **kwargs: Any) -> Upstream:
        return Upstream()

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    registry, server = _portal_server(access, delivery, transport=httpx.MockTransport(respond))
    portal = _PortalApp(server, "portal", 5173)
    incoming = [{"type": "websocket.connect"}] if websocket else []

    async def receive() -> dict[str, Any]:
        if incoming:
            return incoming.pop()
        browser_waiting.set()
        try:
            await browser_aborted.wait()
            return {"type": "http.disconnect"}
        finally:
            browser_read_ended.set()

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "websocket.close":
            raise OSError("browser connection ended")
        if message.get("body") == b"first" or message.get("text") == "first":
            first_message.set()

    try:
        async with asyncio.timeout(2):
            request = asyncio.create_task(portal(scope, receive, send))
            try:
                if kind != "pending_http":
                    await browser_waiting.wait()
                    await first_message.wait()
                    assert access.connections.count(device_id) == 1
                    await access.revoke_devices([device_id], HOST)
                await asyncio.shield(request)
            finally:
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
    finally:
        await registry.aclose()
        await server.aclose()
        await access.close()

    assert access.connections.count(device_id) == 0
    assert upstream_closed.is_set()
    assert browser_read_ended.is_set()
    assert browser_aborted.is_set() == (kind == "fixed_http")
    if kind == "pending_http":
        assert [message["type"] for message in sent] == [
            "http.response.start",
            "http.response.body",
        ]
        assert sent[0]["status"] == 401
    elif websocket:
        assert upstream_read_ended.is_set()
        assert [message["type"] for message in sent] == [
            "websocket.accept",
            "websocket.send",
            "websocket.close",
        ]
        assert sent[-1]["code"] == 4401
    else:
        assert upstream_read_ended.is_set()
        types = ["http.response.start", "http.response.body"]
        if kind == "streaming_http":
            types.append("http.response.body")
            assert not sent[-1].get("more_body")
        assert [message["type"] for message in sent] == types
        assert sent[0]["status"] == 200
