"""Follow one vBot server's live event stream for the tray host.

The monitor holds one ``/ws`` connection as the ``tray`` accessor. The server
pushes Run and resource events, and the connection's protocol pings are the only
traffic while nothing happens, so an idle tray does no periodic work for its
server. While disconnected, the monitor retries with a bounded backoff and
classifies the listener through ``/health``; when that goes unanswered too, the
optional ``classify`` callback tells a busy server from an unreachable target.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from cli._server_target import HEALTH_PATH, HealthProbeResult, health_result
from cli.rpc_client import RPC_PATH

if TYPE_CHECKING:
    from cli.server_management import ServerState

_LOGGER = logging.getLogger("vbot.application.monitor")

# Close codes of a server that stopped or restarted on purpose; anything else,
# including a vanished TCP connection (1006), is an unexpected loss.
COOPERATIVE_CLOSE_CODES = frozenset({1000, 1001, 1012})
# A reconnect this soon after a loss asks the server to replay what it missed.
_RESUME_WINDOW_SECONDS = 60.0
_RETARGET_INTERVAL_SECONDS = 30.0
_NO_TARGET_RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class MonitorStatus:
    """The monitor's current knowledge about its server target.

    ``connection`` is ``unknown`` before the first attempt, ``connected`` while
    the event stream is open, ``refused`` when nothing listens, ``rejected``
    when a listener answered but refused the stream, ``unresponsive`` when the
    target's own server process lives but answered neither the stream nor
    ``/health`` (a busy Event Loop, still starting or already stopping),
    ``unreachable`` when the target could not be reached otherwise, and
    ``no_target`` without a target. ``vbot`` says whether the listener's
    ``/health`` identified vBot.
    """

    url: str | None = None
    connection: str = "unknown"
    vbot: bool = False


class MonitorListener(Protocol):
    """Receives monitor observations on the monitor's event-loop thread."""

    def status_changed(self, status: MonitorStatus) -> None: ...

    def connection_lost(self, close_code: int) -> None: ...

    def event_received(self, event: dict[str, Any]) -> None: ...


