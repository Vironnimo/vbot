"""Extension visibility RPC handlers."""

from __future__ import annotations

from typing import Any

from core.extensions import ExtensionRecord
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]


def _list_extensions(state: Any, params: JsonObject) -> JsonObject:
    """Return every discovered extension record plus its persisted config.

    Records come from the runtime's :class:`ExtensionRegistry` (in load order);
    the persisted ``settings.extensions.config`` for each name is merged in so the
    management surface can render and edit raw per-extension config. When no
    extensions loaded (no registry), the list is empty.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "extensions.list does not accept params")
    try:
        registry = state.runtime.extensions
        config_map = _persisted_extension_config(state)
        records = registry.records() if registry is not None else []
        return {"extensions": [_extension_response(record, config_map) for record in records]}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _persisted_extension_config(state: Any) -> dict[str, dict[str, Any]]:
    """Read ``settings.extensions.config`` so loaded/disabled records can echo it."""
    extensions_settings = state.runtime.storage.load_extensions_settings()
    config = extensions_settings.get("config", {})
    return config if isinstance(config, dict) else {}


def _extension_response(
    record: ExtensionRecord,
    config_map: dict[str, dict[str, Any]],
) -> JsonObject:
    manifest = record.manifest
    return {
        "name": record.name,
        "status": record.status,
        "disabled": record.status == "disabled",
        "root": str(record.root_path),
        "entry": str(record.entry_path),
        "error": record.error,
        "capability_errors": list(record.capability_errors),
        "version": manifest.version if manifest is not None else None,
        "description": manifest.description if manifest is not None else None,
        "display_name": manifest.display_name if manifest is not None else None,
        "api_version": manifest.api_version if manifest is not None else None,
        "config": config_map.get(record.name, {}),
        "capabilities": _extension_capabilities(record),
    }


def _extension_capabilities(record: ExtensionRecord) -> JsonObject:
    """Summarize what a loaded extension contributed (empty for failed/disabled)."""
    declarations = record.declarations
    return {
        "hooks": {
            event: len(handlers) for event, handlers in declarations.hooks.items() if handlers
        },
        "tools": [declaration.name for declaration in declarations.tools],
        "recall_backends": [declaration.name for declaration in declarations.recall_backends],
        "startup": bool(declarations.startup),
        "shutdown": bool(declarations.shutdown),
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return extension visibility RPC handlers."""

    return {
        "extensions.list": _list_extensions,
    }
