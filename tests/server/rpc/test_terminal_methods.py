"""Tests for local operator RPC control of interactive Terminal Sessions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.terminal_methods import (
    _terminal_input,
    _terminal_kill,
    _terminal_list,
    _terminal_resize,
)


class FakeTerminalManager:
    def __init__(self) -> None:
        self.inputs: list[tuple[str, str]] = []
        self.resizes: list[tuple[str, int, int]] = []
        self.kills: list[str] = []

    def list_active_for_operator(self) -> list[dict[str, Any]]:
        return [{"terminal_id": "term-1", "state": "working"}]

    async def send_operator_input(self, terminal_id: str, data: str) -> dict[str, Any]:
        self.inputs.append((terminal_id, data))
        return {"terminal_id": terminal_id, "state": "working"}

    async def resize_for_operator(
        self, terminal_id: str, *, columns: int, rows: int
    ) -> dict[str, Any]:
        self.resizes.append((terminal_id, columns, rows))
        return {"terminal_id": terminal_id, "columns": columns, "rows": rows}

    async def kill_for_operator(self, terminal_id: str) -> dict[str, Any]:
        self.kills.append(terminal_id)
        return {"terminal_id": terminal_id, "state": "exited"}


def _state(manager: FakeTerminalManager) -> SimpleNamespace:
    return SimpleNamespace(runtime=SimpleNamespace(terminal_manager=manager))


@pytest.mark.asyncio
async def test_terminal_operator_handlers_project_list_input_resize_and_kill() -> None:
    manager = FakeTerminalManager()
    state = _state(manager)

    assert _terminal_list(state, {}) == {
        "terminals": [{"terminal_id": "term-1", "state": "working"}]
    }
    assert await _terminal_input(state, {"terminal_id": "term-1", "data": "status\r"}) == {
        "terminal": {"terminal_id": "term-1", "state": "working"}
    }
    assert await _terminal_resize(state, {"terminal_id": "term-1", "columns": 100, "rows": 30}) == {
        "terminal": {
            "terminal_id": "term-1",
            "columns": 100,
            "rows": 30,
        }
    }
    assert await _terminal_kill(state, {"terminal_id": "term-1"}) == {
        "terminal": {"terminal_id": "term-1", "state": "exited"}
    }
    assert manager.inputs == [("term-1", "status\r")]
    assert manager.resizes == [("term-1", 100, 30)]
    assert manager.kills == ["term-1"]


@pytest.mark.asyncio
async def test_terminal_operator_handlers_validate_and_register_contract() -> None:
    manager = FakeTerminalManager()
    state = _state(manager)

    with pytest.raises(RpcError, match="does not accept params"):
        _terminal_list(state, {"extra": True})
    with pytest.raises(RpcError, match="params.columns must be an integer"):
        await _terminal_resize(state, {"terminal_id": "term-1", "columns": True, "rows": 30})

    handlers = build_method_handlers()
    assert {"terminal.list", "terminal.input", "terminal.resize", "terminal.kill"} <= set(handlers)
