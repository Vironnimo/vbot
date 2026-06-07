"""Connection, provider, and model RPC handlers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from core.models.discovery import refresh_models
from core.models.models import ModelRegistry
from core.models.query import ModelQuery
from core.utils.errors import ConfigError
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import _publish_provider_auth_completed_event
from server.rpc.payloads import _model_response
from server.rpc.provider_access import (
    _api_key_connection,
    _connection_response,
    _device_flow_active,
    _device_flow_engine,
    _first_usable_provider_credential,
    _oauth_connection,
    _oauth_device_connection,
    _provider_has_credentials,
    _runtime_resources_dir,
    _runtime_token_store,
)
from server.rpc.validation import _required_string

JsonObject = dict[str, Any]
_LOGGER = logging.getLogger("vbot.server.rpc.connection_methods")
MODEL_LIST_FILTER_FIELDS = frozenset(
    (
        "provider_id",
        "capability",
        "capabilities",
        "task",
        "tasks",
        "task_type",
        "task_types",
        "input_modality",
        "input_modalities",
        "output_modality",
        "output_modalities",
        "min_context_window",
    )
)


def _list_models(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - MODEL_LIST_FILTER_FIELDS)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported model.list fields: {', '.join(unsupported_fields)}",
        )

    try:
        model_query = ModelQuery.from_filters(params)
    except (KeyError, ValueError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    try:
        runtime = state.runtime
        models = sorted(
            (
                _model_response(provider_id, model)
                for provider_id, model in runtime.models.query(model_query)
                if _provider_has_credentials(runtime, provider_id)
            ),
            key=lambda model: (model["provider_id"], model["model_id"]),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"models": models}


def _list_connections(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "connection.list does not accept params")
    try:
        runtime = state.runtime
        connections = [
            _connection_response(runtime, provider_id, connection)
            for provider_id in runtime.providers.list_ids()
            for connection in runtime.providers.get(provider_id).connections
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"connections": connections}


def _set_provider_key(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id", "value"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider set-key fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    value = _required_string(params, "value")
    raw_connection_id = params.get("connection_id")
    connection_id = (
        _required_string(params, "connection_id") if raw_connection_id is not None else None
    )

    try:
        runtime = state.runtime
        connection = _api_key_connection(runtime, provider_id, connection_id)
        public_connection_id = f"{provider_id}:{connection.id}"
        credential_key = connection.auth.credential_key
        runtime.storage.set_data_dir_credential(credential_key, value)
        runtime.reload_provider_credentials()
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "credential_key": credential_key,
        "configured": True,
    }


async def _refresh_model_db(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported model refresh fields: {', '.join(unsupported_fields)}",
        )

    try:
        runtime = state.runtime
        resources_dir = _runtime_resources_dir(runtime)
        if "provider_id" in params:
            provider_id = _required_string(params, "provider_id")
            return await _refresh_provider_model_db(runtime, provider_id, resources_dir)

        result = await _refresh_global_model_db(runtime, resources_dir)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return result


async def _connect_provider(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider connect fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")

    try:
        connection = _oauth_device_connection(state.runtime, provider_id, connection_id)
        engine = _device_flow_engine(state)
        oauth_config = connection.oauth
        session = await engine.start_device_flow(provider_id, connection.id, oauth_config)

        async def on_complete(*, success: bool) -> None:
            _publish_provider_auth_completed_event(
                state,
                provider_id=provider_id,
                connection_id=connection_id,
                success=success,
            )

        poll_task = asyncio.create_task(
            engine._poll_for_token(
                provider_id,
                connection.id,
                oauth_config,
                session.device_code,
                session.interval,
                session.expires_in,
                on_complete,
                user_code=session.user_code,
            )
        )
        poll_task.add_done_callback(_on_device_flow_poll_done)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "user_code": session.user_code,
        "verification_uri": session.verification_uri,
        "expires_in": session.expires_in,
    }


def _on_device_flow_poll_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        _LOGGER.warning("OAuth device flow polling task failed", exc_info=True)


def _disconnect_provider(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider disconnect fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        _runtime_token_store(state.runtime).delete(provider_id, connection.id)
        engine = getattr(state, "device_flow_engine", None)
        if engine is not None:
            engine.cancel_flow(provider_id, connection.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {"provider_id": provider_id, "connection_id": connection_id, "status": "disconnected"}


def _provider_connection_status(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider connection status fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        token_store = _runtime_token_store(state.runtime)
        engine = getattr(state, "device_flow_engine", None)
        connected = token_store.has_valid_token(provider_id, connection.id)
        flow_active = _device_flow_active(engine, provider_id, connection.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": connection_id,
        "connected": connected,
        "flow_active": flow_active,
    }


async def _refresh_global_model_db(runtime: Any, resources_dir: Path) -> JsonObject:
    refreshed_providers: list[JsonObject] = []
    for provider_id in runtime.providers.list_ids():
        provider = runtime.providers.get(provider_id)
        if not getattr(provider, "models_endpoint", None):
            continue

        try:
            credential_connection, credential_value = await _first_usable_provider_credential(
                runtime,
                provider_id,
                provider,
            )
        except ConfigError:
            continue

        result = await refresh_models(
            provider,
            credential_value,
            resources_dir,
            credential_connection=credential_connection,
        )
        refreshed_providers.append(result)

    _reload_runtime_model_registry(runtime, resources_dir)
    return {
        "providers": refreshed_providers,
        "refreshed_count": len(refreshed_providers),
        "model_count": sum(_model_count(result) for result in refreshed_providers),
    }


async def _refresh_provider_model_db(
    runtime: Any,
    provider_id: str,
    resources_dir: Path,
) -> JsonObject:
    provider = runtime.providers.get(provider_id)
    if not getattr(provider, "models_endpoint", None):
        raise RpcError(
            RPC_ERROR_DOMAIN,
            f"provider '{provider_id}' does not support model refresh",
        )

    credential_connection, credential_value = await _first_usable_provider_credential(
        runtime,
        provider_id,
        provider,
    )
    result = await refresh_models(
        provider,
        credential_value,
        resources_dir,
        credential_connection=credential_connection,
    )
    _reload_runtime_model_registry(runtime, resources_dir)
    return result


def _reload_runtime_model_registry(runtime: Any, resources_dir: Path) -> None:
    ModelRegistry.invalidate(resources_dir)
    runtime._models = ModelRegistry.load(resources_dir)


def _model_count(result: JsonObject) -> int:
    model_count = result.get("model_count", 0)
    if isinstance(model_count, bool) or not isinstance(model_count, int):
        return 0
    return int(model_count)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return connection/provider/model RPC handlers."""

    return {
        "connection.list": _list_connections,
        "model.list": _list_models,
        "model.refresh_db": _refresh_model_db,
        "provider.set_key": _set_provider_key,
        "provider.connect": _connect_provider,
        "provider.disconnect": _disconnect_provider,
        "provider.connection_status": _provider_connection_status,
    }
