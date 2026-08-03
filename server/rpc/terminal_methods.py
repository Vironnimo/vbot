"""Local operator RPC handlers for interactive Terminal Sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.tools.terminal_manager import TERMINAL_DEFAULT_COLUMNS, TERMINAL_DEFAULT_ROWS
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _optional_string, _reject_unsupported, _required_string

JsonObject = dict[str, Any]


def _terminal_list(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "terminal.list does not accept params")
    return {"terminals": _terminal_manager(state).list_active_for_operator()}


async def _terminal_start(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"command", "args", "workdir", "columns", "rows"},
        "terminal.start",
    )
    command = _optional_string(params, "command")
    workdir = _optional_string(params, "workdir")
    arguments = _optional_arguments(params)
    columns = (
        _required_integer(params, "columns") if "columns" in params else TERMINAL_DEFAULT_COLUMNS
    )
    rows = _required_integer(params, "rows") if "rows" in params else TERMINAL_DEFAULT_ROWS
    try:
        terminal = await _terminal_manager(state).spawn_for_operator(
            command=command,
            arguments=arguments,
            cwd=Path(workdir).expanduser() if workdir is not None else None,
            columns=columns,
            rows=rows,
        )
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"terminal": terminal}


async def _terminal_input(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"terminal_id", "data"}, "terminal.input")
    terminal_id = _required_string(params, "terminal_id")
    data = _required_string(params, "data")
    try:
        terminal = await _terminal_manager(state).send_operator_input(terminal_id, data)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"terminal": terminal}


async def _terminal_resize(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"terminal_id", "columns", "rows"}, "terminal.resize")
    terminal_id = _required_string(params, "terminal_id")
    columns = _required_integer(params, "columns")
    rows = _required_integer(params, "rows")
    try:
        terminal = await _terminal_manager(state).resize_for_operator(
            terminal_id, columns=columns, rows=rows
        )
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"terminal": terminal}


async def _terminal_kill(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"terminal_id"}, "terminal.kill")
    terminal_id = _required_string(params, "terminal_id")
    try:
        terminal = await _terminal_manager(state).kill_for_operator(terminal_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"terminal": terminal}


def _required_integer(params: JsonObject, key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be an integer")
    return value


def _optional_arguments(params: JsonObject) -> list[str]:
    value = params.get("args", [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.args must be a list of strings",
        )
    return value


def _terminal_manager(state: Any) -> Any:
    manager = getattr(state.runtime, "terminal_manager", None)
    if manager is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Interactive terminals are unavailable")
    return manager


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return interactive Terminal operator handlers."""

    return {
        "terminal.list": _terminal_list,
        "terminal.start": _terminal_start,
        "terminal.input": _terminal_input,
        "terminal.resize": _terminal_resize,
        "terminal.kill": _terminal_kill,
    }
