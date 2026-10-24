"""Real HTTP/1.1 connections guard Runtime pooling and client-local lifetimes."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import httpx
import pytest
import pytest_asyncio

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore
from core.providers import _http_pool
from core.providers.http import ProviderHTTPTransports, build_async_client
from core.utils.workers import cancel_and_wait


@dataclass
class _Server:
    """Small persistent HTTP server; events control streams without time waits."""

    body: bytes = b"ok"
    accepted: int = 0
    closed: int = 0
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    release_stream: asyncio.Event = field(default_factory=asyncio.Event)
    waiting_head: asyncio.Event = field(default_factory=asyncio.Event)
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    handlers: set[asyncio.Task[None]] = field(default_factory=set)
    url: str = ""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        self.writers.add(writer)
        async with self.changed:
            self.accepted += 1
            self.changed.notify_all()
        try:
            while True:
                try:
                    raw = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    break
                lines = raw.decode("ascii").split("\r\n")
                target = lines[0].split(" ")[1]
                headers = dict(line.lower().split(": ", 1) for line in lines[1:] if line)
                if "content-length" in headers:
                    await reader.readexactly(int(headers["content-length"]))
                self.requests.append((target, headers))
                if target == "/head":
                    self.waiting_head.set()
                    await self.release_stream.wait()
                if target == "/stream":
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\none")
                    await writer.drain()
                    await self.release_stream.wait()
                    writer.write(b"two")
                else:
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(self.body)).encode()
                        + b"\r\nSet-Cookie: run=old; Path=/\r\n\r\n"
                        + self.body
                    )
                await writer.drain()
        except ConnectionError, asyncio.CancelledError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.writers.discard(writer)
            self.handlers.discard(task)
            async with self.changed:
                self.closed += 1
                self.changed.notify_all()

    async def wait_closed(self, count: int) -> None:
        async with self.changed:
            await self.changed.wait_for(lambda: self.closed >= count)


@contextlib.asynccontextmanager
async def _server(body: bytes = b"ok") -> AsyncIterator[_Server]:
    state = _Server(body=body)
    server = await asyncio.start_server(state.handle, "127.0.0.1", 0)
    state.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield state
    finally:
        server.close()
        state.release_stream.set()
        for writer in tuple(state.writers):
            writer.close()
        await cancel_and_wait(*tuple(state.handlers))
        await server.wait_closed()


@pytest.fixture(autouse=True)
def _direct_http(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


@pytest_asyncio.fixture
async def owner() -> AsyncIterator[ProviderHTTPTransports]:
    transports = ProviderHTTPTransports()
    try:
        yield transports
    finally:
        await transports.aclose()


def _recorder(tmp_path: Path, run_id: str) -> tuple[ProviderDebugRecorder, DebugTraceStore]:
    store = DebugTraceStore(tmp_path / run_id, trace_limit=10)
    recorder = ProviderDebugRecorder(store)
    recorder.set_context(
        DebugContext(
            run_id=run_id,
            agent_id="agent",
            session_id="session",
            provider_id="provider",
            connection_id="connection",
            model_id="model",
            streaming=False,
            iteration_number=1,
        )
    )
    return recorder, store


@pytest.mark.asyncio
async def test_separate_clients_reuse_one_socket_with_fresh_credentials_cookies_and_capture(
    owner: ProviderHTTPTransports, tmp_path: Path
) -> None:
    scope = owner.scope("provider", "connection", "account")
    first_recorder, first_store = _recorder(tmp_path, "first")
    second_recorder, second_store = _recorder(tmp_path, "second")
    async with _server() as server:
        for secret, recorder in (("first", first_recorder), ("second", second_recorder)):
            async with build_async_client(
                base_url=server.url, http_transport=scope, debug_recorder=recorder
            ) as client:
                assert (await client.get("/", headers={"Authorization": secret})).content == b"ok"
        assert server.accepted == 1
        assert [headers["authorization"] for _, headers in server.requests] == ["first", "second"]
        assert all("cookie" not in headers for _, headers in server.requests)
        assert len(first_store.get_traces()) == len(second_store.get_traces()) == 1
        await owner.aclose()
        await server.wait_closed(1)
        assert not server.writers


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True], ids=["client-close", "runtime-close"])
async def test_closing_active_response_through_its_owner_finalizes_debug_capture(
    owner: ProviderHTTPTransports, tmp_path: Path, terminal: bool
) -> None:
    recorder, store = _recorder(tmp_path, "active")
    async with (
        _server() as server,
        build_async_client(
            base_url=server.url,
            http_transport=owner.scope("provider", "connection"),
            debug_recorder=recorder,
        ) as client,
    ):
        response = await client.send(client.build_request("GET", "/stream"), stream=True)
        if terminal:
            await owner.aclose()
        else:
            await client.aclose()
        assert response.is_closed
        assert len(store.get_traces()) == 1


@pytest.mark.asyncio
async def test_cancelling_and_closing_one_client_preserves_another_live_stream(
    owner: ProviderHTTPTransports,
) -> None:
    scope = owner.scope("provider", "connection")
    async with (
        _server() as server,
        build_async_client(base_url=server.url, http_transport=scope) as first,
        build_async_client(base_url=server.url, http_transport=scope) as sibling,
    ):
        response = await sibling.send(sibling.build_request("GET", "/stream"), stream=True)
        pending = asyncio.create_task(first.get("/head"))
        await server.waiting_head.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await first.aclose()
        server.release_stream.set()
        assert await response.aread() == b"onetwo"
        assert (await sibling.get("/")).content == b"ok"
        assert server.accepted == 2


@pytest.mark.asyncio
async def test_cancellation_at_completed_response_handoff_closes_unclaimed_stream(
    owner: ProviderHTTPTransports, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Body(httpx.AsyncByteStream):
        closed = False

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"complete"

        @override
        async def aclose(self) -> None:
            self.closed = True

    body = Body()
    caller = asyncio.current_task()
    assert caller is not None

    async def completed(
        _transport: httpx.AsyncHTTPTransport, _request: httpx.Request
    ) -> httpx.Response:
        asyncio.get_running_loop().call_soon(caller.cancel)
        return httpx.Response(200, stream=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", completed)
    async with build_async_client(
        base_url="http://provider.invalid", http_transport=owner.scope("provider", "connection")
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.get("/")
        assert body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("all_connections", [False, True], ids=["connection", "all-from-worker"])
async def test_retirement_preserves_admitted_stream_and_old_scope_uses_fresh_pool(
    owner: ProviderHTTPTransports, all_connections: bool
) -> None:
    scope = owner.scope("provider", "connection")
    async with (
        _server() as server,
        build_async_client(base_url=server.url, http_transport=scope) as client,
    ):
        response = await client.send(client.build_request("GET", "/stream"), stream=True)
        if all_connections:
            await asyncio.to_thread(owner.retire_all)
        else:
            await owner.close_connection("provider", "connection")
        assert (await client.get("/")).content == b"ok"
        assert server.accepted == 2
        assert server.closed == 0
        server.release_stream.set()
        assert await response.aread() == b"onetwo"
        await server.wait_closed(1)
        assert (await client.get("/")).content == b"ok"
        assert server.accepted == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("pooled", [False, True])
async def test_environment_proxy_and_no_proxy_routing_match_with_debug(
    owner: ProviderHTTPTransports,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    debug: bool,
    pooled: bool,
) -> None:
    recorder = _recorder(tmp_path, "proxy")[0] if debug else None
    scope = owner.scope("provider", "connection") if pooled else None
    async with _server(b"proxy") as proxy, _server(b"direct") as target:
        monkeypatch.setenv("HTTP_PROXY", proxy.url)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        async with build_async_client(
            base_url="http://provider.invalid", http_transport=scope, debug_recorder=recorder
        ) as client:
            assert (await client.get("/route")).content == b"proxy"
            assert (await client.get(target.url + "/bypass")).content == b"direct"
        assert proxy.requests[0][0] == "http://provider.invalid/route"
        assert target.requests[0][0] == "/bypass"


@pytest.mark.asyncio
async def test_proxy_environment_changes_replace_routing_for_existing_client(
    owner: ProviderHTTPTransports, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _server(b"first") as first, _server(b"second") as second:
        monkeypatch.setenv("HTTP_PROXY", first.url)
        monkeypatch.setenv("NO_PROXY", "")
        async with build_async_client(
            base_url="http://provider.invalid", http_transport=owner.scope("provider", "connection")
        ) as client:
            assert (await client.get("/")).content == b"first"
            monkeypatch.setenv("HTTP_PROXY", second.url)
            assert (await client.get("/")).content == b"second"
        assert first.accepted == second.accepted == 1


@pytest.mark.asyncio
async def test_pool_cache_evicts_idle_scopes_and_closes_all_sockets() -> None:
    owner = ProviderHTTPTransports(max_pools=2)
    try:
        async with _server() as server:
            # The final return to a's scope requires a new connection because
            # b/c displaced it; the cached c scope still reuses its connection.
            for account in ("a", "b", "c", "c", "a"):
                async with build_async_client(
                    base_url=server.url,
                    http_transport=owner.scope("provider", "connection", account),
                ) as client:
                    await client.get("/")
            assert server.accepted == 4
            await server.wait_closed(2)
            await owner.aclose()
            await server.wait_closed(4)
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_idle_expiry_replaces_socket_without_wall_clock_waits(
    owner: ProviderHTTPTransports, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(_http_pool, "monotonic", lambda: now)
    async with (
        _server() as server,
        build_async_client(
            base_url=server.url, http_transport=owner.scope("provider", "connection")
        ) as client,
    ):
        await client.get("/")
        now += 19
        await client.get("/")
        assert server.accepted == 1
        now += 20
        await client.get("/")
        assert server.accepted == 2
        await server.wait_closed(1)


@pytest.mark.asyncio
async def test_same_scope_on_another_running_event_loop_gets_its_own_pool(
    owner: ProviderHTTPTransports,
) -> None:
    scope = owner.scope("provider", "connection")
    async with (
        _server() as server,
        build_async_client(base_url=server.url, http_transport=scope) as client,
    ):
        await client.get("/")

        async def on_worker_loop() -> None:
            async with build_async_client(
                base_url=server.url, http_transport=scope
            ) as worker_client:
                await worker_client.get("/")
                await worker_client.get("/")
            # Each network owner is drained before its Event Loop stops. This
            # also exercises close scheduling back to the concurrently live loop.
            await owner.close_connection("provider", "connection")

        await asyncio.to_thread(asyncio.run, on_worker_loop())
        assert server.accepted == 2
        await server.wait_closed(2)
        await client.get("/")
        assert server.accepted == 3


@pytest.mark.asyncio
async def test_completed_background_cleanup_failure_remains_visible_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = ProviderHTTPTransports()
    transport_close = httpx.AsyncHTTPTransport.aclose
    cleanup_finished = asyncio.Event()

    async def close(transport: httpx.AsyncHTTPTransport) -> None:
        await transport_close(transport)
        # Publish on the following loop turn, after the cleanup Task has settled.
        asyncio.get_running_loop().call_later(0, cleanup_finished.set)
        raise OSError("test transport close failed")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    async with _server() as server:
        async with build_async_client(
            base_url=server.url, http_transport=owner.scope("provider", "connection")
        ) as client:
            await client.get("/")
        owner.retire_all()
        await cleanup_finished.wait()
        with pytest.raises(OSError):
            await owner.aclose()
        await server.wait_closed(1)


@pytest.mark.asyncio
async def test_full_active_cache_uses_temporary_pool_without_evicting_live_stream() -> None:
    owner = ProviderHTTPTransports(max_pools=1)
    try:
        async with (
            _server() as server,
            build_async_client(
                base_url=server.url, http_transport=owner.scope("provider", "first")
            ) as first,
            build_async_client(
                base_url=server.url, http_transport=owner.scope("provider", "second")
            ) as second,
        ):
            response = await first.send(first.build_request("GET", "/stream"), stream=True)
            await second.get("/")
            await server.wait_closed(1)
            server.release_stream.set()
            assert await response.aread() == b"onetwo"
            await first.get("/")
            assert server.accepted == 2
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_worker_constructed_unused_client_acquires_nothing_and_abort_stops_admission(
    owner: ProviderHTTPTransports,
) -> None:
    def build() -> httpx.AsyncClient:
        return build_async_client(
            base_url="http://provider.invalid", http_transport=owner.scope("provider", "connection")
        )

    client = await asyncio.to_thread(build)
    await client.aclose()
    async with (
        _server() as server,
        build_async_client(
            base_url=server.url, http_transport=owner.scope("provider", "connection")
        ) as live,
    ):
        await live.get("/")
        await asyncio.to_thread(owner.abort)
        with pytest.raises(RuntimeError):
            await live.get("/")
        await owner.aclose()
        await server.wait_closed(1)


@pytest.mark.asyncio
async def test_shutdown_drains_queued_exchange_before_closing_active_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = ProviderHTTPTransports(max_connections=1)
    queued = asyncio.Event()
    send = httpx.AsyncHTTPTransport.handle_async_request

    async def admitted(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        if request.url.path == "/queued":
            queued.set()
        return await send(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", admitted)
    try:
        async with (
            _server() as server,
            build_async_client(
                base_url=server.url, http_transport=owner.scope("provider", "connection")
            ) as client,
        ):
            response = await client.send(client.build_request("GET", "/stream"), stream=True)

            async def request_and_close() -> httpx.Response:
                try:
                    return await client.get("/queued")
                finally:
                    # Cleanup may run in its caller's finally block; shutdown
                    # drains only the HTTP operation and must not deadlock here.
                    await client.aclose()

            pending = asyncio.create_task(request_and_close())
            await queued.wait()
            await owner.aclose()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await response.aclose()
            server.release_stream.set()
            await server.wait_closed(1)
            assert server.accepted == 1
            assert response.is_closed
            with pytest.raises(RuntimeError):
                await client.get("/")
    finally:
        await owner.aclose()
