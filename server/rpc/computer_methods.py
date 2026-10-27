"""Operator computer discovery and removal; native capability execution has its own link."""

from __future__ import annotations

from typing import Any

from server.access import ACCESS_DENIED, current_principal
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError


def _list(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "computers.list takes no parameters")
    return {"computers": [item.describe() for item in state.runtime.computers.list()]}


async def _remove(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    computer_id = params.get("id")
    if set(params) != {"id"} or not isinstance(computer_id, str) or not computer_id:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "computers.remove needs only the computer id")
    # Like Device management, only an operator request may remove a computer;
    # in-process Agent or Live dispatch has no principal.
    if current_principal() is None:
        raise RpcError(ACCESS_DENIED, "Removing a computer needs a request from a signed-in client")
    try:
        await state.runtime.computers.forget(computer_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"removed": computer_id}


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {"computers.list": _list, "computers.remove": _remove}
