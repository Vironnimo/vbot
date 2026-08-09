"""Settings and task-model RPC handlers."""

from __future__ import annotations

import json
from contextlib import suppress
from copy import deepcopy
from typing import Any

from core.debug.store import DebugTraceStore
from core.extensions import validate_extension_config
from core.model_tasks import (
    SUPPORTED_TASK_TYPES,
    TaskModelError,
    validate_task_type,
)
from core.recall.recall import FIRST_PARTY_RECALL_BACKENDS
from core.search_config import FIRST_PARTY_WEB_SEARCH_PROVIDERS
from core.settings import (
    APPLICATION_RESTART,
    SettingsPatchOperation,
    SettingsPathError,
    SettingsValidationError,
    apply_settings_patch,
    build_effective_settings,
    catalog_payload,
    parse_patch_operations,
    parse_settings_path,
    parse_settings_update,
    setting_details,
)
from core.utils.logging import get_logger
from server.events import RESOURCE_KIND_COMMANDS
from server.rpc.connection_methods import custom_provider_items
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.provider_access import _provider_has_credentials, _provider_settings_connection
from server.rpc.validation import (
    _ensure_model_connection_supported,
    _reject_unsupported,
    _required_string,
)

JsonObject = dict[str, Any]
_LOGGER = get_logger("server.rpc.settings")
_MISSING = object()
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


def _get_public_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "settings.values does not accept params")
    try:
        values = build_effective_settings(state.runtime.storage.load_settings())
        _apply_runtime_values(state, values)
        return {"settings": values}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _settings_catalog(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"prefix"}, "settings.catalog")
    prefix = params.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.prefix must be a string")
    try:
        entries = catalog_payload(prefix)
        raw_settings = state.runtime.storage.load_settings()
        for entry in entries:
            if "<" in str(entry.get("path", "")):
                continue
            details = _runtime_setting_details(state, raw_settings, str(entry["path"]), True)
            entry.update(
                {
                    key: value
                    for key, value in details.items()
                    if key
                    in {
                        "value",
                        "configured",
                        "configured_value",
                        "source",
                        "restart_required",
                    }
                }
            )
        entries.extend(_dynamic_catalog_entries(state, raw_settings, prefix))
        if prefix and not entries:
            raise SettingsPathError(f"no settings paths match {prefix!r}")
        return {"settings": entries}
    except SettingsPathError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _get_setting_path(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"path", "allow_missing"}, "settings.get_path")
    path = _required_string(params, "path")
    allow_missing = params.get("allow_missing", False)
    if not isinstance(allow_missing, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.allow_missing must be a boolean")
    try:
        return {
            "setting": _runtime_setting_details(
                state,
                state.runtime.storage.load_settings(),
                path,
                allow_missing,
            )
        }
    except SettingsPathError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _patch_setting_paths(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"operations"}, "settings.patch")
    try:
        operations = parse_patch_operations(params.get("operations"))
    except SettingsPathError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    storage = state.runtime.storage

    def mutate(raw_settings: JsonObject) -> tuple[JsonObject, JsonObject, tuple[str, ...]]:
        previous = deepcopy(raw_settings)
        candidate, changed_paths = apply_settings_patch(raw_settings, operations)
        _validate_public_settings_candidate(state.runtime, candidate, operations)
        raw_settings.clear()
        raw_settings.update(candidate)
        return previous, candidate, changed_paths

    try:
        previous, saved, changed_paths = storage.update_settings(mutate)
        commands_changed = await _apply_public_settings_delta(
            state.runtime,
            previous,
            saved,
        )
        changes = [
            _runtime_setting_details(state, saved, operation.resolved.canonical_path, True)
            for operation in operations
        ]
    except SettingsPathError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if changed_paths:
        _LOGGER.info("Settings paths updated (paths=%s)", ",".join(changed_paths))
    if commands_changed:
        publish_resource_changed(state, RESOURCE_KIND_COMMANDS)
    return {
        "changed": list(changed_paths),
        "changes": changes,
        "restart_required": any(change.get("restart_required") is True for change in changes),
    }


