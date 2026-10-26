"""A Desktop PTY shares the normal Terminal contract without local PID/path access."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.computers import ComputerRegistry
from core.tools.terminal_manager import TerminalManager, TerminalRenderHost
from desktop import terminal_execution
from tests.core.permissions.command_check_support import restricted
from tests.core.tools.terminal_helpers import call, make_context
from tests.core.tools.terminal_manager_helpers import AdapterFactory, FakeTree, eventually


class Link:
    """The public computer broker wire over in-memory frames, with a native fake PTY."""

    def __init__(self) -> None:
        self.registry = ComputerRegistry()
        self.native = terminal_execution.TerminalCapability()
        self.hello = {
            "type": "hello",
            "protocol": 1,
            "computer_id": "pc_laptop",
            "name": "Laptop",
            "platform": "win32",
            "capabilities": ["terminal"],
            "instance_id": "desktop_a",
            "key": "a" * 64,
        }
        self.connection: Any = None
        self.pumps: list[asyncio.Task[None]] = []
        self.operations: set[asyncio.Task[None]] = set()
        self.drop_start_reply = False
        self.drop_reply: str | None = None
        self.fail_next: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = []

    async def connect(self) -> None:
        self.connection = await self.registry.connect(self.hello, actor="device")
        self.pumps.append(asyncio.create_task(self._pump(self.connection)))

    async def _pump(self, connection: Any) -> None:
        while (request := await connection.outgoing.get()) is not None:
            task = asyncio.create_task(self._answer(connection, request))
            self.operations.add(task)
            task.add_done_callback(self.operations.discard)

    async def _answer(self, connection: Any, request: dict[str, Any]) -> None:
        self.requests.append(request)
        if code := self.fail_next.pop(request["operation"], None):
            self.registry.receive(
                connection,
                {
                    "type": "result",
                    "id": request["id"],
                    "ok": False,
                    "error": {"code": code, "message": "Controlled transport interruption"},
                },
            )
            return
        try:
            result = await self.native.execute(request["operation"], request["params"])
            response = {"ok": True, "result": result}
        except Exception as error:
            response = {
                "ok": False,
                "error": {
                    "code": getattr(error, "code", "computer_execution_error"),
                    "message": str(error),
                },
            }
        if request["operation"] == "start" and self.drop_start_reply:
            self.drop_start_reply = False
            self.registry.disconnect(connection)
            return
        if request["operation"] == self.drop_reply:
            self.drop_reply = None
            self.registry.disconnect(connection)
            return
        self.registry.receive(connection, {"type": "result", "id": request["id"], **response})

    async def close(self) -> None:
        self.registry.close()
        await self.native.close()
        await asyncio.gather(*self.pumps)
        if self.operations:
            await asyncio.gather(*tuple(self.operations))


@pytest_asyncio.fixture
async def remote(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[TerminalManager, Link, AdapterFactory]]:
    factory = AdapterFactory("ready> ")
    monkeypatch.setattr(terminal_execution, "spawn_terminal_adapter", factory)
    monkeypatch.setattr(terminal_execution, "default_terminal_argv", lambda _env: ["host-shell"])
    monkeypatch.setattr(terminal_execution, "terminal_environment", lambda: {"PATH": ""})
    monkeypatch.setattr(terminal_execution, "observe_process_tree", lambda _pid: FakeTree())
    monkeypatch.setattr(
        terminal_execution, "terminate_process_tree", lambda adapter, **_kw: adapter.terminate()
    )
    link = Link()
    await link.connect()
    manager = TerminalManager(
        computers=link.registry,
        render_host=TerminalRenderHost.in_process(),
        activity_quiet_seconds=0.01,
        idle_seconds=0.02,
        start_wait_seconds=0.05,
    )
    try:
        yield manager, link, factory
    finally:
        await manager.aclose()
        await link.close()


def _context(path: Path) -> Any:
    return replace(
        make_context(path),
        execution={"computer_id": "pc_laptop", "allowed_computer_ids": ["pc_laptop"]},
    )


@pytest.mark.asyncio
async def test_remote_terminal_keeps_its_computer_and_enforces_session_grants(
    remote: tuple[TerminalManager, Link, AdapterFactory],
    tmp_path: Path,
) -> None:
    manager, link, factory = remote
    context = _context(tmp_path)
    started = await call(manager, context, {"action": "start", "command": "editor"})
    assert started["ok"], started
    terminal_id = started["data"]["terminal_id"]
    assert factory.calls[0][0] == ["editor"]
    assert factory.calls[0][1] == Path.home()
    assert started["data"]["computer_id"] == "pc_laptop"
    assert manager.list_for_operator()[0]["computer_name"] == "Laptop"
    context = replace(
        context,
        execution={"computer_id": "server", "allowed_computer_ids": ["server", "pc_laptop"]},
    )
    sent = await call(
        manager, context, {"action": "input", "terminal_id": terminal_id, "data": "hello"}
    )
    assert sent["ok"], sent
    assert "hello" in "".join(factory.adapters[0].writes)
    context = replace(
        context, execution={"computer_id": "server", "allowed_computer_ids": ["server"]}
    )
    for action in ("status", "input", "wait", "kill", "attach", "detach", "resize"):
        refused = await call(manager, context, {"action": action, "terminal_id": terminal_id})
        assert refused["error"]["code"] == "computer_not_allowed", refused
    assert (await call(manager, context, {"action": "list"}))["data"]["terminals"] == []
    assert factory.adapters[0].alive


@pytest.mark.asyncio
async def test_remote_terminal_replays_disconnected_output_and_never_restarts_unknown_launch(
    remote: tuple[TerminalManager, Link, AdapterFactory],
    tmp_path: Path,
) -> None:
    manager, link, factory = remote
    context = _context(tmp_path)
    link.drop_start_reply = True
    started = await call(manager, context, {"action": "start", "command": "editor"})
    assert started["ok"], started
    terminal_id = started["data"]["terminal_id"]
    assert started["data"]["launch_pending"]
    assert not manager.list_for_operator()[0]["computer_online"]
    factory.adapters[0].emit("\r\noffline output")
    refused = await call(
        manager, context, {"action": "input", "terminal_id": terminal_id, "data": "unsafe"}
    )
    assert refused["error"]["code"] == "computer_offline"
    assert factory.adapters[0].writes == []
    await link.connect()

    async def replayed() -> bool:
        return "offline output" in (await manager.read_for_operator(terminal_id))["screen"]

    await eventually(replayed)
    assert len(factory.adapters) == 1
    assert not manager.list_for_operator()[0]["launch_pending"]
    stopped = await call(manager, context, {"action": "kill", "terminal_id": terminal_id})
    assert stopped["ok"], stopped
    assert not factory.adapters[0].alive


@pytest.mark.asyncio
async def test_remote_terminal_refuses_server_permission_rules_before_start(
    remote: tuple[TerminalManager, Link, AdapterFactory],
    tmp_path: Path,
) -> None:
    manager, _link, factory = remote
    context = _context(tmp_path)
    context = replace(context, permissions=restricted(tmp_path))
    result = await call(manager, context, {"action": "start", "command": "editor"})
    assert result["error"]["code"] == "permission_denied", result
    assert factory.adapters == []


@pytest.mark.asyncio
async def test_pending_native_spawn_is_owned_during_reconnect_and_close(
    remote: tuple[TerminalManager, Link, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, link, factory = remote
    entered, finish_spawn = threading.Event(), threading.Event()
    native_start = link.native._start

    def delayed_start(params: dict[str, Any]) -> Any:
        entered.set()
        assert finish_spawn.wait(2)
        return native_start(params)

    monkeypatch.setattr(link.native, "_start", delayed_start)
    launching = asyncio.create_task(
        call(manager, _context(tmp_path), {"action": "start", "command": "editor"})
    )
    assert await asyncio.to_thread(entered.wait, 2)
    link.registry.disconnect(link.connection)
    started = await launching
    terminal_id = started["data"]["terminal_id"]
    process_id = next(r["params"]["process_id"] for r in link.requests if r["operation"] == "start")
    await link.connect()
    reading = asyncio.create_task(
        link.native.execute("read", {"process_id": process_id, "offset": 0})
    )
    checking = asyncio.create_task(link.native.execute("status", {"process_id": process_id}))
    await asyncio.sleep(0)
    assert not reading.done() and not checking.done()
    assert not manager.list_for_operator()[0]["finished_at"]
    finish_spawn.set()
    read, status = await asyncio.gather(reading, checking)
    assert not read["eof"] and status["alive"]
    assert len(factory.adapters) == 1
    await link.native.close()
    assert not factory.adapters[0].alive
    await eventually(lambda: manager.list_for_operator()[0]["finished_at"] is not None)
    assert manager.list_for_operator()[0]["terminal_id"] == terminal_id


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", ["computer_timeout", "computer_closing"])
async def test_remote_io_recovers_safe_reads_and_does_not_replay_unconfirmed_input(
    remote: tuple[TerminalManager, Link, AdapterFactory],
    tmp_path: Path,
    transient: str,
) -> None:
    manager, link, factory = remote
    context = _context(tmp_path)
    started = await call(manager, context, {"action": "start", "command": "editor"})
    terminal_id = started["data"]["terminal_id"]
    link.fail_next["read"] = transient
    link.fail_next["status"] = transient
    factory.adapters[0].emit("\r\ncontinued output")

    async def replayed() -> bool:
        return "continued output" in (await manager.read_for_operator(terminal_id))["screen"]

    await eventually(replayed)
    link.drop_reply = "write"
    result = await call(
        manager, context, {"action": "input", "terminal_id": terminal_id, "data": "once"}
    )
    assert result["error"]["code"] == "computer_connection_lost", result
    assert factory.adapters[0].writes == ["once"]
    await link.connect()
    assert factory.adapters[0].writes == ["once"]
    link.fail_next["release"] = "computer_connection_lost"
    factory.adapters[0].finish(0)
    await eventually(lambda: manager.list_for_operator()[0]["finished_at"] is not None)
    assert link.native._processes  # the failed release did not abort the server's exit state
    await link.connect()
    await eventually(lambda: not link.native._processes)


@pytest.mark.asyncio
async def test_replacement_desktop_cannot_receive_an_old_terminal_input(
    remote: tuple[TerminalManager, Link, AdapterFactory],
    tmp_path: Path,
) -> None:
    manager, link, factory = remote
    context = _context(tmp_path)
    started = await call(manager, context, {"action": "start", "command": "editor"})
    terminal_id = started["data"]["terminal_id"]
    link.registry.disconnect(link.connection)
    await link.native.close()
    link.native = terminal_execution.TerminalCapability()
    link.hello["instance_id"] = "desktop_b"
    await link.connect()
    result = await call(
        manager, context, {"action": "input", "terminal_id": terminal_id, "data": "never"}
    )
    assert not result["ok"]
    assert factory.adapters[0].writes == []
    await eventually(lambda: manager.list_for_operator()[0]["finished_at"] is not None)
    assert link.native.active_count == 0


@pytest.mark.asyncio
async def test_native_close_drains_a_cancelled_pending_launch(
    remote: tuple[TerminalManager, Link, AdapterFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manager, link, factory = remote
    entered, finish_spawn = threading.Event(), threading.Event()
    native_start = link.native._start

    def delayed_start(params: dict[str, Any]) -> Any:
        entered.set()
        assert finish_spawn.wait(2)
        return native_start(params)

    monkeypatch.setattr(link.native, "_start", delayed_start)
    launching = asyncio.create_task(
        link.native.execute("start", {"process_id": "pending", "command": "editor"})
    )
    assert await asyncio.to_thread(entered.wait, 2)
    launching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launching
    closing = asyncio.create_task(link.native.close())
    await asyncio.sleep(0)
    assert not closing.done()
    finish_spawn.set()
    await closing
    assert len(factory.adapters) == 1
    assert not factory.adapters[0].alive
    assert factory.adapters[0].closed
    assert link.native.active_count == 0
