"""Tests for local operator RPC control of interactive Terminal Sessions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.terminal_methods import (
    _terminal_forget,
    _terminal_input,
    _terminal_kill,
    _terminal_list,
    _terminal_resize,
    _terminal_start,
)


class FakeTerminalManager:
    def __init__(self) -> None:
        self.inputs: list[tuple[str, str]] = []
        self.resizes: list[tuple[str, int, int]] = []
        self.kills: list[str] = []
        self.forgotten: list[str] = []
        self.starts: list[dict[str, Any]] = []

    def list_for_operator(self) -> list[dict[str, Any]]:
        return [{"terminal_id": "term-1", "state": "working"}]

    def list_operator_launch_history(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "launch-1",
                "command": "codex",
                "args": ["--profile", "work"],
                "workdir": "~/repo",
                "used_at": "2026-08-08T10:00:00+00:00",
            }
        ]

    async def send_operator_input(self, terminal_id: str, data: str) -> dict[str, Any]:
        self.inputs.append((terminal_id, data))
        return {"terminal_id": terminal_id, "state": "working"}

    async def spawn_for_operator(self, **kwargs: Any) -> dict[str, Any]:
        self.starts.append(kwargs)
        return {"terminal_id": "manual-1", "state": "ready", "owner": None}

    async def resize_for_operator(
        self, terminal_id: str, *, columns: int, rows: int
    ) -> dict[str, Any]:
        self.resizes.append((terminal_id, columns, rows))
        return {"terminal_id": terminal_id, "columns": columns, "rows": rows}

    async def kill_for_operator(self, terminal_id: str) -> dict[str, Any]:
        self.kills.append(terminal_id)
        return {"terminal_id": terminal_id, "state": "exited"}

    def forget_for_operator(self, terminal_id: str) -> dict[str, Any]:
        self.forgotten.append(terminal_id)
        return {"terminal_id": terminal_id, "state": "exited"}


def _state(manager: FakeTerminalManager) -> SimpleNamespace:
    return SimpleNamespace(runtime=SimpleNamespace(terminal_manager=manager))


@pytest.mark.asyncio
async def test_terminal_operator_handlers_project_list_input_resize_and_kill() -> None:
    manager = FakeTerminalManager()
    state = _state(manager)

    assert _terminal_list(state, {}) == {
        "terminals": [{"terminal_id": "term-1", "state": "working"}],
        "launch_history": manager.list_operator_launch_history(),
    }
    assert await _terminal_start(
        state,
        {
            "command": "codex",
            "args": ["--profile", "work space"],
            "workdir": "~/repo",
            "columns": 100,
            "rows": 30,
        },
    ) == {
        "terminal": {"terminal_id": "manual-1", "state": "ready", "owner": None},
        "launch_history": manager.list_operator_launch_history(),
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
    assert _terminal_forget(state, {"terminal_id": "term-1"}) == {
        "terminal": {"terminal_id": "term-1", "state": "exited"}
    }
    assert manager.inputs == [("term-1", "status\r")]
    assert manager.resizes == [("term-1", 100, 30)]
    assert manager.kills == ["term-1"]
    assert manager.forgotten == ["term-1"]
    assert manager.starts == [
        {
            "command": "codex",
            "arguments": ["--profile", "work space"],
            "cwd": Path("~/repo").expanduser(),
            "launch_workdir": "~/repo",
            "columns": 100,
            "rows": 30,
        }
    ]


@pytest.mark.asyncio
async def test_terminal_operator_handlers_validate_and_register_contract() -> None:
    manager = FakeTerminalManager()
    state = _state(manager)

    with pytest.raises(RpcError) as list_error:
        _terminal_list(state, {"extra": True})
    assert list_error.value.code == "invalid_request"
    with pytest.raises(RpcError) as resize_error:
        await _terminal_resize(state, {"terminal_id": "term-1", "columns": True, "rows": 30})
    assert resize_error.value.code == "invalid_request"
    with pytest.raises(RpcError) as start_error:
        await _terminal_start(state, {"args": ["valid", 1]})
    assert start_error.value.code == "invalid_request"
    with pytest.raises(RpcError) as blank_command_error:
        await _terminal_start(state, {"command": "   "})
    assert blank_command_error.value.code == "invalid_request"
    with pytest.raises(RpcError) as blank_argument_error:
        await _terminal_start(state, {"args": ["  "]})
    assert blank_argument_error.value.code == "invalid_request"

    handlers = build_method_handlers()
    assert {
        "terminal.list",
        "terminal.start",
        "terminal.input",
        "terminal.resize",
        "terminal.kill",
        "terminal.forget",
    } <= set(handlers)