def _get_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "settings.get does not accept params")
    try:
        return _settings_response(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _update_settings(state: Any, params: JsonObject) -> JsonObject:
    try:
        settings_update = parse_settings_update(params)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    storage = state.runtime.storage
    previous_settings = storage.load_settings()

    if "recall" in settings_update:
        _validate_recall_backend_known(state.runtime, settings_update["recall"]["backend"])

    _validate_model_connections(state.runtime.models, settings_update)
    if "model_tasks" in settings_update:
        try:
            state.runtime.model_tasks.validate_update(settings_update["model_tasks"])
        except Exception as exc:
            raise _map_expected_error(exc) from exc

    newly_enabled: set[str] = set()
    newly_disabled: set[str] = set()
    if "extensions" in settings_update:
        _validate_extension_configs(state.runtime, settings_update["extensions"])
        # Enabling routes through the full extension reload (it reads the freshly
        # persisted state, so it also applies any name disabled in the same save);
        # a disable-only change takes the surgical live-disable path. Config-value
        # changes are read live via ExtensionAPI.get_config() and touch neither set.
        previous = storage.load_extensions_settings()
        old_disabled = set(previous.get("disabled", []))
        new_disabled = set(settings_update["extensions"].get("disabled", []))
        newly_enabled = old_disabled - new_disabled
        newly_disabled = new_disabled - old_disabled

    try:
        storage.update_settings_sections(settings_update)
        saved_settings = storage.load_settings()
        commands_changed = await _apply_public_settings_delta(
            state.runtime,
            previous_settings,
            saved_settings,
            force_roots=set(settings_update),
        )
        response = _settings_response(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    changed_sections = sorted(
        section
        for section in settings_update
        if previous_settings.get(section, _MISSING) != saved_settings.get(section, _MISSING)
    )
    logged_sections = [section for section in changed_sections if section != "appearance"]
    if logged_sections:
        details = ""
        if newly_enabled:
            details += f" extensions_enabled={','.join(sorted(newly_enabled))}"
        if newly_disabled:
            details += f" extensions_disabled={','.join(sorted(newly_disabled))}"
        _LOGGER.info(
            "Settings updated (sections=%s%s)",
            ",".join(logged_sections),
            details,
        )
    if commands_changed:
        publish_resource_changed(state, RESOURCE_KIND_COMMANDS)
    return response


async def _apply_extension_delta(
    runtime: Any, newly_enabled: set[str], newly_disabled: set[str]
) -> bool:
    """Apply the extensions disabled-set delta live after the write is persisted.

    Enabling any name rebuilds the whole extension layer (``reload_extensions``),
    which reads the freshly persisted state and therefore also applies any names
    disabled in the same save — so a mixed save reloads and does nothing else. A
    disable-only save takes the surgical live-disable path, leaving other
    extensions untouched; a config-value-only change does neither (live reads
    cover it). The defensive ``getattr``/``callable`` guards keep the handler
    working against runtime test stubs that omit these seams.
    """
    if newly_enabled:
        reload_extensions = getattr(runtime, "reload_extensions", None)
        if callable(reload_extensions):
            await reload_extensions()
            return True
        return False
    if newly_disabled:
        apply_disabled = getattr(runtime, "apply_extension_disabled_change", None)
        if callable(apply_disabled):
            await apply_disabled(newly_disabled)
            return True
    return False


def _validate_public_settings_candidate(
    runtime: Any,
    candidate: JsonObject,
    operations: list[SettingsPatchOperation],
) -> None:
    """Apply runtime-aware validation before a public path patch is persisted."""

    for operation in operations:
        _reject_secret_extension_path(
            runtime,
            operation.resolved.path.values,
            operation.resolved.canonical_path,
        )
    effective = build_effective_settings(candidate)
    changed_roots = {operation.resolved.path.values[0] for operation in operations}
    if "recall" in changed_roots:
        _validate_recall_backend_known(runtime, effective["recall"]["backend"])
    model_sections = changed_roots & {"defaults", "compaction", "session_titles"}
    if model_sections:
        _validate_model_connections(
            runtime.models,
            {section: effective[section] for section in model_sections},
        )
    if any(
        operation.resolved.path.values[:2] == ("extensions", "config") for operation in operations
    ):
        _validate_extension_configs(runtime, {"config": effective["extensions"]["config"]})
    changed_tasks = {
        operation.resolved.path.values[1]
        for operation in operations
        if operation.resolved.path.values[:1] == ("model_tasks",)
        and len(operation.resolved.path.values) > 1
    }
    for task_type in changed_tasks:
        binding = effective["model_tasks"].get(task_type)
        if isinstance(binding, dict) and binding.get("target"):
            runtime.model_tasks.validate_binding(task_type, binding)
    _validate_changed_provider_connections(runtime, operations)


def _validate_changed_provider_connections(
    runtime: Any,
    operations: list[SettingsPatchOperation],
) -> None:
    known = {
        f"{provider_id}:{connection.id}"
        for provider_id in runtime.providers.list_ids()
        for connection in runtime.providers.get(provider_id).connections
    }
    for operation in operations:
        values = operation.resolved.path.values
        if values[:2] != ("providers", "connections"):
            continue
        connection_key = values[2]
        if connection_key not in known:
            available = ", ".join(sorted(known)) or "none"
            raise SettingsPathError(
                f"unknown Provider Connection {connection_key!r}; available: {available}"
            )


async def _apply_public_settings_delta(
    runtime: Any,
    previous: JsonObject,
    current: JsonObject,
    *,
    force_roots: set[str] | None = None,
) -> bool:
    """Apply the live lifecycle effects owned by changed Settings roots."""

    forced = force_roots or set()
    extension_directories_changed = previous.get("extension_directories") != current.get(
        "extension_directories"
    )
    extensions_changed = previous.get("extensions") != current.get("extensions")
    skill_directories_changed = "skills" in forced or previous.get(
        "skill_directories"
    ) != current.get("skill_directories")

    extension_layer_reloaded = False
    if extension_directories_changed:
        reload_extensions = getattr(runtime, "reload_extensions", None)
        if callable(reload_extensions):
            await reload_extensions()
            extension_layer_reloaded = True
    elif extensions_changed:
        old_disabled = _normalized_disabled(previous)
        new_disabled = _normalized_disabled(current)
        extension_layer_reloaded = await _apply_extension_delta(
            runtime, old_disabled - new_disabled, new_disabled - old_disabled
        )

    if skill_directories_changed and not extension_layer_reloaded:
        reload_skills = getattr(runtime, "reload_skills", None)
        if callable(reload_skills):
            reload_skills()

    recall_changed = "recall" in forced or previous.get("recall") != current.get("recall")
    if recall_changed and not extension_layer_reloaded:
        reload_recall_backend = getattr(runtime, "reload_recall_backend", None)
        if callable(reload_recall_backend):
            reload_recall_backend()
    return extension_layer_reloaded


def _normalized_disabled(settings: JsonObject) -> set[str]:
    extensions = settings.get("extensions")
    if not isinstance(extensions, dict):
        return set()
    disabled = extensions.get("disabled")
    if not isinstance(disabled, list):
        return set()
    return {name for name in disabled if isinstance(name, str)}


def _apply_runtime_values(state: Any, values: JsonObject) -> None:
    """Replace restart-applied configured values with the active runtime values."""

    server_bind = getattr(state, "server_bind", {})
    active_port = server_bind.get("listen_port")
    if isinstance(active_port, int):
        values["server"]["port"] = active_port

    runtime = state.runtime
    with suppress(AttributeError, RuntimeError):
        values["attachments"]["max_size_bytes"] = runtime.attachment_store.max_size_bytes
    with suppress(AttributeError, RuntimeError):
        values["speech"]["upload_max_size_bytes"] = runtime.speech_upload_max_size_bytes


def _runtime_setting_details(
    state: Any,
    raw_settings: JsonObject,
    path: str,
    allow_missing: bool,
) -> JsonObject:
    parsed = parse_settings_path(path)
    values = parsed.values
    _reject_secret_extension_path(state.runtime, values, path)
    details = setting_details(raw_settings, parsed, allow_missing=allow_missing)

    if values == ("server", "port"):
        desired = details.get("value")
        active = getattr(state, "server_bind", {}).get("listen_port", details.get("value"))
        details["value"] = active
        details["source"] = getattr(state, "server_bind", {}).get(
            "port_source", details.get("source", "default")
        )
        _set_restart_state(details, active, desired)
    elif values == ("attachments", "max_size_bytes"):
        desired = details.get("value")
        try:
            active = state.runtime.attachment_store.max_size_bytes
        except (AttributeError, RuntimeError):
            active = details.get("value")
        details["value"] = active
        _set_restart_state(details, active, desired)
    elif values == ("speech", "upload_max_size_bytes"):
        desired = details.get("value")
        try:
            active = state.runtime.speech_upload_max_size_bytes
        except (AttributeError, RuntimeError):
            active = details.get("value")
        details["value"] = active
        _set_restart_state(details, active, desired)
    elif values[:2] == ("providers", "connections"):
        connection_key = values[2]
        provider_id = connection_key.split(":", 1)[0]
        details["value"] = bool(
            state.runtime.provider_credentials.is_connection_enabled(provider_id, connection_key)
        )
        if not details.get("configured"):
            details["source"] = "provider_default"

    details["restart_required"] = bool(
        details.get("restart_required")
        or (
            details.get("application") == APPLICATION_RESTART
            and details.get("configured")
            and details.get("configured_value") != details.get("value")
        )
    )
    return _apply_extension_field_metadata(state.runtime, details, values)


def _set_restart_state(details: JsonObject, active: Any, desired: Any) -> None:
    """Expose the normalized next-start value when it differs from active state."""

    details["restart_required"] = active != desired
    if active != desired:
        details["pending_value"] = deepcopy(desired)
    else:
        details.pop("pending_value", None)


def _extension_field(runtime: Any, values: tuple[str, ...]) -> Any | None:
    if len(values) != 4 or values[:2] != ("extensions", "config"):
        return None
    extension_name, field_name = values[2], values[3]
    registry = getattr(runtime, "extensions", None)
    if registry is None:
        return None
    record = next((item for item in registry.records() if item.name == extension_name), None)
    schema = record.declarations.settings_schema if record is not None else None
    return next((item for item in schema or [] if item.key == field_name), None)


def _reject_secret_extension_path(
    runtime: Any,
    values: tuple[str, ...],
    rendered_path: str,
) -> None:
    field = _extension_field(runtime, values)
    if field is None or field.type != "secret":
        return
    extension_name, field_name = values[2], values[3]
    raise SettingsPathError(
        f"{rendered_path} is a secret; use 'vbot extensions {extension_name} "
        f"set {field_name} --stdin'"
    )


def _apply_extension_field_metadata(
    runtime: Any,
    details: JsonObject,
    values: tuple[str, ...],
) -> JsonObject:
    if len(values) != 4 or values[:2] != ("extensions", "config"):
        return details
    field = _extension_field(runtime, values)
    if field is None:
        return details
    details["type"] = {"text": "string", "number": "number", "toggle": "boolean"}.get(
        field.type, details["type"]
    )
    details["description"] = field.description or field.label
    if field.default is not None:
        details["has_default"] = True
        details["default"] = field.default
        if not details.get("configured"):
            details["value"] = field.default
    return details


def _dynamic_catalog_entries(
    state: Any,
    raw_settings: JsonObject,
    prefix: str | None,
) -> list[JsonObject]:
    paths: set[str] = set()
    for provider_id in state.runtime.providers.list_ids():
        provider = state.runtime.providers.get(provider_id)
        for connection in provider.connections:
            key = f"{provider_id}:{connection.id}"
            paths.add(f"providers.connections[{json.dumps(key, ensure_ascii=False)}]")

    effective = build_effective_settings(raw_settings)
    for model in effective["local_models"]["context_windows"]:
        paths.add(f"local_models.context_windows[{json.dumps(model, ensure_ascii=False)}]")
    for task in set(SUPPORTED_TASK_TYPES) | set(effective["model_tasks"]):
        encoded_task = json.dumps(task, ensure_ascii=False)
        paths.add(f"model_tasks[{encoded_task}].target")
        paths.add(f"model_tasks[{encoded_task}].options")
    for model in effective["providers"]["openrouter"]["routing"]["models"]:
        paths.add(f"providers.openrouter.routing.models[{json.dumps(model, ensure_ascii=False)}]")

    registry = getattr(state.runtime, "extensions", None)
    if registry is not None:
        for record in registry.records():
            for field in record.declarations.settings_schema or []:
                if field.type == "secret":
                    continue
                extension_name = json.dumps(record.name, ensure_ascii=False)
                field_name = json.dumps(field.key, ensure_ascii=False)
                paths.add(f"extensions.config[{extension_name}][{field_name}]")

    entries: list[JsonObject] = []
    normalized_prefix = (prefix or "").strip()
    for path in sorted(paths):
        if normalized_prefix and not path.startswith(normalized_prefix):
            continue
        try:
            entries.append(_runtime_setting_details(state, raw_settings, path, True))
        except SettingsPathError:
            continue
    return entries


def _validate_extension_configs(runtime: Any, extensions_update: JsonObject) -> None:
    """Reject a schema'd extension's config that violates its declared schema.

    Schema validation lives here (not in ``core/settings/``) because this is the
    layer where the loaded registry — and thus the schemas — is available
    (design decision #2). No-schema and not-loaded names pass through unchanged.
    """
    config = extensions_update.get("config", {})
    if not isinstance(config, dict):
        return
    schemas = _loaded_extension_schemas(runtime)
    for name, extension_config in config.items():
        schema = schemas.get(name)
        if schema is None or not isinstance(extension_config, dict):
            continue
        errors = validate_extension_config(schema, extension_config)
        if errors:
            joined = "; ".join(errors)
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"invalid extension config for {name!r}: {joined}",
            )


def _loaded_extension_schemas(runtime: Any) -> dict[str, Any]:
    """Map each loaded extension's name → its declared settings schema (if any)."""
    registry = getattr(runtime, "extensions", None)
    if registry is None:
        return {}
    schemas: dict[str, Any] = {}
    for record in registry.records():
        if record.status != "loaded":
            continue
        schema = record.declarations.settings_schema
        if schema:
            schemas[record.name] = schema
    return schemas


def _available_recall_backends(runtime: Any) -> list[str]:
    """Return selectable recall backend names from the runtime registry.

    The runtime's registry is the source of truth (built-ins + extension
    backends). Falls back to the first-party set when the runtime predates the
    accessor (e.g. a test stub), so the Settings Recall panel still populates.
    """
    getter = getattr(runtime, "available_recall_backends", None)
    if callable(getter):
        return sorted(getter())
    return sorted(FIRST_PARTY_RECALL_BACKENDS)


def _validate_model_connections(models: Any, settings_update: JsonObject) -> None:
    """Reject default-agent and summary models pinned to a forbidden connection."""
    agent_defaults = settings_update.get("defaults", {}).get("agent", {})
    for field in ("model", "fallback_model"):
        value = agent_defaults.get(field)
        if isinstance(value, str):
            _ensure_model_connection_supported(models, f"defaults.agent.{field}", value)

    compaction_strategy = settings_update.get("compaction", {}).get("strategy", {})
    summary_model = (
        compaction_strategy.get("summary_model") if isinstance(compaction_strategy, dict) else None
    )
    if isinstance(summary_model, str):
        _ensure_model_connection_supported(models, "compaction.summary_model", summary_model)

    title_model = settings_update.get("session_titles", {}).get("model")
    if isinstance(title_model, str) and title_model:
        _ensure_model_connection_supported(models, "session_titles.model", title_model)


def _validate_recall_backend_known(runtime: Any, backend: str) -> None:
    """Reject a ``settings.update`` recall backend the registry does not know."""
    available = _available_recall_backends(runtime)
    if backend not in available:
        allowed = ", ".join(available)
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.recall.backend must be one of: {allowed}",
        )


