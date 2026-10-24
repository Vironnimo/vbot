"""Runtime-owned Provider connections; request state stays in borrowed clients."""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator
from concurrent.futures import Future
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar
from time import monotonic
from typing import Any, override
from urllib.request import getproxies

import httpx

from core.utils.tls import shared_ssl_context
from core.utils.workers import cancel_and_wait, settle_before_cancelling


class _DiscardCookies(CookieJar):
    """The internal routing client must never retain response credentials/state."""

    @override
    def set_cookie(self, cookie: Cookie, *args: Any, **kwargs: Any) -> None:
        pass


type _ScopeKey = tuple[str, str, str]
type _PoolKey = tuple[
    asyncio.AbstractEventLoop,
    _ScopeKey,
    tuple[str, str, int | None],
    tuple[tuple[str, str], ...],
]


@dataclass(eq=False)
class _Entry:
    key: _PoolKey
    client: httpx.AsyncClient
    last_used: float
    active: int = 0
    retired: bool = False
    closing: bool = False
    requests: set[asyncio.Task[httpx.Response]] = field(default_factory=set)
    streams: set[_BorrowedStream] = field(default_factory=set)


class ProviderHTTPTransports:
    """Own bounded, loop-local Provider connection pools for one Runtime.

    Scopes and clients may be constructed in workers. Only a request creates
    network resources, on its Event Loop. Retirement stops reuse immediately;
    already admitted exchanges keep their connections until their streams close.
    ``aclose`` is the terminal shutdown and closes even still-active exchanges.
    """

    def __init__(
        self,
        *,
        max_pools: int = 32,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        keepalive_expiry: float = 20.0,
    ) -> None:
        if min(max_pools, max_connections, max_keepalive_connections) < 1:
            raise ValueError("Provider HTTP pool limits must be positive")
        if keepalive_expiry <= 0:
            raise ValueError("Provider HTTP keepalive expiry must be positive")
        self._max_pools = max_pools
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )
        self._keepalive_expiry = keepalive_expiry
        self._lock = threading.RLock()
        self._cache: OrderedDict[_PoolKey, _Entry] = OrderedDict()
        self._entries: set[_Entry] = set()
        self._pending: set[Future[None]] = set()
        self._close_error: BaseException | None = None
        self._closed = False

    def scope(
        self,
        provider_id: str,
        connection_id: str,
        account_id: str = "default",
    ) -> ProviderHTTPTransportScope:
        """Return a loop-neutral capability, without opening or pinning a pool."""
        return ProviderHTTPTransportScope(self, (provider_id, connection_id, account_id))

    def retire_all(self) -> None:
        """Retire cached connections from any thread without aborting exchanges."""
        with self._lock:
            for entry in tuple(self._entries):
                self._retire(entry)

    async def close_connection(self, provider_id: str, connection_id: str | None = None) -> None:
        """Retire a Provider/Connection and drain its idle network cleanup.

        Active streams finish normally; their last close performs the remaining
        cleanup. The same existing capability acquires a new pool on its next call.
        """
        with self._lock:
            for entry in tuple(self._entries):
                scope = entry.key[1]
                if scope[0] == provider_id and (connection_id is None or scope[1] == connection_id):
                    self._retire(entry)
            pending = tuple(self._pending)
        await self._drain(pending)

    def abort(self) -> None:
        """Stop admission synchronously and schedule cleanup on live owner loops.

        Call ``aclose`` before those loops stop for deterministic socket cleanup.
        Python's async transport API cannot close sockets on a stopped Event Loop.
        """
        with self._lock:
            self._closed = True
            for entry in tuple(self._entries):
                self._retire(entry, force=True)

    async def aclose(self) -> None:
        """Stop admission and drain all network cleanup, including active pools."""
        self.abort()
        with self._lock:
            pending = tuple(self._pending)
        await self._drain(pending)

    async def _drain(self, pending: tuple[Future[None], ...]) -> None:
        if pending:
            await settle_before_cancelling(
                asyncio.gather(
                    *(asyncio.wrap_future(future) for future in pending), return_exceptions=True
                )
            )
        with self._lock:
            error = self._close_error
        if error is not None:
            raise error

    def _acquire(self, scope: _ScopeKey, request: httpx.Request) -> _Entry:
        loop = asyncio.get_running_loop()
        # Use the same public environment source as HTTPX. Including NO_PROXY
        # ensures a routing change cannot reuse a client with old proxy mounts.
        proxy_environment = tuple(sorted(getproxies().items()))
        origin = (request.url.scheme, request.url.host, request.url.port)
        key = (loop, scope, origin, proxy_environment)
        with self._lock:
            if self._closed:
                raise RuntimeError("Provider HTTP transports are closed")
            now = monotonic()
            for old in tuple(self._cache.values()):
                if old.active == 0 and now - old.last_used >= self._keepalive_expiry:
                    self._retire(old)
            entry = self._cache.get(key)
            if entry is None:
                if len(self._cache) >= self._max_pools:
                    for old in self._cache.values():
                        if old.active == 0:
                            self._retire(old)
                            break
                # HTTPX owns environment proxy/NO_PROXY routing through its public
                # client API. This client receives fully-built outer requests;
                # no cookies, auth, redirects or debug state belong to the pool.
                entry = _Entry(
                    key=key,
                    client=httpx.AsyncClient(
                        verify=shared_ssl_context(),
                        limits=self._limits,
                        cookies=_DiscardCookies(),
                        headers={},
                        auth=None,
                        follow_redirects=False,
                        trust_env=True,
                    ),
                    last_used=now,
                )
                self._entries.add(entry)
                if len(self._cache) < self._max_pools:
                    self._cache[key] = entry
                else:
                    # All cached pools are active. A temporary pool keeps the
                    # cache bounded without evicting another Run's live stream.
                    entry.retired = True
            else:
                self._cache.move_to_end(key)
            entry.active += 1
            return entry

    def _release(self, entry: _Entry) -> None:
        with self._lock:
            entry.active -= 1
            entry.last_used = monotonic()
            if entry.retired and entry.active == 0:
                self._schedule_close(entry)

    def _retire(self, entry: _Entry, *, force: bool = False) -> None:
        entry.retired = True
        if self._cache.get(entry.key) is entry:
            del self._cache[entry.key]
        if force or entry.active == 0:
            self._schedule_close(entry)

    def _schedule_close(self, entry: _Entry) -> None:
        if entry.closing:
            return
        loop = entry.key[0]
        if loop.is_closed() or not loop.is_running():
            if self._close_error is None:
                self._close_error = RuntimeError(
                    "Provider HTTP transport owner Event Loop is no longer running"
                )
            return
        work = self._close_entry(entry)
        try:
            future = asyncio.run_coroutine_threadsafe(work, loop)
        except RuntimeError:
            work.close()
            return
        entry.closing = True
        self._pending.add(future)
        future.add_done_callback(self._closed_entry)

    async def _close_entry(self, entry: _Entry) -> None:
        try:
            try:
                # These are isolated exchange Tasks, never their caller's Run.
                # Their failures already belong to those callers; closing still
                # has to drain every admitted request and close every response.
                for request in tuple(entry.requests):
                    request.cancel()
                await asyncio.gather(*tuple(entry.requests), return_exceptions=True)
                await self._close_streams(tuple(entry.streams))
            finally:
                await entry.client.aclose()
        finally:
            with self._lock:
                self._entries.discard(entry)

    @staticmethod
    async def _close_streams(streams: tuple[_BorrowedStream, ...]) -> None:
        results = await asyncio.gather(
            *(stream.close_response() for stream in streams), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def _closed_entry(self, future: Future[None]) -> None:
        with self._lock:
            self._pending.discard(future)
            error = (
                RuntimeError("Provider HTTP transport cleanup was cancelled")
                if future.cancelled()
                else future.exception()
            )
            if self._close_error is None and error is not None:
                self._close_error = error


@dataclass(frozen=True)
class ProviderHTTPTransportScope:
    """A Runtime's Provider/Connection/Account boundary, with no request state."""

    _owner: ProviderHTTPTransports
    _key: _ScopeKey

    def borrow(self) -> httpx.AsyncBaseTransport:
        """Create one client-local transport view; unused views acquire nothing."""
        return _BorrowedTransport(self._owner, self._key)


def standalone_transport() -> httpx.AsyncBaseTransport:
    """Give a standalone captured client the same routing, with owned cleanup."""
    return _BorrowedTransport(ProviderHTTPTransports(), ("", "", ""), close_owner=True)


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        owner: ProviderHTTPTransports,
        scope: _ScopeKey,
        *,
        close_owner: bool = False,
    ) -> None:
        self._owner = owner
        self._scope = scope
        self._close_owner = close_owner
        self._closed = False
        self._requests: set[asyncio.Task[httpx.Response]] = set()
        self._streams: set[_BorrowedStream] = set()

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise RuntimeError("Provider HTTP client is closed")
        # Cancellation belongs to one HTTP exchange. Isolating it also permits
        # shutdown to drain a queued request without awaiting its whole Run,
        # whose finally block may itself close the client or Runtime.
        operation = asyncio.create_task(self._exchange(request))
        self._requests.add(operation)
        try:
            return await operation
        except BaseException as interrupted:
            if operation.done() and not operation.cancelled() and operation.exception() is None:
                # A completed exchange can win the race with cancellation of
                # this await. Its caller never received the response and cannot
                # close it; settle that unclaimed response here.
                try:
                    await settle_before_cancelling(operation.result().aclose())
                except BaseException as cleanup_error:
                    if isinstance(interrupted, asyncio.CancelledError):
                        raise interrupted from cleanup_error
                    raise
            raise
        finally:
            self._requests.discard(operation)

    async def _exchange(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise RuntimeError("Provider HTTP client is closed")
        task = asyncio.current_task()
        assert task is not None
        entry = self._owner._acquire(self._scope, request)
        entry.requests.add(task)
        try:
            try:
                response = await entry.client.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
            except BaseException:
                self._owner._release(entry)
                raise
            # No await between receipt and registration: terminal cleanup sees
            # either an admitted exchange or its response, never a gap.
            stream = _BorrowedStream(response, entry, self)
            self._streams.add(stream)
            entry.streams.add(stream)
            response.stream = stream
            return response
        finally:
            entry.requests.discard(task)

    @override
    async def aclose(self) -> None:
        self._closed = True

        async def close() -> None:
            try:
                await cancel_and_wait(
                    *(task for task in self._requests if task is not asyncio.current_task())
                )
            finally:
                try:
                    await self._owner._close_streams(tuple(self._streams))
                finally:
                    if self._close_owner:
                        await self._owner.aclose()

        await settle_before_cancelling(close())


class _BorrowedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        response: httpx.Response,
        entry: _Entry,
        borrower: _BorrowedTransport,
    ) -> None:
        assert isinstance(response.stream, httpx.AsyncByteStream)
        self._inner = response.stream
        self._response = response
        self._entry = entry
        self._borrower = borrower
        self._close_task: asyncio.Task[None] | None = None

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                yield chunk
        finally:
            await self.aclose()

    @override
    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await settle_before_cancelling(self._close_task)

    async def close_response(self) -> None:
        try:
            # The outer client may have wrapped this stream for Debug capture.
            # Close that public Response so its finalization also runs.
            await self._response.aclose()
        finally:
            await self.aclose()

    async def _close(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._borrower._streams.discard(self)
            self._entry.streams.discard(self)
            self._borrower._owner._release(self._entry)
