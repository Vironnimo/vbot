"""Connection handshakes, private pipe concurrency and installation lifecycle."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from core.channels.config import ChannelConfig, ChannelError
from tests.core.channels.channels_helpers import make_service
from tests.core.channels.test_network_channels import event, make_adapter

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class Socket:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = iter(events)
        self.send = AsyncMock()
        self.close = AsyncMock()

    async def __aenter__(self) -> Socket:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __aiter__(self) -> Socket:
        return self

    async def __anext__(self) -> str:
        try:
            return json.dumps(next(self.events))
        except StopIteration:
            raise StopAsyncIteration from None

    async def recv(self) -> str:
        return await self.__anext__()


@pytest.mark.asyncio
async def test_slack_acknowledges_before_dispatch_and_clears_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = make_adapter(tmp_path, "slack")
    adapter.api = AsyncMock(side_effect=[{"user_id": "BOT"}, {"url": "wss://wss.slack.com/link"}])
    socket = Socket(
        [{"type": "events_api", "envelope_id": "e1", "payload": {"event": {"text": "hi"}}}]
    )
    monkeypatch.setattr("core.channels.slack.connect", lambda *a, **kw: socket)

    async def handle(event: dict[str, Any]) -> None:
        assert adapter.connection_status() == {"connected": True}
        assert socket.send.await_args is not None
        assert json.loads(socket.send.await_args.args[0]) == {"envelope_id": "e1"}
        assert event["text"] == "hi"

    adapter.handle_event = AsyncMock(side_effect=handle)
    try:
        with pytest.raises(ChannelError, match="connection closed"):
            await adapter.start()
        adapter.handle_event.assert_awaited_once()
        assert adapter.connection_status() == {"connected": False}
        assert adapter.api.await_args_list[1].kwargs == {"app_token": True}
    finally:
        await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["OK", "FAIL"])
async def test_mattermost_requires_websocket_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    adapter = make_adapter(tmp_path, "mattermost")
    adapter._connected = False
    adapter.api = AsyncMock(return_value={"id": "BOT", "username": "vbot"})
    socket = Socket([{"seq_reply": 1, "status": status}, {"event": "posted", "data": {}}])
    monkeypatch.setattr("core.channels.mattermost.connect", lambda *a, **kw: socket)
    adapter.handle_event = AsyncMock()
    try:
        with pytest.raises(ChannelError):
            await adapter.start()
        assert adapter.handle_event.await_count == (1 if status == "OK" else 0)
        assert socket.send.await_args is not None
        assert json.loads(socket.send.await_args.args[0])["data"] == {"token": "secret-BOT"}
        assert not adapter.connection_status()["connected"]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_whatsapp_reader_resolves_worker_requests_and_propagates_worker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = make_adapter(tmp_path, "whatsapp")
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"event":"message","id":"first"}\n')

    def write(data: bytes) -> None:
        request = json.loads(data)
        reader.feed_data((json.dumps({"id": request["id"], "ok": True}) + "\n").encode())

    process = SimpleNamespace(
        stdout=reader,
        stdin=SimpleNamespace(write=write, drain=AsyncMock(), close=Mock()),
        returncode=None,
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr("core.channels.whatsapp.bridge_ready", lambda _: True)
    monkeypatch.setattr("core.channels.whatsapp.node_executable", lambda: "node")
    monkeypatch.setattr(
        "core.channels.whatsapp.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )

    async def handle(event: dict[str, Any]) -> None:
        assert (await adapter.call_bridge({"action": "send"}))["ok"]
        raise ChannelError("worker failed")

    adapter.handle_event = handle
    try:
        with pytest.raises(ChannelError, match="worker failed"):
            await asyncio.wait_for(adapter.start(), timeout=2)
        assert adapter._pending == {}
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_whatsapp_setup_is_idempotent_and_shutdown_cancels_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    service.create_channel(
        ChannelConfig(id="wa", platform="whatsapp", agent_id="assistant", enabled=False)
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def install(_: Path) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr("core.channels._whatsapp_setup.install_bridge", install)
    monkeypatch.setattr("core.channels._whatsapp_setup.bridge_ready", lambda _: False)
    try:
        await service.setup_whatsapp("wa")
        await started.wait()
        original = service._whatsapp_setup_tasks["wa"]
        assert (await service.setup_whatsapp("wa"))["setup"] == "installing"
        assert service._whatsapp_setup_tasks["wa"] is original
        with pytest.raises(ChannelError):
            service.delete_channel("wa")
        with pytest.raises(ChannelError):
            service.update_channel("wa", platform="telegram")
    finally:
        await service.aclose()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_ambiguous_http_write_does_not_retry_or_expose_request_url(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "slack")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("secret-url?token=credential", request=request)

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(ChannelError) as error:
            await adapter.api("chat.postMessage", {})
        assert not error.value.retryable
        assert "credential" not in str(error.value)
        assert error.value.__suppress_context__
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_pairing_preparation_prevents_deletion_until_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    service.create_channel(
        ChannelConfig(id="wa", platform="whatsapp", agent_id="assistant", enabled=False)
    )
    entered = asyncio.Event()

    async def status(_: str) -> dict[str, Any]:
        entered.set()
        await asyncio.Future()
        return {}

    monkeypatch.setattr(service, "whatsapp_status", status)
    pairing = asyncio.create_task(service.pair_whatsapp("wa"))
    try:
        await entered.wait()
        with pytest.raises(ChannelError, match="operation to finish"):
            service.delete_channel("wa")
    finally:
        pairing.cancel()
        await asyncio.gather(pairing, return_exceptions=True)
        await service.aclose()
    service.delete_channel("wa")
    assert service.list_channels() == []


@pytest.mark.asyncio
async def test_cancelled_ingress_drains_receipt_write_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.channels import _network_adapter

    adapter = make_adapter(tmp_path, "slack")
    adapter._engine.handle_inbound_text = AsyncMock()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_write = _network_adapter.atomic_write_text

    def blocked_write(*args: Any) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        original_write(*args)

    monkeypatch.setattr(_network_adapter, "atomic_write_text", blocked_write)
    receiving = asyncio.create_task(adapter.handle_event(event("slack")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        receiving.cancel()
        done, _ = await asyncio.wait((receiving,), timeout=0.02)
        assert not done
        receiving.cancel()
        done, _ = await asyncio.wait((receiving,), timeout=0.02)
        assert not done
    finally:
        release.set()
        await asyncio.gather(receiving, return_exceptions=True)
        await adapter.stop()
    assert receiving.cancelled()
    restarted = make_adapter(tmp_path, "slack")
    restarted._engine.handle_inbound_text = AsyncMock()
    try:
        await restarted.load_seen()
        await restarted.handle_event(event("slack"))
        restarted._engine.handle_inbound_text.assert_not_awaited()
    finally:
        await restarted.stop()
