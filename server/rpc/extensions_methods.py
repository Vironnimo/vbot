"""Extension visibility and secret-field RPC handlers."""

from __future__ import annotations

import re
from typing import Any

from core.chat import latest_session_context_usage
from core.extensions import ExtensionRecord, ExtensionRegistrationIdentity, SettingsFieldDeclaration
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_COMMANDS, RESOURCE_KIND_EXTENSIONS
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.operations_methods import FILE_PREVIEW_WORKERS
from server.rpc.payloads import remove_opaque_provider_metadata
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]
_LOGGER = get_logger("server.rpc.extensions")
_HISTORY_WORKERS = BoundedWorkerPool(name="extension-history", max_workers=2)
_FILE_URL_PATTERN = re.compile(r"/api/files/[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


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
        payload = _extensions_payload(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_COMMANDS)
    publish_resource_changed(state, RESOURCE_KIND_EXTENSIONS)
    return payload


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
    tool_names = [
        declaration.name
        for declaration in declarations.tools
        if getattr(declaration, "catalog_visible", True)
    ]
    if declarations.operations is not None:
        tool_names.extend(declarations.operations.catalog_visible_tool_names)
    return {
        "hooks": {
            event: len(handlers) for event, handlers in declarations.hooks.items() if handlers
        },
        "tools": [{"name": name, "ready": _tool_is_ready(state, name)} for name in tool_names],
        "commands": [
            {
                "name": declaration.name,
                "registered": _command_is_registered(
                    state,
                    record.name,
                    declaration.name,
                ),
            }
            for declaration in declarations.commands
        ],
        "recall_backends": [declaration.name for declaration in declarations.recall_backends],
        "interaction_handlers": [
            declaration.prefix for declaration in declarations.interaction_handlers
        ],
        "startup": bool(declarations.startup),
        "shutdown": bool(declarations.shutdown),
    }


def _command_is_registered(
    state: Any,
    extension_name: str,
    command_name: str,
) -> bool:
    dispatcher = getattr(state, "command_dispatcher", None)
    if dispatcher is None:
        dispatcher = getattr(state.runtime, "command_dispatcher", None)
    owner = getattr(dispatcher, "extension_command_owner", None)
    return callable(owner) and owner(command_name) == extension_name


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
        runtime.reload_environment_credentials()
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


async def _extension_operation(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"name", "operation", "arguments", "page"}, "extensions.operation")
    name = _required_str(params, "name")
    operation = _required_str(params, "operation")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Operation arguments must be an object")
    registry = state.runtime.extensions
    if registry is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Extensions are unavailable")
    try:
        page = params.get("page")
        if page is not None:
            await _validate_page_context(state.runtime, registry, name, page)
        management = registry.management(name)
        if operation == "describe":
            return {"operations": management.describe()}
        result = dict(await management.invoke(operation, arguments))
        if page is not None:
            await _validate_page_context(state.runtime, registry, name, page)
        return result
    except ValueError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error


async def _extension_pages(state: Any, params: JsonObject) -> JsonObject:
    """Project current Extension pages without disclosing local asset paths."""
    _reject_unsupported(params, set(), "extensions.pages")
    registry = state.runtime.extensions
    if registry is None:
        return {"pages": []}
    try:
        pages = await FILE_PREVIEW_WORKERS.run(_page_projection, registry, state.file_delivery)
        return {"pages": pages}
    except (OSError, ValueError) as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error


async def _extension_page_run(state: Any, params: JsonObject) -> JsonObject:
    """Open a parent-owned SSE capability for one live, owner-scoped Run."""
    _reject_unsupported(
        params,
        {"name", "page", "group_id", "run_id", "after_sequence"},
        "extensions.page_run",
    )
    name = _required_str(params, "name")
    group_id = _required_str(params, "group_id")
    run_id = _required_str(params, "run_id")
    after_sequence = params.get("after_sequence", 0)
    if (
        not isinstance(after_sequence, int)
        or isinstance(after_sequence, bool)
        or after_sequence < 0
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "extensions.page_run after_sequence must be a non-negative integer",
        )
    registry = state.runtime.extensions
    if registry is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Extension page is unavailable; refresh the page")
    try:
        await _validate_page_context(state.runtime, registry, name, params.get("page"))
        page = params["page"]
        identity = ExtensionRegistrationIdentity(name, page["epoch"])
        host = registry.host_for(identity)
        temporary_agents = host.temporary_agents
        if temporary_agents is None:
            raise ValueError("Extension page is unavailable; refresh the page")
        inspection = await temporary_agents.owned_run(group_id, run_id)
        if inspection.run is None:
            return {"stream": None}
        if state.runtime.extensions is not registry or not registry.is_registration_current(
            identity
        ):
            raise ValueError("Extension page is unavailable; refresh the page")
        await _validate_page_context(state.runtime, registry, name, page)
        verified_host = registry.host_for(identity)
        verified_temporary_agents = verified_host.temporary_agents
        if verified_temporary_agents is None:
            raise ValueError("Extension page is unavailable; refresh the page")
        verified = await verified_temporary_agents.owned_run(group_id, run_id)
        if verified.run is None:
            return {"stream": None}
        return {
            "stream": state.file_delivery.open_extension_run(
                extension=name,
                page=page["id"],
                epoch=page["epoch"],
                group_id=group_id,
                run_id=run_id,
                after_sequence=after_sequence,
            )
        }
    except ValueError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error