def _task_model_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "task_model.settings does not accept params")
    try:
        return {"model_tasks": state.runtime.model_tasks.settings()}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _task_model_update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"model_tasks"}, "task_model.update")
    try:
        settings_update = parse_settings_update({"model_tasks": params.get("model_tasks")})
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    try:
        previous = state.runtime.model_tasks.settings()
        model_tasks = state.runtime.model_tasks.update(settings_update["model_tasks"])
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    changed_tasks = sorted(
        task_type
        for task_type in set(previous) | set(model_tasks)
        if previous.get(task_type) != model_tasks.get(task_type)
    )
    if changed_tasks:
        _LOGGER.info("Task Model bindings updated (tasks=%s)", ",".join(changed_tasks))
    return {"model_tasks": model_tasks}


def _task_model_list_targets(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"task_type"}, "task_model.list_targets")
    task_type = _required_string(params, "task_type")
    try:
        targets = state.runtime.model_tasks.list_targets(task_type)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"targets": [target.to_dict() for target in targets]}


def _task_model_status(state: Any, params: JsonObject) -> JsonObject:
    """Report whether one configured Task Model is currently executable."""

    _reject_unsupported(params, {"task_type"}, "task_model.status")
    task_type = _required_string(params, "task_type")
    try:
        normalized_task_type = validate_task_type(task_type)
        try:
            state.runtime.model_tasks.binding_for(normalized_task_type)
        except TaskModelError:
            configured = False
        else:
            configured = True
        usable = (
            state.runtime.model_tasks.binding_is_usable(normalized_task_type)
            if configured
            else False
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "task_type": normalized_task_type,
        "configured": configured,
        "usable": usable,
    }


