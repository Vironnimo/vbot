"""Client presence RPC handlers."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]


def _list_clients(state: Any, params: JsonObject) -> JsonObject:
    """Return the roster of connected app windows (browser tabs / Desktop shell).

    A pure read of the in-memory presence registry; empty when no registry is
    wired (e.g. a CLI-only runtime stub). The client re-fetches this after each
    ``resource_changed(kind="clients")`` signal.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "client.list does not accept params")
    registry = getattr(state, "client_registry", None)
    if registry is None:
        return {"clients": []}
    return {"clients": [entry.to_dict() for entry in registry.list()]}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return client presence RPC handlers."""

    return {
        "client.list": _list_clients,
    }
