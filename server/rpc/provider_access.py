"""Provider and connection helper functions for RPC handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from core.providers.auth_flow import DeviceFlowEngine
from core.providers.token_getter import OAuthTokenGetter
from core.utils.errors import ConfigError
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RPC_ERROR_OAUTH_NOT_SUPPORTED, RpcError

JsonObject = dict[str, Any]


def _provider_has_credentials(runtime: Any, provider_id: str) -> bool:
    return bool(runtime.has_provider_credentials(provider_id))


def _connection_has_credentials(runtime: Any, provider_id: str, connection_id: str) -> bool:
    return bool(runtime.provider_credentials.has_credentials(provider_id, connection_id))


async def _runtime_provider_credential(
    runtime: Any,
    provider_id: str,
    connection_id: str,
    connection: Any,
) -> str:
    if getattr(connection, "type", "") != "oauth" or getattr(connection, "oauth", None) is None:
        return str(runtime.provider_credentials.get_credentials(provider_id, connection_id))

    token_store = _runtime_token_store(runtime)
    getter = OAuthTokenGetter(token_store, provider_id, connection.id, connection.oauth)
    async with getter:
        return await getter()


def _runtime_resources_dir(runtime: Any) -> Path:
    resolve_resources_path = getattr(runtime, "_resolve_resources_path", None)
    if callable(resolve_resources_path):
        return Path(resolve_resources_path())
    resources_dir = getattr(runtime, "resources_dir", None)
    if resources_dir is not None:
        return Path(resources_dir)
    raise ConfigError("Runtime resources directory is not available")


def _oauth_device_connection(runtime: Any, provider_id: str, connection_id: str) -> Any:
    connection = _oauth_connection(runtime, provider_id, connection_id)
    oauth_config = getattr(connection, "oauth", None)
    if oauth_config is None or getattr(oauth_config, "flow", "") != "device":
        raise RpcError(
            RPC_ERROR_OAUTH_NOT_SUPPORTED,
            f"provider connection '{connection_id}' does not support OAuth Device Flow",
        )
    return connection


def _oauth_connection(runtime: Any, provider_id: str, connection_id: str) -> Any:
    connection = _provider_connection(runtime, provider_id, connection_id)
    if getattr(connection, "type", "") != "oauth":
        raise RpcError(
            RPC_ERROR_OAUTH_NOT_SUPPORTED,
            f"provider connection '{connection_id}' is not an OAuth connection",
        )
    return connection


def _api_key_connection(runtime: Any, provider_id: str, connection_id: str | None) -> Any:
    if connection_id is not None:
        connection = _provider_connection(runtime, provider_id, connection_id)
        if getattr(connection, "type", "") != "api_key":
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"provider connection '{connection_id}' is not an API key connection",
            )
        return connection

    provider = runtime.providers.get(provider_id)
    connections = [
        connection
        for connection in provider.connections
        if getattr(connection, "type", "") == "api_key"
    ]
    if not connections:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"provider '{provider_id}' has no API key connection",
        )
    if len(connections) > 1:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"provider '{provider_id}' has multiple API key connections; pass connection_id",
        )
    return connections[0]


def _provider_connection(runtime: Any, provider_id: str, connection_id: str) -> Any:
    provider = runtime.providers.get(provider_id)
    expected_prefix = f"{provider_id}:"
    if not connection_id.startswith(expected_prefix):
        raise ConfigError(
            f"Connection id '{connection_id}' does not belong to provider '{provider_id}'"
        )
    local_connection_id = connection_id.removeprefix(expected_prefix)
    get_connection = getattr(provider, "get_connection", None)
    if callable(get_connection):
        return get_connection(local_connection_id)
    for connection in provider.connections:
        if getattr(connection, "id", "") == local_connection_id:
            return connection
    raise ConfigError(f"Unknown connection id '{connection_id}' for provider '{provider_id}'")


def _runtime_token_store(runtime: Any) -> Any:
    token_store = getattr(runtime, "token_store", None)
    if token_store is None:
        raise ConfigError("Runtime OAuth token store is not available")
    return token_store


def _device_flow_engine(state: Any) -> DeviceFlowEngine:
    engine = getattr(state, "device_flow_engine", None)
    if engine is not None:
        return cast(DeviceFlowEngine, engine)
    engine = DeviceFlowEngine(_runtime_token_store(state.runtime))
    state.device_flow_engine = engine
    return engine


def _device_flow_active(engine: Any, provider_id: str, local_connection_id: str) -> bool:
    if engine is None:
        return False
    active_flows = getattr(engine, "_active_flows", {})
    task = active_flows.get((provider_id, local_connection_id))
    return bool(task is not None and not task.done())


def _connection_response(runtime: Any, provider_id: str, connection: Any) -> JsonObject:
    connection_id = f"{provider_id}:{connection.id}"
    return {
        "id": connection_id,
        "provider_id": provider_id,
        "type": connection.type,
        "label": connection.label,
        "usable": _connection_has_credentials(runtime, provider_id, connection_id),
    }


def _provider_settings_connection(runtime: Any, provider_id: str, connection: Any) -> JsonObject:
    connection_id = f"{provider_id}:{connection.id}"
    response = {
        "id": connection_id,
        "type": connection.type,
        "label": connection.label,
        "configured": _connection_has_credentials(runtime, provider_id, connection_id),
    }
    if getattr(connection, "type", "") == "oauth":
        oauth_config = getattr(connection, "oauth", None)
        response["connectable"] = bool(
            oauth_config is not None and getattr(oauth_config, "flow", "") == "device"
        )
    return response