def _task_model_options(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"task_type", "target"}, "task_model.options")
    task_type = _required_string(params, "task_type")
    target = params.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.target must be a non-empty string")
    try:
        binding = None
        if target is None:
            binding = state.runtime.model_tasks.binding_for(task_type)
            target = binding.target
        else:
            target = target.strip()
        schema = state.runtime.model_tasks.options(task_type, target)
        if binding is None:
            with suppress(TaskModelError):
                configured = state.runtime.model_tasks.binding_for(task_type)
                if configured.target == target:
                    binding = configured
        configured_options = dict(binding.options) if binding is not None else {}
        effective_options = schema.default_options()
        effective_options.update(configured_options)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    payload = schema.to_dict()
    payload["configured_options"] = configured_options
    payload["effective_options"] = effective_options
    return {"schema": payload}


def _task_model_patch_options(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"task_type", "set", "unset"}, "task_model.patch_options")
    task_type = _required_string(params, "task_type")
    set_values = params.get("set", {})
    unset_names = params.get("unset", [])
    if not isinstance(set_values, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.set must be an object")
    if not isinstance(unset_names, list) or not all(
        isinstance(name, str) and name.strip() for name in unset_names
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.unset must be an array of non-empty strings",
        )
    normalized_unsets = tuple(name.strip() for name in unset_names)
    overlap = sorted(set(set_values) & set(normalized_unsets))
    if overlap:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"options cannot be set and unset together: {', '.join(overlap)}",
        )
    if not set_values and not normalized_unsets:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "at least one option must be set or unset")
    try:
        previous = state.runtime.model_tasks.settings()
        model_tasks = state.runtime.model_tasks.patch_options(
            task_type,
            set_values=set_values,
            unset_names=normalized_unsets,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if previous.get(task_type) != model_tasks.get(task_type):
        _LOGGER.info("Task Model options updated (task=%s)", task_type)
    return {"model_tasks": model_tasks}


def _settings_response(state: Any) -> JsonObject:
    runtime = state.runtime
    appearance = runtime.storage.load_appearance_settings()
    subagents = runtime.storage.load_subagent_settings()
    compaction = runtime.storage.load_compaction_settings()
    recall = runtime.storage.load_recall_settings()
    web_search = runtime.storage.load_web_search_settings()
    debug = runtime.storage.load_debug_settings()
    reflection = runtime.storage.load_reflection_settings()
    speech = runtime.storage.load_speech_settings()
    model_tasks = runtime.storage.load_model_task_settings()
    session_titles = runtime.storage.load_session_title_settings()
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
                "supported": True,
                "items": custom_provider_items(runtime),
            },
        },
        "appearance": {
            "language": appearance["language"],
            "available_languages": runtime.storage.supported_appearance_languages(),
            "chat_width": appearance["chat_width"],
            "chat_working_mode": appearance["chat_working_mode"],
        },
        "defaults": defaults,
        "subagents": {field: subagents[field] for field in SUBAGENT_SETTING_FIELDS},
        "compaction": dict(compaction),
        "recall": {
            "backend": recall["backend"],
            "available_backends": _available_recall_backends(runtime),
        },
        "web_search": {
            "provider": web_search["provider"],
            "available_providers": sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS),
            "default_count": web_search["default_count"],
            "searxng": dict(web_search["searxng"]),
        },
        "debug": {
            "enabled": debug["enabled"],
            "trace_limit": debug["trace_limit"],
            "trace_count": _trace_count(runtime),
        },
        "reflection": dict(reflection),
        "speech": speech,
        "model_tasks": model_tasks,
        "session_titles": session_titles,
        "local_models": runtime.storage.load_local_models_settings(),
    }
    skill_directory_loader = getattr(runtime.storage, "load_skill_directory_settings", None)
    if callable(skill_directory_loader):
        response["skills"] = {
            "default_directory": str(runtime.storage.data_dir / "skills"),
            "directories": skill_directory_loader(),
        }
    return response


