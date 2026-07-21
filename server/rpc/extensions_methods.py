"""Extension visibility and secret-field RPC handlers."""

from __future__ import annotations

from typing import Any

from core.extensions import ExtensionRecord, SettingsFieldDeclaration
from core.utils.logging import get_logger
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]
_LOGGER = get_logger("server.rpc.extensions")


def _list_extensions(state: Any, params: JsonObject) -> JsonObject:
    """Return every discovered extension record plus its persisted config.

    Records come from the runtime's :class:`ExtensionRegistry` (in load order);
    the persisted ``settings.extensions.config`` for each name is merged in so the
    management surface can render and edit per-extension config, and a loaded
    extension's declared settings schema (with live secret state) is surfaced so
    the WebUI can render a real form. When no extensions loaded (no registry),
    the list is empty.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "extensions.list does not accept params")
    try:
        return _extensions_payload(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _reload_extensions(state: Any, params: JsonObject) -> JsonObject:
    """Rebuild the whole extension layer live, then return the ``extensions.list`` shape.

    The explicit reload trigger: it drives ``Runtime.reload_extensions`` (a full,
    restart-equivalent rebuild from disk under the runtime's serialization lock),
    then returns the freshly rebuilt catalog in the same shape as
    :func:`_list_extensions`, so the caller sees the new state without a second
    round-trip. Rejects params like ``extensions.list``.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "extensions.reload does not accept params")
    try:
        await state.runtime.reload_extensions()
        return _extensions_payload(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _extensions_payload(state: Any) -> JsonObject:
    """Build the shared ``extensions.list`` / ``extensions.reload`` catalog payload."""
    registry = state.runtime.extensions
    config_map = _persisted_extension_config(state)
    records = registry.records() if registry is not None else []
    return {"extensions": [_extension_response(record, config_map, state) for record in records]}


def _persisted_extension_config(state: Any) -> dict[str, dict[str, Any]]:
    """Read ``settings.extensions.config`` so loaded/disabled records can echo it."""
    extensions_settings = state.runtime.storage.load_extensions_settings()
    config = extensions_settings.get("config", {})
    return config if isinstance(config, dict) else {}


def _extension_response(
    record: ExtensionRecord,
    config_map: dict[str, dict[str, Any]],
    state: Any,
) -> JsonObject:
    manifest = record.manifest
    return {
        "name": record.name,
        "status": record.status,
        "disabled": record.status == "disabled",
        "root": str(record.root_path),
        "entry": str(record.entry_path),
        "error": record.error,
        "overridden_by": record.overridden_by,
        "capability_errors": list(record.capability_errors),
        "version": manifest.version if manifest is not None else None,
        "description": manifest.description if manifest is not None else None,
        "display_name": manifest.display_name if manifest is not None else None,
        "api_version": manifest.api_version if manifest is not None else None,
        "config": config_map.get(record.name, {}),
        "settings_schema": _settings_schema_response(record, state),
        "capabilities": _extension_capabilities(record, state),
        "ready_state": _extension_ready_state(record, state),
    }


def _settings_schema_response(record: ExtensionRecord, state: Any) -> list[JsonObject] | None:
    """Serialize a loaded extension's declared settings schema, or ``None``.

    ``None`` when the record is not ``loaded`` or declared no schema. Each field
    is ``{key, type, label, description, required, default}``; a secret field
    additionally carries its declared ``env_key`` and a live ``set`` bool (never
    the secret value). Never includes a secret's value anywhere.
    """
    if record.status != "loaded":
        return None
    schema = record.declarations.settings_schema
    if not schema:
        return None
    return [_settings_field_response(field, state) for field in schema]


def _settings_field_response(field: SettingsFieldDeclaration, state: Any) -> JsonObject:
    response: JsonObject = {
        "key": field.key,
        "type": field.type,
        "label": field.label,
        "description": field.description,
        "required": field.required,
        "default": field.default,
    }
    if field.type == "secret":
        env_key = field.env_key or ""
        response["env_key"] = env_key
        response["set"] = _credential_is_set(state, env_key)
    return response


def _credential_is_set(state: Any, env_key: str) -> bool:
    """Resolve a secret's live set/unset state without exposing its value."""
    if not env_key:
        return False
    resolved: str = state.runtime.resolve_environment_credential(env_key)
    return resolved.strip() != ""


def _extension_capabilities(record: ExtensionRecord, state: Any) -> JsonObject:
    """Summarize what a loaded extension contributed (empty for failed/disabled).

    Each declared tool becomes ``{"name", "ready"}``: the declared name is looked
    up live in the runtime ``ToolRegistry`` and its readiness re-evaluated. A name
    that never registered (e.g. skipped on a collision) reports ``ready: false`` —
    it is not offered anywhere, which is exactly what an unready tool means here.
    """
    declarations = record.declarations
    return {
        "hooks": {
            event: len(handlers) for event, handlers in declarations.hooks.items() if handlers
        },
        "tools": [
            {"name": declaration.name, "ready": _tool_is_ready(state, declaration.name)}
            for declaration in declarations.tools
        ],
        "recall_backends": [declaration.name for declaration in declarations.recall_backends],
        "interaction_handlers": [
            declaration.prefix for declaration in declarations.interaction_handlers
        ],
        "startup": bool(declarations.startup),
        "shutdown": bool(declarations.shutdown),
    }


def _tool_is_ready(state: Any, tool_name: str) -> bool:
    """Re-evaluate a declared tool's live readiness through the runtime registry.

    An unregistered name (skipped on a collision, or no registry wired) is not
    ready — it is offered nowhere.
    """
    from core.tools import tool_is_ready as tool_readiness

    registry = getattr(state.runtime, "tools", None)
    if registry is None:
        return False
    try:
        tool = registry.get(tool_name)
    except Exception:
        return False
    return tool_readiness(tool)


def _extension_ready_state(record: ExtensionRecord, state: Any) -> str:
    """Return the derived, display-only extension readiness state.

    ``"waiting"`` when the record is ``loaded``, declares at least one tool, and
    at least one declared tool is not ready (e.g. its credential is unset);
    ``"ready"`` otherwise (including a loaded record with no tools). Not a stored
    state — purely computed from per-tool readiness for the Extensions tab.
    """
    if record.status != "loaded":
        return "ready"
    tools = record.declarations.tools
    if not tools:
        return "ready"
    if any(not _tool_is_ready(state, declaration.name) for declaration in tools):
        return "waiting"
    return "ready"


def _set_extension_secret(state: Any, params: JsonObject) -> JsonObject:
    """Set or clear a schema'd extension's secret in the data-dir ``.env``.

    ``key`` is the **schema field key**, never the env key — the server looks up
    the declared ``env_key`` so a client can never choose where a secret lands.
    An empty ``value`` clears the credential; a non-empty value sets it. Either
    way, provider credentials are reloaded so live resolution sees the change
    immediately. The secret value is never logged.
    """
    _reject_unsupported(params, {"name", "key", "value"}, "extensions.set_secret")

    name = _required_str(params, "name")
    key = _required_str(params, "key")
    value = params.get("value", "")
    if not isinstance(value, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "extensions.set_secret value must be a string")

    field = _resolve_secret_field(state, name, key)
    env_key = field.env_key or ""

    try:
        runtime = state.runtime
        previous_value = runtime.storage.load_environment().get(env_key)
        if value == "":
            changed = runtime.storage.remove_data_dir_credential(env_key)
            new_state = False
        else:
            runtime.storage.set_data_dir_credential(env_key, value)
            changed = previous_value != value
            new_state = True
        runtime.reload_provider_credentials()
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if changed:
        _LOGGER.info(
            "Extension secret %s (extension=%s field=%s)",
            "saved" if new_state else "removed",
            name,
            key,
        )
    return {"name": name, "key": key, "set": new_state}


def _resolve_secret_field(state: Any, name: str, key: str) -> SettingsFieldDeclaration:
    """Find the loaded extension's declared secret field for *key*, or fail."""
    registry = state.runtime.extensions
    record = None
    if registry is not None:
        record = next((item for item in registry.records() if item.name == name), None)
    if record is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"unknown extension: {name!r}")
    if record.status != "loaded":
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"extension {name!r} is not loaded")
    schema = record.declarations.settings_schema
    if not schema:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"extension {name!r} declares no settings schema")
    field: SettingsFieldDeclaration | None = next(
        (item for item in schema if item.key == key), None
    )
    if field is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"unknown settings field {key!r} for {name!r}")
    if field.type != "secret":
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"settings field {key!r} is not a secret")
    return field


def _required_str(params: JsonObject, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, f"extensions.set_secret requires a '{key}' string"
        )
    return value


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return extension visibility and secret RPC handlers."""

    return {
        "extensions.list": _list_extensions,
        "extensions.reload": _reload_extensions,
        "extensions.set_secret": _set_extension_secret,
    }
