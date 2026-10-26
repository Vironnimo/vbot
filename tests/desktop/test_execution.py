"""Desktop grants, native transport ownership, reconnects and app shutdown."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from desktop import servers
from desktop.connection import ConnectionController
from desktop.credentials import DeviceTokenStore
from desktop.execution import ComputerSharing, SharingError
from desktop.probe import (
    PROBE_SERVER_UNREACHABLE,
    PROBE_WEBUI_AVAILABLE,
    DesktopProbeResult,
    DesktopTarget,
)


class Authorization:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.refused: list[str] = []

    def header(self, url: str) -> str:
        self.requests.append(url)
        return "Bearer test-credential"

    def rejected(self, url: str, _header: str | None) -> bool:
        self.refused.append(url)
        return False


class Capability:
    read_only_operations = frozenset({"read", "status"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.active_count = 0
        self.disconnects = 0
        self.closed = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, params))
        if operation == "start":
            self.active_count += 1
        if operation == "held":
            self.entered.set()
            await self.release.wait()
        return {"operation": operation, "count": self.active_count}

    async def disconnected(self) -> None:
        self.disconnects += 1

    async def close(self) -> None:
        self.closed = True
        self.active_count = 0
        self.release.set()


class Socket:
    def __init__(self, server_instance: str = "server-one", *, welcome: bool = True) -> None:
        self.server_instance = server_instance
        self.welcome = welcome
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        await self.outgoing.put(message)
        if message["type"] == "hello" and self.welcome:
            await self.incoming.put(
                {
                    "type": "welcome",
                    "protocol": 1,
                    "computer_id": message["computer_id"],
                    "instance_id": message["instance_id"],
                    "server_instance": self.server_instance,
                }
            )

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def close(self) -> None:
        self.closed = True

    async def call(
        self, call_id: str, operation: str, capability: str = "terminal"
    ) -> dict[str, Any]:
        await self.incoming.put(
            {
                "type": "call",
                "id": call_id,
                "capability": capability,
                "operation": operation,
                "params": {"sentinel": call_id},
            }
        )
        return await asyncio.wait_for(self.outgoing.get(), timeout=2)


class Connections:
    def __init__(self) -> None:
        self.pending: asyncio.Queue[Socket] = asyncio.Queue()
        self.opened: asyncio.Queue[tuple[str, Socket]] = asyncio.Queue()

    @asynccontextmanager
    async def __call__(self, url: str, header: str | None) -> AsyncIterator[Socket]:
        assert header == "Bearer test-credential"
        socket = await self.pending.get()
        await self.opened.put((url, socket))
        try:
            yield socket
        finally:
            await socket.close()

    async def add(self, server_instance: str = "server-one", *, welcome: bool = True) -> Socket:
        socket = Socket(server_instance, welcome=welcome)
        await self.pending.put(socket)
        return socket


def make_sharing(tmp_path: Path) -> tuple[ComputerSharing, Connections, list[Capability]]:
    connection = Connections()
    handlers: list[Capability] = []

    def factory(_emit: Any) -> Capability:
        handler = Capability()
        handlers.append(handler)
        return handler

    return (
        ComputerSharing(
            settings_path=tmp_path / "settings.json",
            identities=DeviceTokenStore(tmp_path / "device-tokens.json"),
            authorization=Authorization(),
            factories={"terminal": factory},
            connector=connection,
        ),
        connection,
        handlers,
    )


def test_grants_and_private_identity_belong_to_one_server(tmp_path: Path) -> None:
    sharing, _connections, _handlers = make_sharing(tmp_path)
    first_url = "https://one.example/"
    sharing.set_server_url(first_url)
    assert sharing.status()["capabilities"] == {"terminal": False, "computer_use": False}
    first = sharing.update({"terminal": True, "name": "My laptop"}, expected_server=first_url)
    assert first["computer_id"].startswith("pc_")
    assert "key" not in first

    sharing.set_server_url("https://two.example/")
    assert sharing.status()["capabilities"]["terminal"] is False
    assert sharing.status()["computer_id"] is None
    with pytest.raises(SharingError) as raised:
        sharing.update({"terminal": True}, expected_server=first_url)
    assert raised.value.error_code == "computer_server_changed"
    sharing.set_server_url(first_url)
    assert sharing.status()["computer_id"] == first["computer_id"]
    assert sharing.status()["capabilities"]["terminal"] is True


@pytest.mark.asyncio
async def test_calls_are_scoped_deduplicated_and_shutdown_stops_native_programs(
    tmp_path: Path,
) -> None:
    sharing, connections, handlers = make_sharing(tmp_path)
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    socket = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        hello = await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        assert hello["capabilities"] == ["terminal"]
        assert hello["key"]
        result = await socket.call("call-one", "start")
        assert result["ok"] is True
        assert sharing.active_terminals == 1
        refused = await socket.call("call-two", "click", "computer_use")
        assert refused["error"]["code"] == "computer_capability_not_shared"
        duplicate = await socket.call("call-one", "start")
        assert duplicate["error"]["code"] == "computer_duplicate_call"
        assert len(handlers[0].calls) == 1
        # Turning Terminal off would stop the running program: it asks first,
        # and without an answer or with a refusal Terminal stays shared.
        asked: list[str] = []

        def refuse(_title: str, message: str) -> bool:
            asked.append(message)
            return False

        for confirm in (None, refuse):
            with pytest.raises(SharingError) as declined:
                sharing.update({"terminal": False}, expected_server=url, confirm=confirm)
            assert declined.value.error_code == "declined"
        assert asked and asked[0].startswith("1 local terminal(s) are still running")
        assert sharing.status()["capabilities"]["terminal"] is True
        assert sharing.prepare_restart() is False
        assert sharing.allow_close(lambda _title, _message: False) is False
        assert (await socket.call("call-three", "status"))["ok"] is True
        assert sharing.allow_close(lambda _title, _message: True) is True
        assert (await socket.call("call-four", "start"))["error"]["code"] == "computer_closing"
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)
    assert handlers[0].closed
    assert socket.closed
    assert sharing.active_terminals == 0


@pytest.mark.parametrize("transition", ["reconnect", "switch", "restart"])
@pytest.mark.asyncio
async def test_a_native_lifetime_survives_only_a_reconnect_to_the_same_server(
    tmp_path: Path, transition: str
) -> None:
    sharing, connections, handlers = make_sharing(tmp_path)
    first_url = "https://one.example/"
    sharing.set_server_url(first_url)
    sharing.update({"terminal": True}, expected_server=first_url)
    first = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        hello = await asyncio.wait_for(first.outgoing.get(), timeout=2)
        assert (await first.call("start-one", "start"))["ok"] is True
        next_instance = "server-two" if transition == "restart" else "server-one"
        second = await connections.add(next_instance)
        if transition == "switch":
            sharing.set_server_url("https://two.example/")
            sharing.update({"terminal": True}, expected_server="https://two.example/")
        else:
            # A settings refresh wakes the reconnect immediately, with no timing sleeps.
            await first.incoming.put(OSError("connection lost"))
            sharing.update({"name": "Laptop renamed"}, expected_server=first_url)
        next_hello = await asyncio.wait_for(second.outgoing.get(), timeout=2)
        if transition == "restart":
            third = await connections.add("server-two")
            next_hello = await asyncio.wait_for(third.outgoing.get(), timeout=2)
            second = third
        assert (await second.call("status-two", "status"))["ok"] is True
        if transition == "reconnect":
            assert len(handlers) == 1
            assert next_hello["instance_id"] == hello["instance_id"]
            assert handlers[0].active_count == 1
            assert handlers[0].closed is False
        else:
            assert len(handlers) == 2
            assert next_hello["instance_id"] != hello["instance_id"]
            assert handlers[0].closed is True
            assert handlers[1].active_count == 0
        assert handlers[0].disconnects >= 1
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.asyncio
async def test_a_window_reconnect_to_the_same_server_keeps_a_healthy_link(
    tmp_path: Path,
) -> None:
    sharing, connections, _handlers = make_sharing(tmp_path)
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    first = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(first.outgoing.get(), timeout=2)
        assert (await first.call("before", "status"))["ok"] is True
        sharing.set_server_url(url)
        for _ in range(20):  # let a wrongly woken loop tear the link down
            await asyncio.sleep(0)
        assert not first.closed
        assert (await first.call("after", "status"))["ok"] is True
        assert connections.opened.qsize() == 1
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.asyncio
async def test_a_server_refusal_reports_the_servers_reason(tmp_path: Path) -> None:
    sharing, connections, _handlers = make_sharing(tmp_path)
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    refused = await connections.add(welcome=False)
    await refused.incoming.put(
        {
            "type": "error",
            "error": {"code": "computer_identity_conflict", "message": "Another installation"},
        }
    )
    running = asyncio.create_task(sharing.run())
    try:
        for _ in range(200):
            if sharing.status()["error_code"]:
                break
            await asyncio.sleep(0)
        assert sharing.status()["error_code"] == "computer_identity_conflict"
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.asyncio
async def test_a_disconnected_call_settles_without_sending_its_reply_to_another_connection(
    tmp_path: Path,
) -> None:
    sharing, connections, handlers = make_sharing(tmp_path)
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    first = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(first.outgoing.get(), timeout=2)
        await first.incoming.put(
            {
                "type": "call",
                "id": "held",
                "capability": "terminal",
                "operation": "held",
                "params": {},
            }
        )
        await asyncio.wait_for(handlers[0].entered.wait(), timeout=2)
        second = await connections.add()
        sharing.update({"name": "Reconnected"}, expected_server=url)
        await asyncio.wait_for(second.outgoing.get(), timeout=2)
        handlers[0].release.set()
        result = await second.call("status", "status")
        assert result["id"] == "status"
        assert first.outgoing.empty()
        assert second.outgoing.empty()
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.parametrize("outcome", ["accepted", "cancelled", "unreachable", "settings_failed"])
@pytest.mark.asyncio
async def test_a_server_switch_preserves_local_work_until_a_ready_target_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    sharing, connections, handlers = make_sharing(tmp_path)
    first_url, second_url = "https://one.example/", "https://two.example/"
    prompts: list[str] = []
    loop = asyncio.get_running_loop()
    prompted = asyncio.Event()
    answer = threading.Event()

    def probe(target: DesktopTarget) -> DesktopProbeResult:
        failed = (
            outcome == "unreachable"
            and target.address is not None
            and target.address.url == second_url
        )
        return DesktopProbeResult(
            status=PROBE_SERVER_UNREACHABLE if failed else PROBE_WEBUI_AVAILABLE, target=target
        )

    def confirm(_title: str, message: str) -> bool:
        prompts.append(message)
        assert sharing.active_terminals == 1
        assert sharing.prepare_restart() is False
        loop.call_soon_threadsafe(prompted.set)
        assert answer.wait(timeout=2)
        return outcome != "cancelled"

    controller = ConnectionController(settings_file=tmp_path / "settings.json", probe=probe)
    controller.set_active_server_listener(sharing.set_server_url)
    controller.set_server_change_guard(lambda url: sharing.changing_server(url, confirm))
    assert controller.prepare_connect("https://one.example", 443).status == PROBE_WEBUI_AVAILABLE
    assert not prompts  # Startup never asks to stop local work.
    sharing.update({"terminal": True}, expected_server=first_url)
    socket = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        assert (await socket.call("start", "start"))["ok"] is True
        if outcome == "settings_failed":

            def fail_save(*_args: Any, **_kwargs: Any) -> None:
                raise OSError("settings write failed")

            monkeypatch.setattr(servers, "select_server", fail_save)
        switching = asyncio.create_task(
            asyncio.to_thread(controller.prepare_connect, "https://two.example", 443)
        )
        if outcome != "unreachable":
            await asyncio.wait_for(prompted.wait(), timeout=2)
            assert (await socket.call("during-read", "read"))["ok"] is True
            assert (await socket.call("during-status", "status"))["ok"] is True
            assert (await socket.call("during-start", "start"))["error"][
                "code"
            ] == "computer_closing"
            answer.set()
        if outcome == "settings_failed":
            with pytest.raises(OSError, match="settings write failed"):
                await asyncio.wait_for(switching, timeout=2)
        else:
            result = await asyncio.wait_for(switching, timeout=2)
            expected = {
                "accepted": PROBE_WEBUI_AVAILABLE,
                "cancelled": "server_switch_cancelled",
                "unreachable": PROBE_SERVER_UNREACHABLE,
            }
            assert result.status == expected[outcome]
        assert len(prompts) == (0 if outcome == "unreachable" else 1)
        if prompts:
            assert "https://two.example" in prompts[0]
        if outcome == "accepted":
            await asyncio.wait_for(handlers[0].release.wait(), timeout=2)
            assert handlers[0].closed
            assert controller.active_server_url() == second_url
            assert sharing.status()["server_url"] == second_url
        else:
            assert (await socket.call("still-running", "status"))["ok"] is True
            assert handlers[0].active_count == 1
            assert not handlers[0].closed
            assert controller.active_server_url() == first_url
            assert sharing.status()["server_url"] == first_url
    finally:
        answer.set()
        controller.close()
        sharing.close()
        await asyncio.wait_for(running, timeout=2)
