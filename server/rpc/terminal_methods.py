"""Local operator RPC handlers for interactive Terminal Sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.tools.terminal_manager import (
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_GROUP_NAME_MAX_CHARS,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TerminalManagerError,
)
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_string,
    _reject_unsupported,
    _required_string,
    _required_string_list,
)

JsonObject = dict[str, Any]


def _terminal_list(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "terminal.list does not accept params")
    manager = _terminal_manager(state)
    return {
        "groups": manager.list_groups_for_operator(),
        "terminals": manager.list_for_operator(),
        "launch_history": manager.list_operator_launch_history(),
    }


def _terminal_group_create(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"name"}, "terminal.group.create")
    name = _group_name_param(params)
    try:
        group = _terminal_manager(state).create_group_for_operator(name)
    except (ValueError, TerminalManagerError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    return {"group": group}


def _terminal_group_rename(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"group_id", "name"}, "terminal.group.rename")
    group_id = _required_string(params, "group_id")
    name = _group_name_param(params)
    try:
        group = _terminal_manager(state).rename_group_for_operator(group_id, name)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except TerminalManagerError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    return {"group": group}


async def _terminal_group_delete(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"group_id"}, "terminal.group.delete")
    group_id = _required_string(params, "group_id")
    try:
        result = await _terminal_manager(state).delete_group_for_operator(group_id)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except TerminalManagerError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    return {
        "group_id": str(result.get("group_id")),
        "terminals_killed": result.get("terminals_killed"),
    }


def _terminal_group_order(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"group_id", "order"}, "terminal.group.order")
    group_id = _required_string(params, "group_id")
    order = _required_string_list(params, "order")
    try:
        result = _terminal_manager(state).set_group_order_for_operator(group_id, order)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except TerminalManagerError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    return {"group_id": str(result.get("group_id")), "order": list(result.get("order", []))}


async def _terminal_start(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"command", "args", "workdir", "name", "group_id", "columns", "rows"},
        "terminal.start",
    )
    command = _optional_string(params, "command")
    workdir = _optional_string(params, "workdir")
    name = _optional_string(params, "name")
    group_id = _optional_string(params, "group_id")
    arguments = _optional_arguments(params)
    columns = (
        _required_integer(params, "columns") if "columns" in params else TERMINAL_DEFAULT_COLUMNS
    )
    rows = _required_integer(params, "rows") if "rows" in params else TERMINAL_DEFAULT_ROWS
    if not TERMINAL_MIN_COLUMNS <= columns <= TERMINAL_MAX_COLUMNS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.columns must be between {TERMINAL_MIN_COLUMNS} and {TERMINAL_MAX_COLUMNS}",
        )
    if not TERMINAL_MIN_ROWS <= rows <= TERMINAL_MAX_ROWS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.rows must be between {TERMINAL_MIN_ROWS} and {TERMINAL_MAX_ROWS}",
        )
    if command is not None and not command.strip():
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.command must not be empty when provided",
        )
    if name is not None and not name.strip():
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.name must not be empty when provided",
        )
    if name is not None and len(name.strip()) > 80:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.name must be at most 80 characters",
        )
    if any(not argument.strip() for argument in arguments):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.args must not contain empty arguments",
        )
    try:
        terminal = await _terminal_manager(state).spawn_for_operator(
            command=command,
            arguments=arguments,
            cwd=Path(workdir).expanduser() if workdir is not None else None,
            launch_workdir=workdir,
            name=name.strip() if name else None,
            group_id=group_id.strip() if group_id else None,
            columns=columns,
            rows=rows,
        )
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "terminal": terminal,
        "launch_history": _terminal_manager(state).list_operator_launch_history(),
    }


async def _terminal_input(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"terminal_id", "data"}, "terminal.input")
    terminal_id = _required_string(params, "terminal_id")
    data = _required_string(params, "data")
    if len(data) > TERMINAL_INPUT_MAX_CHARS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.data must not exceed {TERMINAL_INPUT_MAX_CHARS} characters",
        )
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
    if not TERMINAL_MIN_COLUMNS <= columns <= TERMINAL_MAX_COLUMNS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.columns must be between {TERMINAL_MIN_COLUMNS} and {TERMINAL_MAX_COLUMNS}",
        )
    if not TERMINAL_MIN_ROWS <= rows <= TERMINAL_MAX_ROWS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.rows must be between {TERMINAL_MIN_ROWS} and {TERMINAL_MAX_ROWS}",
        )
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


def _terminal_forget(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"terminal_id"}, "terminal.forget")
    terminal_id = _required_string(params, "terminal_id")
    try:
        terminal = _terminal_manager(state).forget_for_operator(terminal_id)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"terminal": terminal}


def _required_integer(params: JsonObject, key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be an integer")
    return value


def _group_name_param(params: JsonObject) -> str:
    name = _required_string(params, "name")
    if not name.strip():
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.name must not be empty when provided",
        )
    if len(name.strip()) > TERMINAL_GROUP_NAME_MAX_CHARS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.name must be at most {TERMINAL_GROUP_NAME_MAX_CHARS} characters",
        )
    return name.strip()


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
        "terminal.forget": _terminal_forget,
        "terminal.group.create": _terminal_group_create,
        "terminal.group.rename": _terminal_group_rename,
        "terminal.group.delete": _terminal_group_delete,
        "terminal.group.order": _terminal_group_order,
    }
