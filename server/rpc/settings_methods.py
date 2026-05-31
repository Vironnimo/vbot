"""Settings and task-model RPC handlers."""

from __future__ import annotations

from contextlib import suppress
from typing import Any, cast

from core.recall.recall import FIRST_PARTY_RECALL_BACKENDS
from core.settings import SettingsValidationError, parse_settings_update
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.provider_access import _provider_has_credentials, _provider_settings_connection
from server.rpc.validation import _required_string

JsonObject = dict[str, Any]
SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)


def _get_settings_raw(state: Any, params: JsonObject) -> JsonObject:
    try:
        settings = state.runtime.storage.load_settings()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"settings": dict(settings)}


def _set_settings_key(state: Any, params: JsonObject) -> JsonObject:
    if "key" not in params or "value" not in params:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "settings.set_key requires 'key' and 'value'",
        )

    key = _required_string(params, "key")
    value = params["value"]

    try:
        settings = dict(state.runtime.storage.load_settings())
        settings[key] = value
        state.runtime.storage.save_settings(settings)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {"settings": settings}


def _get_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "settings.get does not accept params")
    try:
        return _settings_response(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _update_settings(state: Any, params: JsonObject) -> JsonObject:
    try:
        settings_update = parse_settings_update(params)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    storage = state.runtime.storage
    original_settings: JsonObject | None = None
    should_reload_recall_backend = False
    should_reload_skills = False

    try:
        original_settings = dict(storage.load_settings())
        if "appearance" in settings_update:
            storage.update_appearance_settings(settings_update["appearance"])
        if "skills" in settings_update:
            storage.update_skill_directory_settings(settings_update["skills"]["directories"])
            should_reload_skills = True
        if "subagents" in settings_update:
            _update_subagent_settings(storage, settings_update["subagents"])
        if "compaction" in settings_update:
            storage.update_compaction_settings(settings_update["compaction"])
        if "defaults" in settings_update:
            defaults_update = cast(JsonObject, settings_update["defaults"])
            if "agent" in defaults_update:
                storage.update_defaults("agent", defaults_update["agent"])
        if "recall" in settings_update:
            storage.update_recall_settings(settings_update["recall"])
            should_reload_recall_backend = True
        if "model_tasks" in settings_update:
            storage.update_model_task_settings(settings_update["model_tasks"])
        if should_reload_skills:
            reload_skills = getattr(state.runtime, "reload_skills", None)
            if callable(reload_skills):
                reload_skills()
        if should_reload_recall_backend:
            reload_recall_backend = getattr(state.runtime, "reload_recall_backend", None)
            if callable(reload_recall_backend):
                reload_recall_backend()
        return _settings_response(state)
    except Exception as exc:
        if original_settings is not None:
            with suppress(Exception):
                storage.save_settings(original_settings)
        raise _map_expected_error(exc) from exc


def _task_model_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "task_model.settings does not accept params")
    try:
        return {"model_tasks": state.runtime.model_tasks.settings()}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _task_model_update(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"model_tasks"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported task_model.update fields: {', '.join(unsupported_fields)}",
        )
    try:
        settings_update = parse_settings_update({"model_tasks": params.get("model_tasks")})
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    try:
        model_tasks = state.runtime.model_tasks.update(settings_update["model_tasks"])
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"model_tasks": model_tasks}


def _task_model_list_targets(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"task_type"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported task_model.list_targets fields: {', '.join(unsupported_fields)}",
        )
    task_type = _required_string(params, "task_type")
    try:
        targets = state.runtime.model_tasks.list_targets(task_type)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"targets": [target.to_dict() for target in targets]}


def _task_model_options(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"task_type", "target"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported task_model.options fields: {', '.join(unsupported_fields)}",
        )
    task_type = _required_string(params, "task_type")
    target = _required_string(params, "target")
    try:
        schema = state.runtime.model_tasks.options(task_type, target)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"schema": schema.to_dict()}


def _settings_response(state: Any) -> JsonObject:
    runtime = state.runtime
    appearance = runtime.storage.load_appearance_settings()
    subagents = runtime.storage.load_subagent_settings()
    compaction = runtime.storage.load_compaction_settings()
    recall = runtime.storage.load_recall_settings()
    model_tasks = runtime.storage.load_model_task_settings()
    defaults = runtime.storage.load_defaults()
    server_bind = _server_bind_response(state)

    response = {
        "general": {
            "server": server_bind,
            "data_directory": str(runtime.storage.data_dir),
        },
        "providers": {
            "items": [
                _provider_settings_item(runtime, provider_id)
                for provider_id in runtime.providers.list_ids()
            ],
            "custom_endpoints": {
                "supported": False,
                "items": [],
            },
        },
        "appearance": {
            "language": appearance["language"],
            "available_languages": runtime.storage.supported_appearance_languages(),
        },
        "defaults": defaults,
        "subagents": {field: subagents[field] for field in SUBAGENT_SETTING_FIELDS},
        "compaction": dict(compaction),
        "recall": {
            "backend": recall["backend"],
            "available_backends": sorted(FIRST_PARTY_RECALL_BACKENDS),
        },
        "model_tasks": model_tasks,
    }
    skill_directory_loader = getattr(runtime.storage, "load_skill_directory_settings", None)
    if callable(skill_directory_loader):
        response["skills"] = {
            "default_directory": str(runtime.storage.data_dir / "skills"),
            "directories": skill_directory_loader(),
        }
    return response


def _update_subagent_settings(storage: Any, subagents: JsonObject) -> None:
    settings = storage.load_settings()
    merged_settings = dict(settings)
    for field in SUBAGENT_SETTING_FIELDS:
        merged_settings[field] = subagents[field]
    storage.save_settings(merged_settings)


def _server_bind_response(state: Any) -> JsonObject:
    server_bind = getattr(state, "server_bind", {})
    listen_host = server_bind.get("listen_host", "127.0.0.1")
    listen_port = server_bind.get("listen_port", 8420)
    port_source = server_bind.get("port_source", "default")
    return {
        "listen_host": listen_host,
        "listen_port": listen_port,
        "port_source": port_source,
    }


def _provider_settings_item(runtime: Any, provider_id: str) -> JsonObject:
    provider = runtime.providers.get(provider_id)
    credentials_configured = _provider_has_credentials(runtime, provider_id)
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "models_endpoint": getattr(provider, "models_endpoint", None),
        "connections": [
            _provider_settings_connection(runtime, provider.id, connection)
            for connection in provider.connections
        ],
        "credentials_configured": credentials_configured,
        "status": "configured" if credentials_configured else "missing_credentials",
        "model_count": len(runtime.models.list_for_provider(provider_id)),
        "kind": "remote" if provider.base_url else "local",
        "editable": False,
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return settings and task-model RPC handlers."""

    return {
        "settings.get_raw": _get_settings_raw,
        "settings.set_key": _set_settings_key,
        "settings.get": _get_settings,
        "settings.update": _update_settings,
        "task_model.settings": _task_model_settings,
        "task_model.update": _task_model_update,
        "task_model.list_targets": _task_model_list_targets,
        "task_model.options": _task_model_options,
    }
