"""The tray's server monitor follows one /ws stream and classifies a missing server."""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from cli._server_target import HealthProbeResult
from cli.application import monitor as monitor_module
from cli.application.monitor import MonitorStatus, ServerMonitor
from cli.server_management import ServerState

_RUN_EVENT = {
    "type": "run_completed",
    "sequence": 5,
    "payload": {"run_id": "run_1", "run_kind": "user"},
}


class _Recorder:
    def __init__(self) -> None:
        self.calls: queue.Queue[tuple[str, Any]] = queue.Queue()

    def status_changed(self, status: MonitorStatus) -> None:
        self.calls.put(("status", status))

    def connection_lost(self, close_code: int) -> None:
        self.calls.put(("lost", close_code))

    def event_received(self, event: dict[str, Any]) -> None:
        self.calls.put(("event", event))

    async def next(self) -> tuple[str, Any]:
        return await asyncio.to_thread(self.calls.get, True, 5)


class _SilentClient(httpx.AsyncClient):
    """Answers no request in time, like a server whose Event Loop is blocked."""

    def __init__(self, **kwargs: Any) -> None:
        def time_out(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("no answer", request=request)

        super().__init__(transport=httpx.MockTransport(time_out), **kwargs)


def _query(path: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}


def test_monitor_follows_the_stream_as_tray_and_resumes_after_a_restart():
    paths: list[str] = []
    resumed = asyncio.Event()

    async def handler(connection: ServerConnection) -> None:
        assert connection.request is not None
        paths.append(connection.request.path)
        if len(paths) == 1:
            hello = {"epoch": "e1", "last_sequence": 4, "replay_status": "fresh"}
            await connection.send(json.dumps({"type": "connection_ready", **hello}))
            await connection.send(json.dumps({"type": "heartbeat"}))
            await connection.send(json.dumps(_RUN_EVENT))
            await connection.close(1012, "restart")
            return
        hello = {"epoch": "e1", "last_sequence": 5, "replay_status": "resumed"}
        await connection.send(json.dumps({"type": "connection_ready", **hello}))
        resumed.set()
        await connection.wait_closed()

    async def scenario() -> list[tuple[str, Any]]:
        async with serve(handler, "127.0.0.1", 0) as server:
            url = f"http://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"
            recorder = _Recorder()
            monitor = ServerMonitor(lambda: url, recorder, local=True, user_agent="test")
            await asyncio.to_thread(monitor.start)
            try:
                calls = [await recorder.next() for _ in range(3)]
                await asyncio.wait_for(resumed.wait(), timeout=5)
            finally:
                await asyncio.to_thread(monitor.close)
            assert monitor.status == MonitorStatus(url, "connected", True)
        return calls

    calls = asyncio.run(scenario())

    url = calls[0][1].url
    assert calls == [
        ("status", MonitorStatus(url, "connected", True)),
        ("event", _RUN_EVENT),
        ("lost", 1012),
    ]
    first, second = _query(paths[0]), _query(paths[1])
    assert first["accessor"] == "tray"
    assert "epoch" not in first
    assert second == {**first, "epoch": "e1", "after_sequence": "5"}


@pytest.mark.parametrize(
    ("health", "recorded", "expected"),
    [
        pytest.param(None, "absent", ("refused", False), id="nothing-listens"),
        pytest.param('{"status":"ok"}', "running", ("rejected", True), id="vbot-refuses-stream"),
        pytest.param("<html></html>", "foreign", ("rejected", False), id="foreign-listener"),
        pytest.param(TimeoutError, "unresponsive", ("unresponsive", False), id="busy-server"),
        pytest.param(TimeoutError, "absent", ("unreachable", False), id="silent-listener"),
        pytest.param(TimeoutError, "foreign", ("unreachable", False), id="silent-stranger"),
    ],
)
def test_monitor_classifies_a_target_without_an_event_stream(
    monkeypatch: pytest.MonkeyPatch,
    health: str | type[TimeoutError] | None,
    recorded: ServerState,
    expected: tuple[str, bool],
):
    observed: list[HealthProbeResult] = []

    def classify(result: HealthProbeResult) -> ServerState:
        observed.append(result)
        return recorded

    if health is None or health is TimeoutError:
        # A real refused loopback connect takes about two seconds on Windows, and a
        # busy server holds the handshake for the whole ten-second open timeout.
        failure = ConnectionRefusedError if health is None else TimeoutError

        async def fail(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(monitor_module, "connect", fail)
    if health is TimeoutError:
        monkeypatch.setattr(monitor_module.httpx, "AsyncClient", _SilentClient)

    def respond(connection: ServerConnection, request: Request) -> Response:
        if request.path.startswith("/ws"):
            return connection.respond(403, "Forbidden")
        return connection.respond(200, health if isinstance(health, str) else "")

    async def never_streams(_connection: ServerConnection) -> None:
        return None

    async def scenario() -> tuple[str, Any]:
        async with serve(never_streams, "127.0.0.1", 0, process_request=respond) as server:
            url = f"http://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"
            recorder = _Recorder()
            monitor = ServerMonitor(
                lambda: url, recorder, local=True, user_agent="test", classify=classify
            )
            await asyncio.to_thread(monitor.start)
            try:
                return await recorder.next()
            finally:
                await asyncio.to_thread(monitor.close)

    name, status = asyncio.run(scenario())

    assert name == "status"
    assert (status.connection, status.vbot) == expected
    # Only an unanswered `/health` request is classified, from that observation alone.
    assert bool(observed) == (health is TimeoutError)
    assert all(
        (item.reachable, item.is_vbot, item.timed_out) == (False, False, True) for item in observed
    )
