"""Connection, provider, and model RPC handlers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import httpx

from core.models.database import (
    ModelDatabaseRefresh,
    begin_runtime_model_database_refresh,
    begin_system_model_database_refresh,
)
from core.models.discovery import ModelDiscoveryError, refresh_models
from core.models.models import ModelRegistry
from core.models.models_dev import (
    ModelsDevCatalog,
    ModelsDevError,
    fetch_catalog,
    refresh_canonical_layer,
)
from core.models.query import ModelQuery
from core.providers.accounts import (
    CREDENTIAL_KEY_ACCOUNT_SEPARATOR,
    DEFAULT_ACCOUNT_ID,
    compose_connection_id,
    derive_credential_key,
    split_connection_id,
    validate_account_id,
)
from core.providers.errors import NetworkError, ProviderError
from core.providers.providers import custom_provider_credential_key
from core.providers.token_getter import COPILOT_API_ENDPOINT_EXTRA_KEY
from core.settings.normalizers import (
    normalize_custom_provider_id,
    normalize_custom_provider_settings,
)
from core.utils.errors import ConfigError
from server.events import RESOURCE_KIND_MODELS, RESOURCE_KIND_PROVIDERS
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import (
    _publish_provider_auth_completed_event,
    publish_resource_changed,
)
from server.rpc.payloads import _model_detail_response, _model_response
from server.rpc.provider_access import (
    _api_key_connection,
    _connection_reachability,
    _connection_response,
    _device_flow_active,
    _device_flow_engine,
    _oauth_connection,
    _oauth_device_connection,
    _provider_connection,
    _runtime_provider_credential,
    _runtime_resources_dir,
    _runtime_token_store,
)
from server.rpc.validation import _reject_unsupported, _required_string

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
_MODEL_REFRESH_TARGET_RUNTIME = "runtime"
_MODEL_REFRESH_TARGET_SYSTEM = "system"
_MODEL_REFRESH_TARGETS = frozenset({_MODEL_REFRESH_TARGET_RUNTIME, _MODEL_REFRESH_TARGET_SYSTEM})


async def _list_models(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, MODEL_LIST_FILTER_FIELDS, "model.list")

    try:
        model_query = ModelQuery.from_filters(params)
    except (KeyError, ValueError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    # Give local auto-refresh catalogs (e.g. Ollama) a small window to update
    # before listing; on timeout the sweep finishes in the background and this
    # call serves the last known catalog. The runtime method is throttled and
    # never raises.
    await _await_local_catalog_refresh(state.runtime)

    try:
        runtime = state.runtime
        local_context_windows = runtime.storage.load_local_models_settings()["context_windows"]
        usable_connections_by_provider: dict[str, list[Any]] = {}

        def _usable_connections(provider_id: str) -> list[Any]:
            cached = usable_connections_by_provider.get(provider_id)
            if cached is not None:
                return cached
            provider = _provider_config_or_none(runtime, provider_id)
            connections = [
                connection
                for connection in getattr(provider, "connections", [])
                if runtime.provider_credentials.is_usable(
                    provider_id, f"{provider_id}:{connection.id}"
                )
            ]
            usable_connections_by_provider[provider_id] = connections
            return connections

        models = []
        for provider_id, model in runtime.models.query(model_query):
            allowed_connections = [
                connection
                for connection in _usable_connections(provider_id)
                if model.allows_connection(connection.id)
            ]
            if not allowed_connections:
                continue
            response = _model_response(
                provider_id,
                model,
                provider_config=_provider_config_or_none(runtime, provider_id),
                local_context_windows=local_context_windows,
            )
            reachable = _model_reachability(runtime, provider_id, allowed_connections)
            if reachable is not None:
                response["reachable"] = reachable
            models.append(response)
        models.sort(key=lambda model: (model["provider_id"], model["model_id"]))
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"models": models}


async def _get_model(state: Any, params: JsonObject) -> JsonObject:
    """Return the complete public data for one exact Provider Model."""

    _reject_unsupported(params, {"model"}, "model.get")
    model_reference = _required_string(params, "model")
    bare_reference = (
        model_reference.rpartition("::")[0] if "::" in model_reference else model_reference
    )
    provider_id, separator, model_id = bare_reference.partition("/")
    if separator != "/" or not provider_id or not model_id:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.model must be '<provider>/<model-id>'",
        )

    await _await_local_catalog_refresh(state.runtime)
    runtime = state.runtime
    try:
        model = runtime.models.get(provider_id, model_id)
    except KeyError as exc:
        candidates = [
            f"{candidate_provider}/{candidate_model.model_id}"
            for candidate_provider, candidate_model in runtime.models.query(ModelQuery())
        ]
        suggestions = get_close_matches(bare_reference, candidates, n=3, cutoff=0.35)
        suffix = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unknown Model {bare_reference!r}{suffix}",
        ) from exc

    local_context_windows = runtime.storage.load_local_models_settings()["context_windows"]
    response = _model_detail_response(
        provider_id,
        model,
        provider_config=_provider_config_or_none(runtime, provider_id),
        local_context_windows=local_context_windows,
    )
    provider = _provider_config_or_none(runtime, provider_id)
    allowed_connections = [
        connection
        for connection in getattr(provider, "connections", ())
        if model.allows_connection(connection.id)
    ]
    response["usable_connections"] = [
        connection.id
        for connection in allowed_connections
        if runtime.provider_credentials.is_usable(
            provider_id,
            f"{provider_id}:{connection.id}",
        )
    ]
    reachable = _model_reachability(runtime, provider_id, allowed_connections)
    if reachable is not None:
        response["reachable"] = reachable
    return {"model": response}


async def _routing_provider_options(state: Any, params: JsonObject) -> JsonObject:
    """Return provider slugs selectable for OpenRouter globally or per Model."""

    _reject_unsupported(params, {"provider_id", "model_id"}, "provider routing options")
    provider_id = _required_string(params, "provider_id")
    if provider_id != "openrouter":
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"provider '{provider_id}' does not expose routing options",
        )
    model_id = params.get("model_id")
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.model_id must be a non-empty string")
        model_id = model_id.strip()

    try:
        runtime = state.runtime
        provider = runtime.providers.get(provider_id)
        connection_id = next(
            (
                f"{provider_id}:{connection.id}"
                for connection in provider.connections
                if runtime.provider_credentials.is_usable(
                    provider_id,
                    f"{provider_id}:{connection.id}",
                )
            ),
            None,
        )
        if connection_id is None:
            raise ConfigError("OpenRouter has no usable Connection")
        adapter = runtime.get_adapter(provider_id, connection_id)
        loader = getattr(adapter, "routing_provider_options", None)
        if not callable(loader):
            await adapter.aclose()
            raise ConfigError("OpenRouter Adapter does not expose routing options")
        try:
            providers = await loader(model_id)
        finally:
            await adapter.aclose()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"providers": providers}


def _model_reachability(
    runtime: Any, provider_id: str, allowed_connections: list[Any]
) -> bool | None:
    """Return ``False`` when a model is only served by unreachable local endpoints.

    Only local auto-refresh connections carry a probe outcome. A model whose
    every usable connection is such a connection with a failed last probe is
    marked ``reachable: false`` so pickers can badge it ("service not running")
    while keeping it selectable. Any non-probed (remote) connection or an
    unknown probe state means no statement — the key is omitted.
    """

    saw_probe_failure = False
    for connection in allowed_connections:
        if not getattr(connection, "auto_refresh", False):
            return None
        probe = _connection_reachability(runtime, f"{provider_id}:{connection.id}")
        if probe is not False:
            return None
        saw_probe_failure = True
    return False if saw_probe_failure else None


def _provider_config_or_none(runtime: Any, provider_id: str) -> Any:
    try:
        return runtime.providers.get(provider_id)
    except (KeyError, AttributeError):
        return None


# Wall-clock budget model.list grants the local-catalog auto-refresh before
# serving the stale catalog and letting the sweep finish in the background.
LOCAL_CATALOG_REFRESH_WAIT_SECONDS = 3.0

# Strong references to background refresh sweeps that outlived their
# model.list call, so the tasks are not garbage-collected mid-flight.
_BACKGROUND_REFRESH_TASKS: set[asyncio.Task[None]] = set()


async def _await_local_catalog_refresh(runtime: Any) -> None:
    maybe_refresh = getattr(runtime, "maybe_refresh_local_catalogs", None)
    if not callable(maybe_refresh):
        return
    refresh_task = asyncio.ensure_future(maybe_refresh())
    # ``asyncio.wait`` (unlike ``wait_for``) does not cancel on timeout — the
    # sweep keeps running in the background and later calls see its result.
    done, pending = await asyncio.wait({refresh_task}, timeout=LOCAL_CATALOG_REFRESH_WAIT_SECONDS)
    for task in done:
        # The runtime method is designed never to raise; consume a defensive
        # surprise so it degrades to a logged stale-catalog listing.
        exception = task.exception()
        if exception is not None:
            _LOGGER.warning("Local catalog auto-refresh failed: %s", exception)
    if pending:
        _BACKGROUND_REFRESH_TASKS.add(refresh_task)
        refresh_task.add_done_callback(_BACKGROUND_REFRESH_TASKS.discard)


async def _set_connection_enabled(state: Any, params: JsonObject) -> JsonObject:
    """Enable or disable one provider connection (persisted settings override).

    Enabling a local auto-refresh connection also forces an immediate catalog
    probe so the caller gets live reachability feedback ("enabled, but the
    service is not running" is a valid, reported outcome — the enable sticks).
    """

    _reject_unsupported(
        params, {"provider_id", "connection_id", "enabled"}, "connection set-enabled"
    )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.enabled must be a boolean")

    try:
        runtime = state.runtime
        local_connection_id, account_id = split_connection_id(provider_id, connection_id)
        if account_id is not None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "connection set-enabled targets a connection, not an account",
            )
        connection = _provider_connection(runtime, provider_id, connection_id)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        was_enabled = runtime.provider_credentials.is_connection_enabled(
            provider_id, public_connection_id
        )
        runtime.storage.set_provider_connection_enabled(public_connection_id, enabled)

        reachable: bool | None = None
        if enabled and getattr(connection, "auto_refresh", False):
            await runtime.maybe_refresh_local_catalogs(force=True)
            reachable = _connection_reachability(runtime, public_connection_id)
        configured = runtime.provider_credentials.has_credentials(provider_id, public_connection_id)
        usable = runtime.provider_credentials.is_usable(provider_id, public_connection_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # An enable/disable immediately alters which models are selectable.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    response: JsonObject = {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "enabled": enabled,
        "configured": configured,
    }
    if getattr(connection, "auto_refresh", False):
        response["reachable"] = reachable
    if was_enabled != enabled:
        _LOGGER.info(
            "Provider connection %s (provider=%s connection=%s configured=%s usable=%s%s)",
            "enabled" if enabled else "disabled",
            provider_id,
            connection.id,
            configured,
            usable,
            f" reachable={reachable}" if reachable is not None else "",
        )
    return response


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


def custom_provider_items(runtime: Any) -> list[JsonObject]:
    """Return secret-free Custom Provider records with live runtime state."""

    loader = getattr(runtime.storage, "load_custom_providers_settings", None)
    if not callable(loader):
        return []
    providers = loader()
    items: list[JsonObject] = []
    for provider_id, provider in sorted(providers.items()):
        connection_id = f"{provider_id}:default"
        items.append(
            {
                "id": provider_id,
                **provider,
                "connection_id": connection_id,
                "credentials_configured": runtime.provider_credentials.has_credentials(
                    provider_id,
                    connection_id,
                ),
                "usable": runtime.provider_credentials.is_usable(
                    provider_id,
                    connection_id,
                ),
                "model_count": len(runtime.models.list_for_provider(provider_id)),
            }
        )
    return items


def _list_custom_providers(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "provider.custom_list does not accept params")
    return {"providers": custom_provider_items(state.runtime)}


def _save_custom_provider(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider", "api_key"}, "provider custom-save")
    raw_provider = params.get("provider")
    if not isinstance(raw_provider, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.provider must be an object")

    raw_provider_id = raw_provider.get("id")
    try:
        provider_id = normalize_custom_provider_id(raw_provider_id)
        provider = normalize_custom_provider_settings(
            provider_id,
            {key: value for key, value in raw_provider.items() if key != "id"},
        )
    except Exception as exc:
        if isinstance(exc, RpcError):
            raise
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    api_key = params.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.api_key must be a non-empty string")
    if api_key is not None and provider["auth"] != "api_key":
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.api_key is only supported when provider.auth is 'api_key'",
        )

    runtime = state.runtime
    existing_custom = runtime.storage.load_custom_providers_settings()
    if provider_id not in existing_custom:
        try:
            existing_provider = runtime.providers.get(provider_id)
        except KeyError:
            existing_provider = None
        if existing_provider is not None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"Custom Provider id '{provider_id}' conflicts with a bundled Provider",
            )

    previous = existing_custom.get(provider_id)
    try:
        runtime.storage.save_custom_provider_settings(provider_id, provider)
        runtime.reload_custom_providers()
        if api_key is not None:
            runtime.storage.set_data_dir_credential(
                custom_provider_credential_key(provider_id),
                api_key.strip(),
            )
        elif previous is not None and previous["auth"] == "api_key" and provider["auth"] == "none":
            _remove_custom_provider_credentials(runtime, provider_id)
        runtime.reload_environment_credentials()
        item = next(item for item in custom_provider_items(runtime) if item["id"] == provider_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    publish_resource_changed(state, RESOURCE_KIND_MODELS)
    _LOGGER.info(
        "Custom Provider saved (provider=%s auth=%s models=%s)",
        provider_id,
        provider["auth"],
        len(provider["models"]),
    )
    return {"provider": item}


def _delete_custom_provider(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider_id"}, "provider custom-delete")
    try:
        provider_id = normalize_custom_provider_id(_required_string(params, "provider_id"))
    except Exception as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    runtime = state.runtime
    try:
        removed = runtime.storage.delete_custom_provider_settings(provider_id)
        if removed is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"Custom Provider '{provider_id}' does not exist",
            )
        credentials_removed = _remove_custom_provider_credentials(runtime, provider_id)
        runtime.reload_custom_providers()
        runtime.reload_environment_credentials()
    except RpcError:
        raise
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    publish_resource_changed(state, RESOURCE_KIND_MODELS)
    _LOGGER.info("Custom Provider deleted (provider=%s)", provider_id)
    return {
        "provider_id": provider_id,
        "deleted": True,
        "credentials_removed": credentials_removed,
    }


def _remove_custom_provider_credentials(runtime: Any, provider_id: str) -> int:
    base_key = custom_provider_credential_key(provider_id)
    account_prefix = f"{base_key}{CREDENTIAL_KEY_ACCOUNT_SEPARATOR}"
    credential_keys = [
        key
        for key in runtime.storage.load_environment()
        if key == base_key or key.startswith(account_prefix)
    ]
    return sum(bool(runtime.storage.remove_data_dir_credential(key)) for key in credential_keys)


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
    _reject_unsupported(
        params, {"provider_id", "connection_id", "value", "account"}, "provider set-key"
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
        previous_value = runtime.storage.load_environment().get(credential_key)
        runtime.storage.set_data_dir_credential(credential_key, value)
        runtime.reload_environment_credentials()
        account_connection_id = compose_connection_id(provider_id, connection.id, account_id)
        usable = runtime.provider_credentials.is_usable(provider_id, account_connection_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # A credential change immediately alters which models are selectable.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    if previous_value != value:
        _LOGGER.info(
            "Provider credential saved (provider=%s connection=%s configured=true usable=%s)",
            provider_id,
            connection.id,
            usable,
        )
    return {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "account": account_id,
        "credential_key": credential_key,
        "configured": True,
    }


def _unset_provider_key(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider_id", "connection_id", "account"}, "provider unset-key")

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
        runtime.reload_environment_credentials()
        configured = runtime.provider_credentials.has_credentials(
            provider_id,
            compose_connection_id(provider_id, connection.id, account_id),
        )
        usable = runtime.provider_credentials.is_usable(
            provider_id,
            compose_connection_id(provider_id, connection.id, account_id),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # A credential change immediately alters which models are selectable.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    if removed:
        _LOGGER.info(
            "Provider credential removed (provider=%s connection=%s configured=%s usable=%s)",
            provider_id,
            connection.id,
            configured,
            usable,
        )
    return {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "account": account_id,
        "credential_key": credential_key,
        "removed": removed,
        "configured": configured,
    }


async def _refresh_model_db(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"expected_resources_dir", "provider_id", "target"},
        "model refresh",
    )

    database_refresh: ModelDatabaseRefresh | None = None
    refresh_resources_dir: Path | None = None
    try:
        runtime = state.runtime
        target = params.get("target", _MODEL_REFRESH_TARGET_RUNTIME)
        if not isinstance(target, str) or target not in _MODEL_REFRESH_TARGETS:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "model refresh target must be 'runtime' or 'system'",
            )
        system_resources_dir = _runtime_resources_dir(runtime)
        data_dir = Path(runtime.storage.data_dir)
        if target == _MODEL_REFRESH_TARGET_RUNTIME:
            if "expected_resources_dir" in params:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    "expected_resources_dir is only valid for a system Model DB refresh",
                )
            database_refresh = begin_runtime_model_database_refresh(
                system_resources_dir,
                data_dir,
            )
            refresh_resources_dir = database_refresh.resources_dir
        else:
            expected_resources_dir = _required_string(params, "expected_resources_dir")
            if Path(expected_resources_dir).resolve() != system_resources_dir.resolve():
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    "the serving checkout does not match the requested system Model DB",
                )
            database_refresh = begin_system_model_database_refresh(system_resources_dir)
            refresh_resources_dir = database_refresh.resources_dir
        if "provider_id" in params:
            provider_id = _required_string(params, "provider_id")
            result = await _refresh_provider_model_db(
                runtime,
                provider_id,
                refresh_resources_dir,
            )
            scope = provider_id
            provider_count = 1
            model_count = _model_count(result)
        else:
            result = await _refresh_global_model_db(runtime, refresh_resources_dir)
            scope = "all"
            provider_count = int(result.get("refreshed_count", 0))
            model_count = int(result.get("model_count", 0))
        ModelRegistry.invalidate(refresh_resources_dir)
        ModelRegistry.load(refresh_resources_dir)
        ModelRegistry.invalidate(refresh_resources_dir)
        database_refresh.commit()
        _reload_runtime_model_registry(runtime, system_resources_dir)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    finally:
        if refresh_resources_dir is not None:
            ModelRegistry.invalidate(refresh_resources_dir)
        if database_refresh is not None:
            database_refresh.discard()
    # Both refresh paths reloaded the registry in place; tell open windows to
    # reload their model lists. Single tail emit so the per-provider early
    # return cannot skip the signal.
    publish_resource_changed(state, RESOURCE_KIND_MODELS)
    errors = result.get("errors")
    error_count = len(errors) if isinstance(errors, list) else 0
    _LOGGER.info(
        "Model database refreshed (scope=%s target=%s providers=%s models=%s errors=%s)",
        scope,
        target,
        provider_count,
        model_count,
        error_count,
    )
    return result


async def _connect_provider(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider_id", "connection_id", "account"}, "provider connect")

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
            # The targeted auth event drives the OAuth modal; a successful login
            # also newly enables this provider's models, so signal the generic
            # reload alongside it (only on success — connect-start does not).
            if success:
                publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)

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
    _reject_unsupported(params, {"provider_id", "connection_id", "account"}, "provider disconnect")

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    account = _account_param(params)

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        token_store = _runtime_token_store(state.runtime)
        had_token = token_store.load(provider_id, connection.id, account_id=account_id) is not None
        engine = getattr(state, "device_flow_engine", None)
        flow_active = _device_flow_active(engine, provider_id, connection.id, account_id)
        token_store.delete(provider_id, connection.id, account_id=account_id)
        if engine is not None:
            engine.cancel_flow(provider_id, connection.id, account_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # Dropping a connection immediately alters which models are selectable.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    if had_token or flow_active:
        _LOGGER.info(
            "OAuth provider disconnected (provider=%s connection=%s)",
            provider_id,
            connection.id,
        )
    return {
        "provider_id": provider_id,
        "connection_id": compose_connection_id(provider_id, connection.id),
        "account": account_id,
        "status": "disconnected",
    }


def _provider_connection_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"provider_id", "connection_id", "account"}, "provider connection status"
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
    provider_count, model_count = _summarize_refreshed_providers(refreshed_providers)
    result: JsonObject = {
        "providers": refreshed_providers,
        "refreshed_count": provider_count,
        "model_count": model_count,
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

    Connections without an effective ``models_endpoint`` are skipped.
    Credentialed catalogs also require a usable Connection; explicitly public
    catalogs refresh without a credential while inference usability remains
    unchanged. Successful refreshes accumulate into the shared
    ``<provider>.json`` catalog via discovery's merge logic. The
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
        credential_value = ""
        if getattr(connection, "catalog_requires_credentials", True):
            if not runtime.provider_credentials.is_usable(provider_id, connection_id):
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
            discovery_connection = connection
            if provider_id == "github-copilot":
                token_extra_reader = getattr(runtime, "get_connection_token_extra", None)
                token_extra = (
                    token_extra_reader(provider_id, connection_id)
                    if callable(token_extra_reader)
                    else {}
                )
                copilot_endpoint = token_extra.get(COPILOT_API_ENDPOINT_EXTRA_KEY)
                if copilot_endpoint:
                    discovery_connection = replace(connection, base_url=copilot_endpoint)
            result = await refresh_models(
                provider,
                credential_value,
                resources_dir,
                credential_connection=discovery_connection,
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


def _reload_runtime_model_registry(runtime: Any, system_resources_dir: Path) -> None:
    # Reload in place rather than rebinding ``runtime._models``: services that
    # captured the registry at construction (task-model targets for
    # speech/image/embeddings, the status display, the recall backend) hold the
    # same instance, so an in-place swap reaches all of them without re-wiring.
    reload_options: dict[str, Any] = {
        "runtime_models_dir": runtime.storage.layout.models,
    }
    custom_loader = getattr(runtime.storage, "load_custom_providers_settings", None)
    if callable(custom_loader):
        reload_options["custom_providers"] = custom_loader()
    runtime.models.reload(system_resources_dir, **reload_options)


def _model_count(result: JsonObject) -> int:
    model_count = result.get("model_count", 0)
    if isinstance(model_count, bool) or not isinstance(model_count, int):
        return 0
    return int(model_count)


def _summarize_refreshed_providers(successes: list[JsonObject]) -> tuple[int, int]:
    """Collapse per-connection refresh results to ``(providers, models)``.

    ``_refresh_provider_connections`` emits one entry per refreshed *connection*,
    and each entry's ``model_count`` is the size of the provider's full catalog
    after that connection merged into it — not just the models that connection
    contributed. Counting entries would overstate the provider total for a
    provider with several connections, and summing every ``model_count`` would
    count the shared catalog once per connection. Both collapse to one value per
    provider: the provider total is the number of distinct providers, and a
    provider's model total is the size it reported on its last (so most
    complete) write, which is what the catalog file actually holds.
    """

    per_provider: dict[str, int] = {}
    for entry in successes:
        provider_id = entry.get("provider_id")
        if not isinstance(provider_id, str):
            continue
        per_provider[provider_id] = _model_count(entry)
    return len(per_provider), sum(per_provider.values())


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return connection/provider/model RPC handlers."""

    return {
        "connection.list": _list_connections,
        "connection.set_enabled": _set_connection_enabled,
        "model.get": _get_model,
        "model.list": _list_models,
        "model.refresh_db": _refresh_model_db,
        "provider.routing_options": _routing_provider_options,
        "provider.custom_list": _list_custom_providers,
        "provider.custom_save": _save_custom_provider,
        "provider.custom_delete": _delete_custom_provider,
        "provider.set_key": _set_provider_key,
        "provider.unset_key": _unset_provider_key,
        "provider.connect": _connect_provider,
        "provider.disconnect": _disconnect_provider,
        "provider.connection_status": _provider_connection_status,
    }
