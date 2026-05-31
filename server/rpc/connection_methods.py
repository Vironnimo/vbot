"""Connection, provider, and model RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return connection/provider/model RPC handlers from the delegates facade."""

    return {
        "connection.list": delegates._list_connections,
        "model.list": delegates._list_models,
        "model.refresh_db": delegates._refresh_model_db,
        "provider.set_key": delegates._set_provider_key,
        "provider.connect": delegates._connect_provider,
        "provider.disconnect": delegates._disconnect_provider,
        "provider.connection_status": delegates._provider_connection_status,
    }
