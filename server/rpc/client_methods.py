"""Client presence RPC handlers."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]


def _list_clients(state: Any, params: JsonObject) -> JsonObject:
    """Return the roster of connected app clients (browser tabs, Desktop, tray).

    A pure read of the in-memory presence registry. The client re-fetches this
    after each ``resource_changed(kind="clients")`` signal.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "client.list does not accept params")
    return {"clients": [entry.to_dict() for entry in state.client_registry.list()]}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return client presence RPC handlers."""

    return {
        "client.list": _list_clients,
    }
