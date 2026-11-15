"""Client presence RPC handlers."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.validation import _reject_unsupported, rpc_request_fields

JsonObject = dict[str, Any]


def _list_clients(state: Any, params: JsonObject) -> JsonObject:
    """Return the roster of connected app clients (browser tabs, Desktop, tray).

    A pure read of the in-memory presence registry. The client re-fetches this
    after each ``resource_changed(kind="clients")`` signal.
    """
    _reject_unsupported(params, rpc_request_fields("client.list"), "client.list")
    return {"clients": [entry.to_dict() for entry in state.client_registry.list()]}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return client presence RPC handlers."""

    return {
        "client.list": _list_clients,
    }