def _trace_count(runtime: Any) -> int:
    """Return the number of stored debug traces, or 0 if the store is unavailable."""
    try:
        debug_settings = runtime.storage.load_debug_settings()
        store = DebugTraceStore(
            data_dir=runtime.storage.data_dir,
            trace_limit=debug_settings.get("trace_limit", 50),
        )
        return len(store.get_traces())
    except (FileNotFoundError, OSError):
        # Expected when the trace store has never been written; not an error.
        return 0
    except Exception:
        _LOGGER.warning("Failed to read debug trace count; reporting 0", exc_info=True)
        return 0


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
    item: JsonObject = {
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
        "editable": bool(getattr(provider, "custom", False)),
    }
    if getattr(provider, "custom", False):
        item["custom"] = True
        item["adapter"] = provider.adapter
    if provider.id == "openrouter":
        item["routing"] = runtime.storage.load_openrouter_routing_settings()
    return item


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return settings and task-model RPC handlers."""

    return {
        "settings.get_raw": _get_settings_raw,
        "settings.get": _get_settings,
        "settings.values": _get_public_settings,
        "settings.catalog": _settings_catalog,
        "settings.get_path": _get_setting_path,
        "settings.patch": _patch_setting_paths,
        "settings.update": _update_settings,
        "task_model.settings": _task_model_settings,
        "task_model.update": _task_model_update,
        "task_model.list_targets": _task_model_list_targets,
        "task_model.status": _task_model_status,
        "task_model.options": _task_model_options,
        "task_model.patch_options": _task_model_patch_options,
    }
