"""WhatsApp: the self-chat adapter over its private Node bridge pipe."""

from __future__ import annotations

import asyncio
import gc
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.channels.adapter import FileData
from core.channels.config import ChannelError

from .channels_test_support import wait_until
from .network_test_support import make_adapter

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class FakeBridge:
    """Stands in for the bridge process: reports a connection and records requests.

    With ``answer``, every request is confirmed with ``ok``.
    """

    def __init__(self, *, answer: bool = True) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdin = SimpleNamespace(write=self._write, drain=AsyncMock(), close=Mock())
        self.returncode: int | None = None
        self.wait = AsyncMock(return_value=0)
        self.requests: list[dict[str, Any]] = []
        self.written = asyncio.Event()
        self._answer = answer
        self.emit({"event": "connected"})

    def emit(self, event: dict[str, Any]) -> None:
        self.stdout.feed_data((json.dumps(event) + "\n").encode())

    def _write(self, data: bytes) -> None:
        request = json.loads(data)
        self.requests.append(request)
        self.written.set()
        if self._answer:
            self.emit({"id": request["id"], "ok": True})


async def start_bridge(
    adapter: Any, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> asyncio.Task[None]:
    """Run the adapter's connection loop on ``bridge`` until it reports connected."""
    monkeypatch.setattr("core.channels.whatsapp.bridge_ready", lambda _: True)
    monkeypatch.setattr("core.channels.whatsapp.node_executable", lambda: "node")
    monkeypatch.setattr(
        "core.channels.whatsapp.asyncio.create_subprocess_exec", AsyncMock(return_value=bridge)
    )
    listener = asyncio.create_task(adapter.start())
    await wait_until(
        lambda: listener.done() or adapter.pairing_status()["state"] == "connected", timeout=5
    )
    return listener


async def stop_bridge(adapter: Any, listener: asyncio.Task[None], *pending: Any) -> None:
    for task in (listener, *pending):
        task.cancel()
    await asyncio.gather(listener, *pending, return_exceptions=True)
    await adapter.stop()


@pytest.mark.asyncio
async def test_start_requires_installed_support_and_stops_cleanly(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "whatsapp", connected=False).adapter

    with pytest.raises(ChannelError, match="Install WhatsApp support") as error:
        await adapter.start()

    assert not error.value.retryable
    await adapter.stop()
    assert adapter.pairing_status() == {"state": "starting", "qr_image": None}


@pytest.mark.asyncio
async def test_sends_only_to_the_self_chat_with_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = make_adapter(tmp_path, "whatsapp", connected=False).adapter
    bridge = FakeBridge()
    listener = await start_bridge(adapter, bridge, monkeypatch)
    try:
        with pytest.raises(ChannelError):
            await adapter.send("hello", "123@s.whatsapp.net")
        assert bridge.requests == []
        await adapter.send("hello", "self", files=[FileData("note.txt", "text/plain", b"test")])
        assert bridge.requests == [
            {"action": "send", "target": "self", "text": "hello", "id": "1"},
            {
                "action": "send",
                "target": "self",
                "file": {"name": "note.txt", "mimetype": "text/plain", "data": "dGVzdA=="},
                "id": "2",
            },
        ]
    finally:
        await stop_bridge(adapter, listener)


@pytest.mark.asyncio
async def test_reader_resolves_worker_requests_and_propagates_worker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = make_adapter(tmp_path, "whatsapp", connected=False).adapter
    bridge = FakeBridge()
    bridge.emit({"event": "message", "id": "first"})

    async def handle(event: dict[str, Any]) -> None:
        # The ingress worker waits on a bridge reply the reader must still deliver.
        assert (await adapter.call_bridge({"action": "send"}))["ok"]
        raise ChannelError("worker failed")

    adapter.handle_event = handle
    listener = await start_bridge(adapter, bridge, monkeypatch)
    try:
        with pytest.raises(ChannelError, match="worker failed"):
            await asyncio.wait_for(listener, timeout=2)
        assert adapter._pending == {}
    finally:
        await stop_bridge(adapter, listener)


@pytest.mark.asyncio
async def test_logout_fails_pending_calls_without_leaving_pairing_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = make_adapter(tmp_path, "whatsapp", connected=False).adapter
    bridge = FakeBridge(answer=False)
    listener = await start_bridge(adapter, bridge, monkeypatch)
    sending = asyncio.create_task(adapter.call_bridge({"action": "send"}))
    try:
        async with asyncio.timeout(2):
            await bridge.written.wait()
            bridge.emit({"event": "closed", "reason": "logged_out"})
            done, _ = await asyncio.wait((sending,), timeout=0.1)
            assert sending in done
            with pytest.raises(ChannelError) as error:
                await sending
            assert not error.value.retryable
            assert adapter._pending == {}
            assert adapter.pairing_status()["state"] == "logged_out"
            # Revoked credentials cannot recover by restarting; the loop stays idle.
            assert not listener.done()
    finally:
        await stop_bridge(adapter, listener, sending)


@pytest.mark.asyncio
async def test_drain_failure_retrieves_pending_disconnect_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = make_adapter(tmp_path, "whatsapp", connected=False).adapter
    bridge = FakeBridge(answer=False)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _, context: unhandled.append(context))

    async def disconnect_during_drain() -> None:
        # The disconnect fails the pending reply before the pipe write itself fails.
        bridge.emit({"event": "closed"})
        await wait_until(lambda: adapter.pairing_status()["state"] == "disconnected")
        raise BrokenPipeError

    bridge.stdin.drain = disconnect_during_drain
    listener = await start_bridge(adapter, bridge, monkeypatch)
    try:
        with pytest.raises(ChannelError, match="could not be confirmed"):
            await adapter.call_bridge({"action": "send"})
        gc.collect()
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)
        await stop_bridge(adapter, listener)


def test_bridge_gate_real_javascript() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the WhatsApp bridge tests")
    script = Path(__file__).with_name("whatsapp_gate.test.mjs")
    completed = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    bridge = Path(__file__).parents[3] / "core/channels/whatsapp_bridge/bridge.js"
    parsed = subprocess.run(
        [node, "--check", str(bridge)], capture_output=True, text=True, timeout=30
    )
    assert parsed.returncode == 0, parsed.stderr
