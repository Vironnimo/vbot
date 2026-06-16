"""Connection, provider, and model RPC handlers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from core.models.discovery import ModelDiscoveryError, refresh_models
from core.models.models_dev import (
    ModelsDevCatalog,
    ModelsDevError,
    fetch_catalog,
    refresh_canonical_layer,
)
from core.models.query import ModelQuery
from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    compose_connection_id,
    derive_credential_key,
    split_connection_id,
    validate_account_id,
)
from core.providers.errors import NetworkError, ProviderError
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
    _oauth_connection,
    _oauth_device_connection,
    _provider_has_credentials,
    _runtime_provider_credential,
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


def _account_param(params: JsonObject) -> str | None:
    """Return the validated optional ``account`` param, or ``None`` when absent."""

    if params.get("account") is None:
        return None
    account = _required_string(params, "account")
    try:
        return validate_account_id(account)
    except ConfigError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _effective_account_id(
    provider_id: str,
    connection_id: str | None,
    account: str | None,
) -> str:
    """Combine the ``account`` param with an account-carrying connection id.

    An account embedded in the compositional connection id and an explicit
    ``account`` param must agree; either alone wins over the default.
    """

    embedded_account_id = None
    if connection_id is not None:
        _local_connection_id, embedded_account_id = split_connection_id(provider_id, connection_id)
    if account is not None and embedded_account_id is not None and account != embedded_account_id:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.account '{account}' conflicts with account "
            f"'{embedded_account_id}' in params.connection_id",
        )
    if account is not None:
        return account
    if embedded_account_id is not None:
        return embedded_account_id
    return DEFAULT_ACCOUNT_ID


def _set_provider_key(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id", "value", "account"})
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
    account = _account_param(params)

    try:
        runtime = state.runtime
        connection = _api_key_connection(runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        credential_key = derive_credential_key(connection.auth.credential_key, account_id)
        runtime.storage.set_data_dir_credential(credential_key, value)
        runtime.reload_provider_credentials()
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "account": account_id,
        "credential_key": credential_key,
        "configured": True,
    }


def _unset_provider_key(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id", "account"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider unset-key fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    raw_connection_id = params.get("connection_id")
    connection_id = (
        _required_string(params, "connection_id") if raw_connection_id is not None else None
    )
    account = _account_param(params)

    try:
        runtime = state.runtime
        connection = _api_key_connection(runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        credential_key = derive_credential_key(connection.auth.credential_key, account_id)
        removed = bool(runtime.storage.remove_data_dir_credential(credential_key))
        runtime.reload_provider_credentials()
        configured = runtime.provider_credentials.has_credentials(
            provider_id,
            compose_connection_id(provider_id, connection.id, account_id),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "account": account_id,
        "credential_key": credential_key,
        "removed": removed,
        "configured": configured,
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
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id", "account"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider connect fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    account = _account_param(params)

    try:
        connection = _oauth_device_connection(state.runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        engine = _device_flow_engine(state)
        oauth_config = connection.oauth
        session = await engine.start_device_flow(
            provider_id,
            connection.id,
            oauth_config,
            account_id=account_id,
        )

        async def on_complete(*, success: bool) -> None:
            _publish_provider_auth_completed_event(
                state,
                provider_id=provider_id,
                connection_id=public_connection_id,
                account=account_id,
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
                account_id=account_id,
            )
        )
        poll_task.add_done_callback(_on_device_flow_poll_done)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "user_code": session.user_code,
        "verification_uri": session.verification_uri,
        "expires_in": session.expires_in,
        "account": account_id,
    }


def _on_device_flow_poll_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        _LOGGER.warning("OAuth device flow polling task failed", exc_info=True)


def _disconnect_provider(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id", "account"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider disconnect fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    account = _account_param(params)

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        _runtime_token_store(state.runtime).delete(
            provider_id, connection.id, account_id=account_id
        )
        engine = getattr(state, "device_flow_engine", None)
        if engine is not None:
            engine.cancel_flow(provider_id, connection.id, account_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": compose_connection_id(provider_id, connection.id),
        "account": account_id,
        "status": "disconnected",
    }


def _provider_connection_status(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id", "account"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider connection status fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    account = _account_param(params)

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        token_store = _runtime_token_store(state.runtime)
        engine = getattr(state, "device_flow_engine", None)
        connected = token_store.has_valid_token(provider_id, connection.id, account_id=account_id)
        flow_active = _device_flow_active(engine, provider_id, connection.id, account_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": compose_connection_id(provider_id, connection.id),
        "account": account_id,
        "connected": connected,
        "flow_active": flow_active,
    }


async def _fetch_catalog_for_refresh() -> ModelsDevCatalog | None:
    """Fetch the models.dev catalog once for a refresh — best-effort.

    Shared by both refresh entry points so the public catalog is fetched a
    single time and threaded to every per-provider refresh + the canonical
    projection. A fetch failure logs and returns ``None``: the canonical join is
    enrichment, not a dependency, so refresh still writes pure provider wire
    facts without it.
    """

    try:
        return await fetch_catalog()
    except (ModelsDevError, ProviderError, NetworkError, httpx.HTTPError) as exc:
        _LOGGER.warning(
            "models.dev catalog unavailable for this refresh; "
            "writing provider catalogs without canonical enrichment: %s",
            exc,
        )
        return None


async def _refresh_global_model_db(runtime: Any, resources_dir: Path) -> JsonObject:
    catalog = await _fetch_catalog_for_refresh()
    refreshed_providers: list[JsonObject] = []
    refresh_errors: list[JsonObject] = []
    for provider_id in runtime.providers.list_ids():
        provider = runtime.providers.get(provider_id)
        if not _provider_supports_refresh(provider):
            continue

        successes, errors = await _refresh_provider_connections(
            runtime,
            provider_id,
            provider,
            resources_dir,
            catalog,
        )
        refreshed_providers.extend(successes)
        refresh_errors.extend(errors)

    canonical_result = await _refresh_canonical_layer_if_possible(catalog, resources_dir)
    _reload_runtime_model_registry(runtime, resources_dir)
    result: JsonObject = {
        "providers": refreshed_providers,
        "refreshed_count": len(refreshed_providers),
        "model_count": sum(_model_count(entry) for entry in refreshed_providers),
        "canonical": canonical_result,
    }
    if refresh_errors:
        result["errors"] = refresh_errors
    return result


async def _refresh_canonical_layer_if_possible(
    catalog: ModelsDevCatalog | None,
    resources_dir: Path,
) -> JsonObject | None:
    """Project the canonical layer when a catalog is available; else ``None``.

    Writes ``models.json`` + the raw dump + seeds ``models.overrides.json``.
    Skipped (returns ``None``) when the catalog could not be fetched.
    """

    if catalog is None:
        return None
    return await refresh_canonical_layer(resources_dir, catalog=catalog)


async def _refresh_provider_model_db(
    runtime: Any,
    provider_id: str,
    resources_dir: Path,
) -> JsonObject:
    provider = runtime.providers.get(provider_id)
    if not _provider_supports_refresh(provider):
        raise RpcError(
            RPC_ERROR_DOMAIN,
            f"provider '{provider_id}' does not support model refresh",
        )

    catalog = await _fetch_catalog_for_refresh()
    successes, errors = await _refresh_provider_connections(
        runtime,
        provider_id,
        provider,
        resources_dir,
        catalog,
    )
    if not successes:
        # An explicit single-provider refresh that produced nothing useful
        # still reports why: a discovery failure surfaces its message, an
        # absent credential keeps the existing "not found" wording.
        if errors:
            raise RpcError(RPC_ERROR_DOMAIN, str(errors[0]["error"]))
        raise RpcError(
            RPC_ERROR_DOMAIN,
            f"Provider credentials not found for provider '{provider_id}'",
        )
    await _refresh_canonical_layer_if_possible(catalog, resources_dir)
    _reload_runtime_model_registry(runtime, resources_dir)
    result = dict(successes[0])
    if errors:
        result["errors"] = errors
    return result


def _provider_supports_refresh(provider: Any) -> bool:
    """Return whether *provider* exposes a refreshable ``models_endpoint``.

    A provider counts as refreshable when it has a provider-level
    ``models_endpoint`` or at least one connection-level one. This guard is
    separate from credential presence so the RPC layer can distinguish
    "no refresh endpoint" from "no credentials".
    """

    if getattr(provider, "models_endpoint", None):
        return True
    return any(
        getattr(connection, "models_endpoint", None)
        for connection in getattr(provider, "connections", [])
    )


def _connection_effective_endpoint(connection: Any, provider: Any) -> str | None:
    return getattr(connection, "models_endpoint", None) or getattr(
        provider, "models_endpoint", None
    )


async def _refresh_provider_connections(
    runtime: Any,
    provider_id: str,
    provider: Any,
    resources_dir: Path,
    models_dev_catalog: ModelsDevCatalog | None = None,
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Refresh every connection on *provider* that supports it.

    Connections without an effective ``models_endpoint`` or without
    credentials are skipped. Successful refreshes accumulate into the
    shared ``<provider>.json`` catalog via discovery's merge logic. The
    pre-fetched ``models_dev_catalog`` is threaded into each refresh so the
    public catalog is fetched once per refresh, not once per connection.

    A connection whose discovery fails (provider unreachable, bad key, fatal
    HTTP status, malformed catalog) is logged and recorded as an error rather
    than raised: one broken connection must never abort its sibling
    connections or — in a global refresh — the remaining providers. Returns
    ``(successes, errors)``; each error carries the connection id and message.
    """

    successes: list[JsonObject] = []
    errors: list[JsonObject] = []
    for connection in getattr(provider, "connections", []):
        if not _connection_effective_endpoint(connection, provider):
            continue
        connection_id = f"{provider_id}:{connection.id}"
        if not runtime.provider_credentials.has_credentials(provider_id, connection_id):
            continue
        try:
            credential_value = await _runtime_provider_credential(
                runtime, provider_id, connection_id, connection
            )
        except (ConfigError, RpcError) as exc:
            _LOGGER.warning(
                "Skipping model refresh for provider '%s' connection '%s': %s",
                provider_id,
                connection.id,
                exc,
            )
            continue
        try:
            result = await refresh_models(
                provider,
                credential_value,
                resources_dir,
                credential_connection=connection,
                models_dev_catalog=models_dev_catalog,
            )
        except ModelDiscoveryError as exc:
            _LOGGER.warning(
                "Model refresh failed for provider '%s' connection '%s': %s",
                provider_id,
                connection.id,
                exc,
            )
            errors.append(
                {
                    "provider_id": provider_id,
                    "connection_id": connection_id,
                    "error": str(exc),
                }
            )
            continue
        successes.append(result)
    return successes, errors


def _reload_runtime_model_registry(runtime: Any, resources_dir: Path) -> None:
    # Reload in place rather than rebinding ``runtime._models``: services that
    # captured the registry at construction (task-model targets for
    # speech/image/embeddings, the status display, the recall backend) hold the
    # same instance, so an in-place swap reaches all of them without re-wiring.
    runtime.models.reload(resources_dir)


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
        "provider.unset_key": _unset_provider_key,
        "provider.connect": _connect_provider,
        "provider.disconnect": _disconnect_provider,
        "provider.connection_status": _provider_connection_status,
    }
