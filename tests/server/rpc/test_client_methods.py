"""Tests for the ``client.list`` presence RPC handler.

Coverage:
- returns the registry roster as row dicts,
- empty when no registry is wired (CLI-only runtime stub),
- rejects unexpected params,
- the handler is registered in the method table.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.clients import ClientRegistry
from server.rpc.client_methods import _list_clients
from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers


def test_list_clients_returns_registry_roster() -> None:
    registry = ClientRegistry()
    registry.register(
        connection_id="tab-a",
        accessor="browser",
        user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537",
    )
    state = SimpleNamespace(client_registry=registry)

    result = _list_clients(state, {})

    assert len(result["clients"]) == 1
    entry = result["clients"][0]
    assert entry["connection_id"] == "tab-a"
    assert entry["accessor"] == "browser"
    assert entry["browser"] == "Chrome"
    assert entry["os"] == "Windows"
    assert entry["status"] == "connected"


def test_list_clients_empty_without_registry() -> None:
    state = SimpleNamespace()

    result = _list_clients(state, {})

    assert result == {"clients": []}


def test_list_clients_rejects_params() -> None:
    state = SimpleNamespace(client_registry=ClientRegistry())

    with pytest.raises(RpcError, match="does not accept params"):
        _list_clients(state, {"unexpected": True})


def test_client_list_is_registered() -> None:
    handlers = build_method_handlers()

    assert "client.list" in handlers
