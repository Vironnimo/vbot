"""Desktop grants, native transport ownership, reconnects and app shutdown."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast, override

import pytest

from desktop import execution, servers
from desktop.connection import ConnectionController
from desktop.credentials import CredentialStoreError, DeviceTokenStore, StoredComputerIdentity
from desktop.execution import ComputerSharing, SharingError
from desktop.extensions import BeforeReload, DesktopExtensionCatalog, EventSink, NativeCapability
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
        self.capability_updates = True
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
                    "capability_updates": 1 if self.capability_updates else 0,
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


def make_sharing(
    tmp_path: Path, capability: str = "terminal"
) -> tuple[ComputerSharing, Connections, list[Capability]]:
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
            factories={capability: factory},
            connector=connection,
        ),
        connection,
        handlers,
    )


def test_grants_and_private_identity_belong_to_one_server(tmp_path: Path) -> None:
    sharing, _connections, _handlers = make_sharing(tmp_path)
    first_url = "https://one.example/"
    sharing.set_server_url(first_url)
    assert sharing.status()["capabilities"] == {"terminal": False}
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


def test_new_grants_require_a_saved_identity_and_revocation_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    identities = DeviceTokenStore(tmp_path / "device-tokens.json")
    sharing = ComputerSharing(
        settings_path=path,
        identities=identities,
        authorization=Authorization(),
        factories={"terminal": lambda _emit: Capability(), "printer": lambda _emit: Capability()},
    )
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"printer": True}, expected_server=url)
    unchanged = path.read_bytes()
    identity = identities.computer_identity
    unavailable = True

    def store(origin: str, *, create: bool = False) -> StoredComputerIdentity | None:
        if unavailable and create:
            raise CredentialStoreError("Identity store unavailable")
        return identity(origin, create=create)

    monkeypatch.setattr(identities, "computer_identity", store)
    with pytest.raises(CredentialStoreError):
        sharing.update({"terminal": True}, expected_server=url)
    assert sharing.status()["capabilities"] == {"terminal": False, "printer": True}
    assert path.read_bytes() == unchanged

    unavailable = False
    assert sharing.update({"terminal": True}, expected_server=url)["capabilities"]["terminal"]
    unavailable = True
    revoked = sharing.update({"terminal": False}, expected_server=url)
    assert revoked["capabilities"] == {"terminal": False, "printer": True}


@pytest.mark.asyncio
async def test_calls_are_scoped_deduplicated_and_shutdown_stops_native_programs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution, "MAX_REMEMBERED_CALLS", 2)
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
        await socket.incoming.put(
            {
                "type": "call",
                "id": "held-original",
                "capability": "terminal",
                "operation": "held",
                "params": {},
            }
        )
        await handlers[0].entered.wait()
        for request_id in ("fast-one", "fast-two"):
            assert (await socket.call(request_id, "status"))["ok"] is True
        duplicate = await socket.call("held-original", "held")
        assert duplicate["error"]["code"] == "computer_duplicate_call"
        assert sum(operation == "held" for operation, _ in handlers[0].calls) == 1
        handlers[0].release.set()
        completed = await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        assert completed["id"] == "held-original" and completed["ok"] is True
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
@pytest.mark.parametrize("capability", ["terminal", "printer"])
@pytest.mark.asyncio
async def test_a_native_lifetime_survives_only_a_reconnect_to_the_same_server(
    tmp_path: Path, transition: str, capability: str
) -> None:
    sharing, connections, handlers = make_sharing(tmp_path, capability)
    first_url = "https://one.example/"
    sharing.set_server_url(first_url)
    sharing.update({capability: True}, expected_server=first_url)
    first = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        hello = await asyncio.wait_for(first.outgoing.get(), timeout=2)
        assert (await first.call("start-one", "start", capability))["ok"] is True
        next_instance = "server-two" if transition == "restart" else "server-one"
        second = await connections.add(next_instance)
        if transition == "switch":
            sharing.set_server_url("https://two.example/")
            sharing.update({capability: True}, expected_server="https://two.example/")
        else:
            # A settings refresh wakes the reconnect immediately, with no timing sleeps.
            await first.incoming.put(OSError("connection lost"))
            sharing.update({"name": "Laptop renamed"}, expected_server=first_url)
        next_hello = await asyncio.wait_for(second.outgoing.get(), timeout=2)
        if transition == "restart":
            third = await connections.add("server-two")
            next_hello = await asyncio.wait_for(third.outgoing.get(), timeout=2)
            second = third
        assert (await second.call("status-two", "status", capability))["ok"] is True
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
@pytest.mark.parametrize("hook_fails", [False, True], ids=["settings-wake", "failed-hook"])
async def test_reconnect_waits_for_every_disconnected_handler(
    tmp_path: Path, hook_fails: bool
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class Delayed(Capability):
        @override
        async def disconnected(self) -> None:
            started.set()
            await release.wait()
            finished.set()
            await super().disconnected()

    class Failure(Capability):
        @override
        async def disconnected(self) -> None:
            await super().disconnected()
            if hook_fails and self.disconnects == 1:
                raise RuntimeError("native handler failed to disconnect")

    connections = Connections()
    sharing = ComputerSharing(
        settings_path=tmp_path / "settings.json",
        identities=DeviceTokenStore(tmp_path / "device-tokens.json"),
        authorization=Authorization(),
        factories={"printer": lambda _emit: Failure(), "terminal": lambda _emit: Delayed()},
        connector=connections,
    )
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True, "printer": True}, expected_server=url)
    first, next_link = await connections.add(), await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(first.outgoing.get(), timeout=2)
        assert (await first.call("before", "status"))["ok"] is True
        await first.incoming.put(OSError("connection dropped"))
        await asyncio.wait_for(started.wait(), timeout=2)
        sharing.update({"name": "Renamed"}, expected_server=url)
        for _ in range(20):
            await asyncio.sleep(0)
        assert next_link.outgoing.empty()
        release.set()
        assert (await asyncio.wait_for(next_link.outgoing.get(), timeout=2))["name"] == "Renamed"
        assert finished.is_set()
        assert (await next_link.call("after", "status"))["ok"] is True
    finally:
        release.set()
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["name", "credential"])
async def test_registration_changes_during_live_reload_reconnect(
    tmp_path: Path, change: str
) -> None:
    closing = asyncio.Event()
    release = asyncio.Event()

    class Retiring(Capability):
        @override
        async def close(self) -> None:
            closing.set()
            await release.wait()
            await super().close()

    class RotatingAuthorization(Authorization):
        current = "Bearer first-fake"

        @override
        def header(self, url: str) -> str:
            return self.current

    old = NativeCapability(
        "printer", "office", "Printer", "", lambda _emit: Retiring(), source="local", revision="1"
    )
    new = NativeCapability(
        "printer", "office", "Printer", "", lambda _emit: Capability(), source="local", revision="2"
    )

    def reload_catalog(
        _previous: DesktopExtensionCatalog, before_load: BeforeReload
    ) -> DesktopExtensionCatalog:
        before_load(frozenset({old.id}))
        return DesktopExtensionCatalog({new.id: new})

    authorization = RotatingAuthorization()
    connection_headers: list[str | None] = []
    connections = Connections()

    @asynccontextmanager
    async def connector(url: str, header: str | None) -> AsyncIterator[Socket]:
        connection_headers.append(header)
        async with connections(url, "Bearer test-credential") as socket:
            yield socket

    sharing = ComputerSharing(
        settings_path=tmp_path / "settings.json",
        identities=DeviceTokenStore(tmp_path / "device-tokens.json"),
        authorization=authorization,
        factories={old.id: old.factory},
        capabilities={old.id: old},
        connector=connector,
        reload_extensions=reload_catalog,
    )
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({old.id: True, "name": "Original name"}, expected_server=url)
    first, second = await connections.add(), await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        before = await asyncio.wait_for(first.outgoing.get(), timeout=2)
        assert (await first.call("initial", "status", old.id))["ok"] is True
        assert sharing.reload_extensions(expected_server=url)["reloading"]
        await asyncio.wait_for(closing.wait(), timeout=2)
        if change == "name":
            sharing.update({"name": "Renamed"}, expected_server=url)
        else:
            authorization.current = "Bearer replacement-fake"
            sharing.set_server_url(url)
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        after = await asyncio.wait_for(second.outgoing.get(), timeout=2)
        assert after["type"] == "hello"
        assert after["instance_id"] == before["instance_id"]
        assert after["name"] == ("Renamed" if change == "name" else "Original name")
        assert connection_headers == ["Bearer first-fake", authorization.current]
        assert (await second.call("reconnected", "status", new.id))["ok"] is True
        assert first.closed and not sharing.status()["reloading"]
    finally:
        release.set()
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


@pytest.mark.asyncio
async def test_disabling_idle_terminal_fences_a_concurrent_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sharing, connections, handlers = make_sharing(tmp_path)
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    socket = await connections.add()
    running = asyncio.create_task(sharing.run())
    checked = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    original = cast(
        "Callable[[ComputerSharing], int]",
        cast("property", vars(ComputerSharing)["active_terminals"]).fget,
    )
    first = True

    def pause_after_count(instance: ComputerSharing) -> int:
        nonlocal first
        count = original(instance)
        if instance is sharing and first:
            first = False
            loop.call_soon_threadsafe(checked.set)
            assert proceed.wait(timeout=2)
        return count

    try:
        await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        monkeypatch.setattr(ComputerSharing, "active_terminals", property(pause_after_count))
        disabling = asyncio.create_task(
            asyncio.to_thread(sharing.update, {"terminal": False}, expected_server=url)
        )
        await asyncio.wait_for(checked.wait(), timeout=2)
        result = await socket.call("during-disable", "start")
        assert result["error"]["code"] == "computer_closing"
        assert handlers[0].active_count == 0
        proceed.set()
        await asyncio.wait_for(disabling, timeout=2)
        await asyncio.wait_for(handlers[0].release.wait(), timeout=2)
        assert handlers[0].closed
    finally:
        proceed.set()
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.parametrize("outcome", ["accepted", "declined", "settings_failed", "closed"])
@pytest.mark.asyncio
async def test_disabling_terminal_fences_mutations_until_confirmation_and_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    sharing, connections, handlers = make_sharing(tmp_path)
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    socket = await connections.add()
    running = asyncio.create_task(sharing.run())
    prompted = asyncio.Event()
    answer = threading.Event()
    loop = asyncio.get_running_loop()

    def confirm(_title: str, _message: str) -> bool:
        loop.call_soon_threadsafe(prompted.set)
        assert answer.wait(timeout=2)
        return outcome != "declined"

    try:
        await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        assert (await socket.call("initial", "start"))["ok"] is True
        if outcome == "settings_failed":

            def fail_save(*_args: Any, **_kwargs: Any) -> None:
                raise OSError("settings write failed")

            monkeypatch.setattr(execution, "update_section", fail_save)
        disabling = asyncio.create_task(
            asyncio.to_thread(
                sharing.update, {"terminal": False}, expected_server=url, confirm=confirm
            )
        )
        await asyncio.wait_for(prompted.wait(), timeout=2)
        assert (await socket.call("during-read", "read"))["ok"] is True
        assert (await socket.call("during-stop", "kill"))["ok"] is True
        assert (await socket.call("during-start", "start"))["error"]["code"] == "computer_closing"
        assert sharing.prepare_restart() is False
        assert sharing.status()["active_terminals"] == 1
        if outcome == "closed":
            sharing.close()
        answer.set()
        if outcome == "declined":
            with pytest.raises(SharingError) as declined:
                await asyncio.wait_for(disabling, timeout=2)
            assert declined.value.error_code == "declined"
        elif outcome == "settings_failed":
            with pytest.raises(OSError, match="settings write failed"):
                await asyncio.wait_for(disabling, timeout=2)
        elif outcome == "closed":
            with pytest.raises(SharingError) as closed:
                await asyncio.wait_for(disabling, timeout=2)
            assert closed.value.error_code == "computer_server_changed"
        else:
            await asyncio.wait_for(disabling, timeout=2)
            await asyncio.wait_for(handlers[0].release.wait(), timeout=2)
            assert handlers[0].closed
        if outcome in {"declined", "settings_failed"}:
            assert sharing.status()["capabilities"]["terminal"] is True
            assert (await socket.call("after", "start"))["ok"] is True
            assert handlers[0].active_count == 2
    finally:
        answer.set()
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.asyncio
async def test_identity_failure_on_first_connection_retries_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sharing, connections, _handlers = make_sharing(tmp_path)
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    socket = await connections.add()
    identity = sharing._identities.computer_identity
    failures = 0
    unavailable = asyncio.Event()

    def transient_failure(origin: str, *, create: bool = False) -> Any:
        nonlocal failures
        if create and failures == 0:
            failures += 1
            raise OSError("identity temporarily unavailable")
        return identity(origin, create=create)

    monkeypatch.setattr(sharing._identities, "computer_identity", transient_failure)
    sharing._publish = lambda status: unavailable.set() if status["error_code"] else None
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(unavailable.wait(), timeout=2)
        assert not running.done()
        sharing.set_server_url(url)  # Wake the retry without a real backoff wait.
        hello = await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        assert hello["type"] == "hello"
        assert (await socket.call("status", "status"))["ok"] is True
        assert failures == 1
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["factory", "ready", "cleanup", "invalid_handler"])
async def test_failed_capability_initialization_preserves_healthy_native_work_and_can_recover(
    tmp_path: Path, failure: str
) -> None:
    broken = True
    healthy = Capability()
    attempted: list[Capability] = []
    published: list[Mapping[str, Any]] = []
    connections = Connections()

    class UnreadyCapability(Capability):
        async def ready(self) -> str | None:
            if broken and failure == "ready":
                raise RuntimeError("private readiness details")
            return "Device unavailable" if broken else None

        @override
        async def close(self) -> None:
            await super().close()
            if broken and failure == "cleanup":
                raise OSError("private cleanup details")

    def custom_factory(_emit: Any) -> execution.ExecutionHandler:
        if broken and failure == "factory":
            raise ModuleNotFoundError("private import details")
        if broken and failure == "invalid_handler":
            return cast(execution.ExecutionHandler, None)
        handler = UnreadyCapability()
        attempted.append(handler)
        return handler

    sharing = ComputerSharing(
        settings_path=tmp_path / "settings.json",
        identities=DeviceTokenStore(tmp_path / "device-tokens.json"),
        authorization=Authorization(),
        factories={"terminal": lambda _emit: healthy, "z_custom": custom_factory},
        publish=published.append,
        connector=connections,
    )
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    initial = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(initial.outgoing.get(), timeout=2)
        assert (await initial.call("start", "start"))["result"]["count"] == 1
        degraded = await connections.add()
        sharing.update({"z_custom": True}, expected_server=url)
        hello = await asyncio.wait_for(degraded.outgoing.get(), timeout=2)
        assert hello["capabilities"] == ["terminal"]
        assert (await degraded.call("retained", "status"))["result"]["count"] == 1
        expected = {
            "factory": "ModuleNotFoundError",
            "ready": "RuntimeError",
            "cleanup": "Device unavailable; close: OSError",
            "invalid_handler": "TypeError",
        }[failure]
        status = sharing.status()
        assert status["connected"] is True
        assert status["error_code"] is None
        assert status["available"] == {"terminal": True, "z_custom": False}
        assert status["capability_errors"] == {"z_custom": expected}
        assert any(state["capability_errors"] == {"z_custom": expected} for state in published)
        assert all(handler.closed for handler in attempted)
        assert not healthy.closed

        broken = False
        recovered = await connections.add()
        disabled = sharing.update({"z_custom": False}, expected_server=url)
        assert disabled["available"]["z_custom"] is True
        sharing.update({"z_custom": True}, expected_server=url)
        hello = await asyncio.wait_for(recovered.outgoing.get(), timeout=2)
        assert hello["capabilities"] == ["terminal", "z_custom"]
        assert (await recovered.call("recovered", "status", "z_custom"))["ok"] is True
        assert (await recovered.call("still_retained", "status"))["result"]["count"] == 1
        assert sharing.status()["capability_errors"] == {}
        assert not healthy.closed
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)
    assert healthy.closed
    assert all(handler.closed for handler in attempted)


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_during_scan", [False, True])
async def test_native_reload_refuses_legacy_servers_before_and_after_scanning(
    tmp_path: Path,
    connect_during_scan: bool,
) -> None:
    loop = asyncio.get_running_loop()
    scanned = asyncio.Event()
    release_scan = threading.Event()
    connections = Connections()
    handler = Capability()
    declaration = NativeCapability("terminal", "terminal", "Terminal", "", lambda _emit: handler)
    catalog = DesktopExtensionCatalog({"terminal": declaration})

    def scan(
        _previous: DesktopExtensionCatalog, before_load: BeforeReload
    ) -> DesktopExtensionCatalog:
        loop.call_soon_threadsafe(scanned.set)
        assert release_scan.wait(2)
        before_load(frozenset())
        return catalog

    sharing = ComputerSharing(
        settings_path=tmp_path / "settings.json",
        identities=DeviceTokenStore(tmp_path / "device-tokens.json"),
        authorization=Authorization(),
        factories=catalog.factories,
        capabilities=catalog.capabilities,
        connector=connections,
        reload_extensions=scan,
    )
    url = "https://legacy.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True}, expected_server=url)
    socket = await connections.add()
    socket.capability_updates = False
    reloading: asyncio.Task[dict[str, Any]] | None = None
    if connect_during_scan:
        reloading = asyncio.create_task(
            asyncio.to_thread(sharing.reload_extensions, expected_server=url)
        )
        await asyncio.wait_for(scanned.wait(), timeout=2)
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        assert (await socket.call("started", "start"))["ok"]
        assert not sharing.status()["reload_supported"]
        release_scan.set()
        with pytest.raises(SharingError) as refused:
            if reloading is not None:
                await reloading
            else:
                sharing.reload_extensions(expected_server=url)
        assert refused.value.error_code == "computer_reload_unavailable"
        assert scanned.is_set() is connect_during_scan
        assert not socket.closed and handler.disconnects == 0
        assert (await socket.call("retained_legacy", "status"))["result"]["count"] == 1
    finally:
        release_scan.set()
        sharing.close()
        await asyncio.wait_for(running, timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("retire_error", [False, True, "cancel"])
async def test_local_reload_retires_changed_work_preserves_other_handlers_and_binds_grants(
    tmp_path: Path,
    retire_error: bool | str,
) -> None:
    connections = Connections()
    healthy = Capability()
    instances: list[Capability] = []
    sinks: list[EventSink] = []
    custom_id = "extension:office:printer"
    fail_after_import = False

    class ReloadCapability(Capability):
        @override
        async def close(self) -> None:
            if retire_error and self is instances[0]:
                if retire_error == "cancel":
                    raise asyncio.CancelledError
                raise OSError("retirement unavailable")
            await super().close()

    def custom_factory(emit: EventSink) -> Capability:
        handler = ReloadCapability()
        instances.append(handler)
        sinks.append(emit)
        return handler

    terminal = NativeCapability(
        "terminal",
        "terminal",
        "Terminal",
        "",
        lambda _emit: healthy,
        source="builtin:terminal",
        revision="stable",
    )
    original = NativeCapability(
        custom_id, "office", "Printer", "", custom_factory, source="local-package", revision="first"
    )
    catalog = DesktopExtensionCatalog({"terminal": terminal, custom_id: original})
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "computer_sharing": {
                    "servers": {"https://one.example": {"terminal": True, "printer": True}}
                }
            }
        )
    )

    def reload_catalog(
        previous: DesktopExtensionCatalog, before_load: BeforeReload
    ) -> DesktopExtensionCatalog:
        previous_terminal = previous.capabilities.get("terminal")
        next_terminal = catalog.capabilities.get("terminal")
        before_load(
            frozenset({"terminal"})
            if previous_terminal is not None
            and (next_terminal is None or next_terminal.identity != previous_terminal.identity)
            else frozenset()
        )
        if fail_after_import:
            assert sharing.allow_close(lambda _title, _message: True)
        return catalog

    sharing = ComputerSharing(
        settings_path=tmp_path / "settings.json",
        identities=DeviceTokenStore(tmp_path / "device-tokens.json"),
        authorization=Authorization(),
        factories=catalog.factories,
        capabilities=catalog.capabilities,
        connector=connections,
        reload_extensions=reload_catalog,
    )
    url = "https://one.example/"
    sharing.set_server_url(url)
    assert sharing.status()["capabilities"] == {"terminal": True, custom_id: False}
    sharing.update({"terminal": True, custom_id: True}, expected_server=url)
    initial = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        assert (await asyncio.wait_for(initial.outgoing.get(), timeout=2))["type"] == "hello"
        await initial.call("start", "start")
        assert not sharing.reload_extensions(expected_server=url)["reloading"]
        assert sharing.status()["connected"]
        assert (await initial.call("unchanged", "status"))["ok"]
        await initial.incoming.put(
            {
                "type": "call",
                "id": "held",
                "capability": custom_id,
                "operation": "held",
                "params": {},
            }
        )
        await asyncio.wait_for(instances[0].entered.wait(), timeout=2)
        await initial.incoming.put(
            {
                "type": "call",
                "id": "unchanged_held",
                "capability": "terminal",
                "operation": "held",
                "params": {},
            }
        )
        await asyncio.wait_for(healthy.entered.wait(), timeout=2)
        edited = NativeCapability(
            custom_id,
            "office",
            "Edited",
            "",
            custom_factory,
            source=original.source,
            revision="second",
        )
        added_id = "extension:office:new"
        added = NativeCapability(
            added_id, "office", "New", "", custom_factory, source=original.source, revision="second"
        )
        catalog = DesktopExtensionCatalog(
            {"terminal": terminal, custom_id: edited, added_id: added}
        )
        pending = sharing.reload_extensions(expected_server=url)
        assert pending["reloading"]
        assert pending["capabilities"] == {"terminal": True, custom_id: True, added_id: False}
        if retire_error:
            assert await asyncio.wait_for(initial.outgoing.get(), timeout=2) == {
                "type": "capabilities",
                "capabilities": ["terminal"],
            }
            assert sharing.status()["capability_errors"][custom_id] == (
                "close: CancelledError" if retire_error == "cancel" else "close: OSError"
            )
            assert len(instances) == 1
            assert (await initial.call("retirement_failed", "status"))["result"]["count"] == 1
            retire_error = False
            sharing.reload_extensions(expected_server=url)
        retired_result = await asyncio.wait_for(initial.outgoing.get(), timeout=2)
        assert (
            retired_result["type"] == "result"
            and retired_result["id"] == "held"
            and retired_result["ok"]
        )
        assert await asyncio.wait_for(initial.outgoing.get(), timeout=2) == {
            "type": "capabilities",
            "capabilities": sorted(["terminal", custom_id]),
        }
        assert not initial.closed and healthy.disconnects == 0
        healthy.release.set()
        retained_result = await asyncio.wait_for(initial.outgoing.get(), timeout=2)
        assert (
            retained_result["type"] == "result"
            and retained_result["id"] == "unchanged_held"
            and retained_result["ok"]
        )
        assert instances[0].closed and len(instances) == 2
        assert not healthy.closed
        assert not sharing.status()["reloading"]
        sinks[0]("stale", {})
        assert (await initial.call("retained", "status"))["result"]["count"] == 1
        assert (await initial.call("new_version", "status", custom_id))["ok"]

        # A different local package with the same folder identity cannot inherit consent.
        copied = NativeCapability(
            custom_id,
            "office",
            "Other package",
            "",
            custom_factory,
            source="different-package",
            revision="third",
        )
        catalog = DesktopExtensionCatalog({"terminal": terminal, custom_id: copied})
        status = sharing.reload_extensions(expected_server=url)
        assert status["capabilities"][custom_id] is False
        assert (await asyncio.wait_for(initial.outgoing.get(), timeout=2))["capabilities"] == [
            "terminal"
        ]
        assert instances[1].closed
        assert not healthy.closed
        with pytest.raises(SharingError) as refused:
            sharing.update(
                {custom_id: True},
                expected_server=url,
                expected_revision=pending["catalog_revision"],
            )
        assert refused.value.error_code == "computer_catalog_changed"
        stored = json.loads((tmp_path / "settings.json").read_text())["computer_sharing"]
        assert stored["servers"]["https://one.example"][custom_id] is True
        assert stored["servers"]["https://one.example"]["printer"] is True
        assert stored["capability_sources"]["https://one.example"][custom_id] == "local-package"
        catalog = DesktopExtensionCatalog({})  # Removing a running Terminal needs local consent.

        def failed_confirmation(_title: str, _message: str) -> bool:
            raise RuntimeError("dialog unavailable")

        for confirm in (None, lambda _title, _message: False, failed_confirmation):
            with pytest.raises(SharingError) as declined:
                sharing.reload_extensions(expected_server=url, confirm=confirm)
            assert declined.value.error_code == "declined"
        assert not healthy.closed
        assert (await initial.call("declined_reload", "status"))["result"]["count"] == 1
        # Imports alone cannot advance the accepted fingerprint baseline. A
        # concurrent native fence after import rejects publication; trying again
        # must still confirm the changed, running Terminal package.
        catalog = DesktopExtensionCatalog(
            {
                "terminal": NativeCapability(
                    "terminal",
                    "terminal",
                    "Changed",
                    "",
                    lambda _emit: Capability(),
                    source=terminal.source,
                    revision="changed",
                )
            }
        )
        confirmed = []

        def accept(_title: str, _message: str) -> bool:
            confirmed.append(True)
            return True

        revision = sharing.status()["catalog_revision"]
        fail_after_import = True
        for _attempt in range(2):
            with pytest.raises(SharingError) as rejected:
                sharing.reload_extensions(expected_server=url, confirm=accept)
            assert rejected.value.error_code == "computer_server_changed"
            assert sharing.status()["catalog_revision"] == revision
            assert not healthy.closed and healthy.active_count == 1
            sharing.resume()
        assert len(confirmed) == 2
        fail_after_import = False
        sharing.reload_extensions(expected_server=url, confirm=accept)
        assert len(confirmed) == 3
        update = await asyncio.wait_for(initial.outgoing.get(), timeout=2)
        assert update == {"type": "capabilities", "capabilities": ["terminal"]}
    finally:
        sharing.close()
        await asyncio.wait_for(running, timeout=2)
    assert healthy.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("hang_in_close", [False, True])
async def test_reload_quarantines_unsettled_handlers_with_one_bounded_wait_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hang_in_close: bool,
) -> None:
    connections = Connections()
    healthy = Capability()
    release_close = asyncio.Event()
    closed = [asyncio.Event(), asyncio.Event()]
    close_counts = [0, 0]
    handlers: list[Capability] = []
    ids = ("extension:office:first", "extension:office:second")

    class SlowCapability(Capability):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

        @override
        async def close(self) -> None:
            close_counts[self.index] += 1
            closed[self.index].set()
            if hang_in_close:
                await release_close.wait()
            self.closed = True
            # A noncooperative operation can outlive close as well. Deliberately
            # leave it running so retirement must retain its exact owner.

    def factory(index: int) -> Callable[[EventSink], Capability]:
        def create(_emit: EventSink) -> Capability:
            handler = SlowCapability(index)
            handlers.append(handler)
            return handler

        return create

    terminal = NativeCapability("terminal", "terminal", "Terminal", "", lambda _emit: healthy)
    catalog = DesktopExtensionCatalog(
        {
            "terminal": terminal,
            **{
                name: NativeCapability(name, "office", name, "", factory(index), revision="first")
                for index, name in enumerate(ids)
            },
        }
    )

    def reload_catalog(
        _previous: DesktopExtensionCatalog, before_load: BeforeReload
    ) -> DesktopExtensionCatalog:
        before_load(frozenset(ids))
        return catalog

    waits: list[tuple[asyncio.Task[None], ...]] = []

    async def controlled_wait(tasks: Any, *, timeout: float) -> Any:
        assert timeout == execution.RETIREMENT_TIMEOUT
        snapshot = tuple(tasks)
        waits.append(snapshot)
        await asyncio.gather(*(event.wait() for event in closed))
        # Exhaust this one shared deadline deterministically. Every old task is
        # still owned, not cancelled; another reload observes the same tasks.
        return (
            {task for task in snapshot if task.done()},
            {task for task in snapshot if not task.done()},
        )

    monkeypatch.setattr(execution, "_wait_for_retirement", controlled_wait)
    sharing = ComputerSharing(
        settings_path=tmp_path / "settings.json",
        identities=DeviceTokenStore(tmp_path / "device-tokens.json"),
        authorization=Authorization(),
        factories=catalog.factories,
        capabilities=catalog.capabilities,
        connector=connections,
        reload_extensions=reload_catalog,
    )
    url = "https://one.example/"
    sharing.set_server_url(url)
    sharing.update({"terminal": True, **dict.fromkeys(ids, True)}, expected_server=url)
    socket = await connections.add()
    running = asyncio.create_task(sharing.run())
    try:
        await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        for index, name in enumerate(ids):
            await socket.incoming.put(
                {
                    "type": "call",
                    "id": str(index),
                    "capability": name,
                    "operation": "held",
                    "params": {},
                }
            )
        await asyncio.wait_for(
            asyncio.gather(*(handler.entered.wait() for handler in handlers)), timeout=2
        )
        catalog = DesktopExtensionCatalog(
            {
                "terminal": terminal,
                **{
                    name: NativeCapability(
                        name, "office", name, "", factory(index), revision="second"
                    )
                    for index, name in enumerate(ids)
                },
            }
        )
        for attempt in range(2):
            assert sharing.reload_extensions(expected_server=url)["reloading"]
            update = await asyncio.wait_for(socket.outgoing.get(), timeout=2)
            assert update == {"type": "capabilities", "capabilities": ["terminal"]}
            status = sharing.status()
            assert not status["reloading"]
            assert set(status["capability_errors"]) == set(ids)
            assert all(not status["available"][name] for name in ids)
            assert len(handlers) == 2 and close_counts == [1, 1]
            assert (await socket.call(f"healthy-{attempt}", "status"))["ok"]
        assert len(waits) == 2 and set(waits[0]) == set(waits[1])
        assert all(not task.cancelled() for task in waits[0])
        release_close.set()
        for handler in handlers:
            handler.release.set()
        results = [await asyncio.wait_for(socket.outgoing.get(), timeout=2) for _ in ids]
        assert {result["id"] for result in results} == {"0", "1"}
        assert all(result["ok"] for result in results)
        await asyncio.gather(*waits[0])
        sharing.reload_extensions(expected_server=url)
        update = await asyncio.wait_for(socket.outgoing.get(), timeout=2)
        assert update["capabilities"] == sorted(["terminal", *ids])
        assert not sharing.status()["capability_errors"]
        assert len(handlers) == 4 and close_counts == [1, 1]
        assert not socket.closed and healthy.disconnects == 0
    finally:
        release_close.set()
        for handler in handlers:
            handler.release.set()
        sharing.close()
        await asyncio.wait_for(running, timeout=2)
