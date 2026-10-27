"""Only the operator removes an offline computer registration."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from core.computers import ComputerRegistry
from server._app_lifecycle import _register_computer_change_bridge
from server.access import bound_principal
from server.access._principal import HOST
from server.events import ServerEventBus
from tests.core.computers.test_computers import hello
from tests.server.rpc_test_support import StubAdapter, make_state, rpc_error, rpc_result


@pytest.mark.asyncio
async def test_only_an_operator_removes_an_offline_computer(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.event_bus = ServerEventBus()
    registry = ComputerRegistry()
    state.runtime.computers = registry
    connection = await registry.connect(hello(), actor="host")
    unsubscribe = _register_computer_change_bridge(state)

    refused = await rpc_error(state, "computers.remove", id="pc_laptop")
    assert refused["code"] == "access_denied"
    with bound_principal(HOST):
        online = await rpc_error(state, "computers.remove", id="pc_laptop")
        assert online["code"] == "computer_online"
        registry.disconnect(connection)
        assert await rpc_result(state, "computers.remove", id="pc_laptop") == {
            "removed": "pc_laptop"
        }
    listed = await rpc_result(state, "computers.list")
    assert [item["id"] for item in listed["computers"]] == ["server"]
    assert [event["payload"] for event in state.event_bus.events] == [
        {"kind": "computers"},
        {"kind": "computers"},
    ]
    unsubscribe()
    registry.close()


@pytest.mark.asyncio
async def test_cancelled_remove_publishes_the_completed_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.event_bus = ServerEventBus()
    registry = ComputerRegistry(tmp_path)
    state.runtime.computers = registry
    connection = await registry.connect(hello(), actor="host")
    registry.disconnect(connection)
    unsubscribe = _register_computer_change_bridge(state)
    assert registry._store is not None
    forget = registry._store.forget
    loop = asyncio.get_running_loop()
    started = loop.create_future()
    release = threading.Event()

    def delayed_forget(computer_id: str) -> None:
        loop.call_soon_threadsafe(started.set_result, None)
        assert release.wait(5)
        forget(computer_id)

    monkeypatch.setattr(registry._store, "forget", delayed_forget)

    async def remove() -> None:
        with bound_principal(HOST):
            await rpc_result(state, "computers.remove", id="pc_laptop")

    task = asyncio.create_task(remove())
    try:
        await asyncio.wait_for(started, 2)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    listed = await rpc_result(state, "computers.list")
    assert [item["id"] for item in listed["computers"]] == ["server"]
    assert [event["payload"] for event in state.event_bus.events] == [{"kind": "computers"}]
    unsubscribe()
    registry.close()
