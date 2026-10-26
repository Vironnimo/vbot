"""Only the operator removes an offline computer registration."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.computers import ComputerRegistry
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
    assert [event["payload"] for event in state.event_bus.events] == [{"kind": "computers"}]
    registry.close()