async def _extension_page_history(state: Any, params: JsonObject) -> JsonObject:
    """Project one owner-bound temporary Session history for a registered page."""
    _reject_unsupported(
        params,
        {"name", "page", "group_id", "participant_id", "query"},
        "extensions.page_history",
    )
    name = _required_str(params, "name")
    group_id = _required_str(params, "group_id")
    participant_id = _required_str(params, "participant_id")
    query = params.get("query", {})
    if not isinstance(query, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "extensions.page_history query must be an object")
    registry = state.runtime.extensions
    if registry is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Extension page is unavailable; refresh the page")
    try:
        await _validate_page_context(state.runtime, registry, name, params.get("page"))
        page = params["page"]
        identity = ExtensionRegistrationIdentity(name, page["epoch"])
        host = registry.host_for(identity)
        temporary_agents = host.temporary_agents
        if temporary_agents is None:
            raise ValueError("Extension page is unavailable; refresh the page")
        snapshot = await temporary_agents.inspect(group_id, participant_id, query)
        projection = await _HISTORY_WORKERS.run(
            _temporary_history_projection, snapshot, state.file_delivery
        )
        if state.runtime.extensions is not registry or not registry.is_registration_current(
            identity
        ):
            raise ValueError("Extension page is unavailable; refresh the page")
        await _validate_page_context(state.runtime, registry, name, page)
        return projection
    except ValueError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error


def _temporary_history_projection(snapshot: Any, delivery: Any) -> JsonObject:
    """Use the ordinary client projection while withholding internal Session records."""
    messages = [
        remove_opaque_provider_metadata(message.to_dict(), file_delivery=delivery)
        for message in snapshot.page.messages
        if getattr(message, "role", None) not in {"note", "history_edit"}
    ]
    response: JsonObject = {
        "messages": messages,
        "has_more": snapshot.page.has_more,
        "session_usage": snapshot.session_usage,
        "context_usage": latest_session_context_usage(list(snapshot.context_messages)),
        "file_urls": _projected_file_urls(messages, delivery),
    }
    if snapshot.page.before_cursor is not None:
        response["next_before"] = snapshot.page.before_cursor
    return response


def _projected_file_urls(value: Any, delivery: Any) -> list[str]:
    """List only file capabilities that survived the ordinary visible projection."""
    if delivery is None:
        return []
    urls: list[str] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            for url in _FILE_URL_PATTERN.findall(item):
                token = url.removeprefix("/api/files/")
                if url not in seen and delivery.resolve_token(token) is not None:
                    seen.add(url)
                    urls.append(url)

    visit(value)
    return urls


async def _validate_page_context(runtime: Any, registry: Any, name: str, page: Any) -> None:
    if not isinstance(page, dict):
        raise ValueError("page must be an object")
    page_id = page.get("id")
    epoch = page.get("epoch")
    if not isinstance(page_id, str) or not isinstance(epoch, str):
        raise ValueError("page requires id and epoch strings")
    identity = ExtensionRegistrationIdentity(name, epoch)
    if runtime.extensions is not registry or not registry.is_registration_current(identity):
        raise ValueError("Extension page is unavailable; refresh the page")
    declarations = await FILE_PREVIEW_WORKERS.run(registry.page_declarations)
    if not registry.is_registration_current(identity):
        raise ValueError("Extension page is unavailable; refresh the page")
    if not any(
        candidate == identity and declaration.page_id == page_id
        for candidate, declaration, _entry in declarations
    ):
        raise ValueError("Extension page is unavailable; refresh the page")


def _page_projection(registry: Any, delivery: Any) -> list[JsonObject]:
    """Read local page assets and mint capability URLs off the Event Loop."""
    pages = []
    for identity, declaration, entry in registry.page_declarations():
        page = delivery.open_extension_page(
            extension=identity.name,
            page=declaration.page_id,
            epoch=identity.epoch,
            entry=entry,
        )
        pages.append(
            {
                "extension": identity.name,
                "page": declaration.page_id,
                "title": declaration.title,
                "icon": declaration.icon,
                "route": f"extension:{identity.name}:{declaration.page_id}",
                "entry_url": page["url"],
                "epoch": identity.epoch,
            }
        )
    return pages


def _extension_requests(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "extensions.requests")
    registry = state.runtime.extensions
    requests = []
    if registry is not None:
        for record in registry.records():
            operations = record.declarations.operations
            if record.status != "loaded" or operations is None or operations.pending_inputs is None:
                continue
            for request in operations.pending_inputs():
                requests.append(
                    {
                        **request,
                        "extension": record.name,
                        "response_operation": operations.input_response_operation,
                    }
                )
    return {"requests": requests}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return extension visibility and secret RPC handlers."""

    return {
        "extensions.list": _list_extensions,
        "extensions.operation": _extension_operation,
        "extensions.page_history": _extension_page_history,
        "extensions.page_run": _extension_page_run,
        "extensions.pages": _extension_pages,
        "extensions.requests": _extension_requests,
        "extensions.reload": _reload_extensions,
        "extensions.set_secret": _set_extension_secret,
    }
