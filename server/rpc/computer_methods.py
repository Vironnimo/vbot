"""Operator computer discovery; native capability execution has its own link."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError


def _list(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "computers.list takes no parameters")
    return {"computers": [item.describe() for item in state.runtime.computers.list()]}


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {"computers.list": _list}
