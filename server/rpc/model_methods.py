"""Model methods."""

from __future__ import annotations

import asyncio
import logging
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from core.models.query import ModelQuery
from core.providers.accounts import ConnectionRef
from core.utils.errors import ConfigError
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_MODELS
from server.rpc._mutations import state_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.payloads import (
    _model_detail_response,
    _model_response,
    _wire_profile_detail_response,
    _wire_status_response,
)
from server.rpc.provider_access import (
    _connection_reachability,
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
        "query",
    )
)

_MODEL_REFRESH_TARGET_RUNTIME = "runtime"

_MODEL_REFRESH_TARGET_SYSTEM = "system"

_MODEL_REFRESH_TARGETS = frozenset({_MODEL_REFRESH_TARGET_RUNTIME, _MODEL_REFRESH_TARGET_SYSTEM})

LOCAL_CATALOG_REFRESH_WAIT_SECONDS = 3.0

_MODEL_READ_WORKERS = BoundedWorkerPool(name="model-access", max_workers=4)


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
    # A Provider mutation publishes Settings, credentials and registries as one read boundary.
    async with state_lock(state, "_settings_mutation_lock"):
        try:
            runtime = state.runtime
            selected_models = runtime.models.query(model_query)
            local_context_windows, usable_connections_by_provider = await _MODEL_READ_WORKERS.run(
                _model_access_snapshot, runtime, {provider_id for provider_id, _ in selected_models}
            )

            models = []
            for provider_id, model in selected_models:
                allowed_connections = [
                    connection
                    for connection in usable_connections_by_provider[provider_id]
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
                response["wire_profiles"] = {
                    connection.id: {
                        **_wire_status_response(
                            *runtime.wire_status(provider_id, connection.id, model.model_id)
                        ),
                        **(
                            {"fast_mode": True}
                            if runtime.wire_profile(
                                provider_id, connection.id, model.model_id
                            ).request.fast_mode
                            else {}
                        ),
                    }
                    for connection in allowed_connections
                }
                if response["wire_profiles"][allowed_connections[0].id].get("fast_mode"):
                    response["fast_mode"] = True
                reachable = _model_reachability(runtime, provider_id, allowed_connections)
                if reachable is not None:
                    response["reachable"] = reachable
                models.append(response)
            models.sort(key=lambda model: (model["provider_id"], model["model_id"]))
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return {"models": models}


def _model_access_snapshot(
    runtime: Any, provider_ids: set[str]
) -> tuple[Any, dict[str, list[Any]]]:
    """Read credential/settings projections together, without loop-owned wire state."""
    windows = runtime.local_context_windows()
    usable: dict[str, list[Any]] = {}
    for provider_id in provider_ids:
        provider = _provider_config_or_none(runtime, provider_id)
        usable[provider_id] = [
            connection
            for connection in getattr(provider, "connections", ())
            if runtime.provider_credentials.is_usable(provider_id, f"{provider_id}:{connection.id}")
        ]
    return windows, usable


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
    # A Provider mutation publishes Settings, credentials and registries as one read boundary.
    async with state_lock(state, "_settings_mutation_lock"):
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

        local_context_windows, usable_connections = await _MODEL_READ_WORKERS.run(
            _model_access_snapshot, runtime, {provider_id}
        )
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
            if connection in usable_connections[provider_id]
        ]
        response["wire_profiles"] = {
            connection_id: _wire_profile_detail_response(
                runtime.wire_profile(provider_id, connection_id, model.model_id),
                runtime.learned_wire_facts(provider_id, connection_id, model.model_id),
            )
            for connection_id in response["usable_connections"]
        }
        reachable = _model_reachability(runtime, provider_id, allowed_connections)
        if reachable is not None:
            response["reachable"] = reachable
        return {"model": response}