class ServerMonitor:
    """Own the tray's background event loop, stream connection and server RPC client.

    ``classify`` classifies the target from the monitor's own unanswered
    ``/health`` request, which it receives as the health observation. It runs on
    the monitor's thread only after a failed connect, so it must stay cheap and
    must not probe the target again; without it, silence counts as
    ``unreachable``.
    """

    def __init__(
        self,
        target: Callable[[], str | None],
        listener: MonitorListener,
        *,
        local: bool,
        user_agent: str,
        classify: Callable[[HealthProbeResult], ServerState] | None = None,
    ) -> None:
        self._target = target
        self._listener = listener
        self._local = local
        self._user_agent = user_agent
        self._classify = classify
        # One stable id lets the server's client roster recognize reconnects.
        self._connection_id = uuid.uuid4().hex
        self._status = MonitorStatus()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._client: httpx.AsyncClient | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vbot-tray-monitor", daemon=True)

    @property
    def status(self) -> MonitorStatus:
        return self._status

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)

    def close(self) -> None:
        loop, main = self._loop, self._main
        if loop is not None and main is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(main.cancel)
        self._thread.join(timeout=5)

    def reconnect(self) -> None:
        """Skip the current retry delay, for example right after a server start."""

        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wake.set)

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Call one RPC on the monitored server; ``None`` for any failure."""

        url, client = self._status.url, self._client
        if url is None or client is None:
            return None
        try:
            response = await client.post(
                f"{url}{RPC_PATH}", json={"method": method, "params": params}
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(payload, dict) and payload.get("ok") is True:
            result = payload.get("result")
            if isinstance(result, dict):
                return result
        return None

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:
            _LOGGER.exception("The tray server monitor stopped unexpectedly")
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._main = asyncio.current_task()
        self._wake = asyncio.Event()
        self._ready.set()
        async with httpx.AsyncClient(trust_env=False, timeout=5.0) as client:
            self._client = client
            with contextlib.suppress(asyncio.CancelledError):
                await self._follow()

    async def _follow(self) -> None:
        delay = 0.0
        cursor: tuple[str, int] | None = None
        lost_at = 0.0
        while True:
            url = self._target()
            if url is None:
                self._set_status(MonitorStatus(connection="no_target"))
                await self._pause(_NO_TARGET_RETRY_SECONDS)
                continue
            resume = cursor if time.monotonic() - lost_at < _RESUME_WINDOW_SECONDS else None
            try:
                connection = await connect(
                    _stream_url(url, self._connection_id, resume),
                    user_agent_header=self._user_agent,
                    proxy=None,
                    compression=None,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                    max_size=None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._classify_failure(url, exc)
                cap = 5.0 if self._local else 30.0
                delay = min(cap, max(1.0, delay * 2))
                await self._pause(delay)
                continue
            try:
                hello = await self._hello(connection)
                if hello is None:
                    await self._classify_failure(url, TimeoutError("no connection_ready"))
                    delay = min(5.0, max(1.0, delay * 2))
                    await self._pause(delay)
                    continue
                delay = 0.0
                cursor = _next_cursor(cursor, hello)
                self._set_status(MonitorStatus(url=url, connection="connected", vbot=True))
                cursor = await self._stream(url, connection, cursor)
            finally:
                await connection.close()
            lost_at = time.monotonic()
            # A vanished connection reports 1006: no close frame arrived.
            self._notify("connection_lost", connection.close_code or 1006)
            # The first retry follows at once; it tells a restart from a stop.
            await self._pause(0.25)

    @staticmethod
    async def _hello(connection: ClientConnection) -> dict[str, Any] | None:
        try:
            hello = json.loads(await asyncio.wait_for(connection.recv(), timeout=10))
        except (ConnectionClosed, TimeoutError, ValueError):
            return None
        if not isinstance(hello, dict) or hello.get("type") != "connection_ready":
            return None
        return hello

    async def _stream(
        self, url: str, connection: ClientConnection, cursor: tuple[str, int] | None
    ) -> tuple[str, int] | None:
        retarget = None if self._local else asyncio.create_task(self._watch_target(url, connection))
        try:
            async for frame in connection:
                try:
                    event = json.loads(frame)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") == "heartbeat":
                    continue
                sequence = event.get("sequence")
                if cursor is not None and isinstance(sequence, int):
                    cursor = (cursor[0], sequence)
                self._notify("event_received", event)
        except ConnectionClosed:
            pass
        finally:
            if retarget is not None:
                retarget.cancel()
        return cursor

    async def _watch_target(self, url: str, connection: ClientConnection) -> None:
        # A Desktop Client follows the server its Desktop last used.
        while True:
            await asyncio.sleep(_RETARGET_INTERVAL_SECONDS)
            if self._target() != url:
                await connection.close(1000, "target changed")
                return

    async def _classify_failure(self, url: str, error: Exception) -> None:
        if isinstance(error, ConnectionRefusedError):
            self._set_status(MonitorStatus(url=url, connection="refused"))
            return
        if not isinstance(error, (OSError, InvalidHandshake, TimeoutError)):
            _LOGGER.warning("Unexpected tray stream failure: %s", error, exc_info=error)
        assert self._client is not None
        try:
            health = health_result(await self._client.get(f"{url}{HEALTH_PATH}", timeout=2.0))
        except httpx.RequestError as exc:
            if isinstance(exc, httpx.ConnectError) and self._local:
                connection = "refused"
            else:
                connection = self._classify_silence(exc)
            self._set_status(MonitorStatus(url=url, connection=connection))
            return
        self._set_status(MonitorStatus(url=url, connection="rejected", vbot=health.is_vbot))

    def _classify_silence(self, error: httpx.RequestError) -> str:
        """Tell a busy server from an unreachable target without probing it again."""

        if self._classify is None:
            return "unreachable"
        health = HealthProbeResult(
            reachable=False,
            is_vbot=False,
            error=str(error) or type(error).__name__,
            timed_out=isinstance(error, httpx.TimeoutException),
        )
        try:
            state = self._classify(health)
        except Exception:
            _LOGGER.exception("Could not classify the silent tray server target")
            return "unreachable"
        return "unresponsive" if state == "unresponsive" else "unreachable"

    async def _pause(self, seconds: float) -> None:
        assert self._wake is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        self._wake.clear()

    def _set_status(self, status: MonitorStatus) -> None:
        if status != self._status:
            self._status = status
            self._notify("status_changed", status)

    def _notify(self, name: str, value: Any) -> None:
        try:
            getattr(self._listener, name)(value)
        except Exception:
            _LOGGER.exception("The tray monitor listener failed in %s", name)


def _next_cursor(cursor: tuple[str, int] | None, hello: dict[str, Any]) -> tuple[str, int] | None:
    """Keep a replayed cursor; otherwise start after the hello's high-water mark."""

    epoch, last_sequence = hello.get("epoch"), hello.get("last_sequence")
    if not isinstance(epoch, str) or not isinstance(last_sequence, int):
        return None
    if cursor is not None and cursor[0] == epoch and hello.get("replay_status") == "resumed":
        return cursor
    return (epoch, last_sequence)


def _stream_url(url: str, connection_id: str, resume: tuple[str, int] | None) -> str:
    scheme, _, rest = url.partition("://")
    params: dict[str, str | int] = {"connection_id": connection_id, "accessor": "tray"}
    if resume is not None:
        params["epoch"], params["after_sequence"] = resume
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/ws?{urlencode(params)}"