async def _forget_wire_facts(state: Any, params: JsonObject) -> JsonObject:
    """Drop learned wire facts for a Provider or one of its Models, optionally on one Connection."""

    _reject_unsupported(params, {"model", "connection"}, "model.forget_wire_facts")
    reference = _required_string(params, "model")
    bare_reference = reference.rpartition("::")[0] if "::" in reference else reference
    provider_id, _separator, model_id = bare_reference.partition("/")
    connection_id = params.get("connection")
    if connection_id is not None and (not isinstance(connection_id, str) or not connection_id):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.connection must be a non-empty string")
    runtime = state.runtime
    provider = _provider_config_or_none(runtime, provider_id)
    if provider is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"unknown Provider {provider_id!r}")
    if connection_id is not None:
        connection_id = connection_id.removeprefix(f"{provider_id}:")
        known = [connection.id for connection in getattr(provider, "connections", ())]
        if connection_id not in known:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"unknown Connection {connection_id!r} of {provider_id!r}; "
                f"choices: {', '.join(known)}",
            )
    forgotten = runtime.forget_wire_facts(provider_id, connection_id, model_id or None)
    return {
        "provider_id": provider_id,
        "model_id": model_id or None,
        "connection_id": connection_id,
        "forgotten": forgotten,
    }


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

    adapter: Any = None
    try:
        runtime = state.runtime
        _windows, usable = await _MODEL_READ_WORKERS.run(
            _model_access_snapshot, runtime, {provider_id}
        )
        if not usable[provider_id]:
            raise ConfigError("OpenRouter has no usable Connection")
        connection_id = f"{provider_id}:{usable[provider_id][0].id}"

        def prepare() -> None:
            nonlocal adapter
            adapter = runtime.get_adapter(ConnectionRef(provider_id, connection_id))

        await _MODEL_READ_WORKERS.run(prepare)
        loader = getattr(adapter, "routing_provider_options", None)
        if not callable(loader):
            raise ConfigError("OpenRouter Adapter does not expose routing options")
        providers = await loader(model_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    finally:
        if adapter is not None:
            await adapter.aclose()
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
    except KeyError, AttributeError:
        return None


async def _await_local_catalog_refresh(runtime: Any) -> None:
    refresh_task = runtime.start_local_catalog_refresh()
    # ``asyncio.wait`` (unlike ``wait_for``) does not cancel on timeout — the
    # sweep keeps running in the background and later calls see its result.
    done, _pending = await asyncio.wait({refresh_task}, timeout=LOCAL_CATALOG_REFRESH_WAIT_SECONDS)
    for task in done:
        # The runtime method is designed never to raise; consume a defensive
        # surprise so it degrades to a logged stale-catalog listing.
        exception = task.exception()
        if exception is not None:
            _LOGGER.warning("Local catalog auto-refresh failed: %s", exception)


async def _refresh_model_db(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"expected_resources_dir", "provider_id", "target"}, "model refresh"
    )
    try:
        runtime = state.runtime
        target = params.get("target", _MODEL_REFRESH_TARGET_RUNTIME)
        if not isinstance(target, str) or target not in _MODEL_REFRESH_TARGETS:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, "model refresh target must be 'runtime' or 'system'"
            )
        if target == _MODEL_REFRESH_TARGET_RUNTIME:
            if "expected_resources_dir" in params:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    "expected_resources_dir is only valid for a system Model DB refresh",
                )
        else:
            expected_resources_dir = _required_string(params, "expected_resources_dir")
            if not await _MODEL_READ_WORKERS.run(
                lambda: (
                    Path(expected_resources_dir).resolve()
                    == runtime.storage.resources_dir.resolve()
                )
            ):
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    "the serving checkout does not match the requested system Model DB",
                )
        provider_id = _required_string(params, "provider_id") if "provider_id" in params else None
        result: JsonObject = await runtime.refresh_model_database(
            provider_id=provider_id, target=target
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Manual refresh always signals success, even when the effective catalog is unchanged.
    publish_resource_changed(state, RESOURCE_KIND_MODELS)
    return result


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the registered model RPC handlers."""
    return {
        "model.forget_wire_facts": _forget_wire_facts,
        "model.get": _get_model,
        "model.list": _list_models,
        "model.refresh_db": _refresh_model_db,
        "provider.routing_options": _routing_provider_options,
    }
