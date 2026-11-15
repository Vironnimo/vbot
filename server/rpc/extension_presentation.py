"""Registration-scoped UI contributions and declared product services."""

from __future__ import annotations

import copy
from typing import Any

from core.extensions import API_VERSION, ExtensionRegistrationIdentity
from core.extensions.contributions import EXTENSION_FEATURES
from core.extensions.operations import ExtensionOperationError
from core.extensions.services import ExtensionServiceError
from server.file_delivery import FILE_PREVIEW_WORKERS
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string


async def _contributions(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    from server.rpc.extensions_methods import _page_projection

    _reject_unsupported(params, set(), "extensions.contributions")
    result: dict[str, Any] = {
        "api_version": API_VERSION,
        "features": sorted(EXTENSION_FEATURES),
        "contributions": [],
    }
    registry = state.runtime.extensions
    if registry is None:
        return result
    try:
        pages = await FILE_PREVIEW_WORKERS.run(_page_projection, registry, state.file_delivery)
        if state.runtime.extensions is not registry:
            raise ValueError("Extensions changed; refresh their catalog")
        page_map = {(page["extension"], page["page"]): page for page in pages}
        for identity, declaration in registry.contribution_declarations():
            spec = copy.deepcopy(declaration.spec)
            if spec["kind"] == "view":
                page = page_map.get((identity.name, spec["page"]))
                if page is None:
                    continue
                spec["page"] = page
            result["contributions"].append(
                {
                    **spec,
                    "id": f"extension:{identity.name}:{declaration.id}",
                    "local_id": declaration.id,
                    "extension": identity.name,
                    "epoch": identity.epoch,
                }
            )
        return result
    except (ValueError, OSError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _contribution_operation(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(
        params,
        {"extension", "id", "epoch", "operation", "params"},
        "extensions.contribution_operation",
    )
    name = _required_string(params, "extension")
    contribution_id = _required_string(params, "id")
    identity = ExtensionRegistrationIdentity(name, _required_string(params, "epoch"))
    operation = _required_string(params, "operation")
    arguments = params.get("params", {})
    if not isinstance(arguments, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Operation params must be an object")
    registry = state.runtime.extensions

    def check() -> None:
        declaration = registry.current_contribution(identity, contribution_id) if registry else None
        if state.runtime.extensions is not registry or declaration is None:
            raise ValueError("Extension contribution is unavailable; refresh its catalog")
        if operation not in declaration.spec["operations"]:
            raise ValueError("Operation is not declared by this contribution")

    try:
        check()
        result = dict(await registry.management(name).invoke(operation, arguments))
        check()
        return result
    except ExtensionOperationError as exc:
        raise RpcError(exc.code, str(exc)) from exc
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _page_service(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(
        params, {"extension", "page", "epoch", "kind", "name", "params"}, "extensions.page_service"
    )
    extension = _required_string(params, "extension")
    page_id = _required_string(params, "page")
    identity = ExtensionRegistrationIdentity(extension, _required_string(params, "epoch"))
    kind = _required_string(params, "kind")
    if kind not in {"describe", "query", "execute"}:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Service kind must be describe, query or execute")
    arguments = params.get("params", {})
    if not isinstance(arguments, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Service params must be an object")
    registry = state.runtime.extensions

    def page() -> Any:
        found = registry.current_page(identity, page_id) if registry else None
        if state.runtime.extensions is not registry or found is None:
            raise ValueError("Extension page is unavailable; refresh the page")
        return found[0]

    try:
        declaration = page()
        services = registry.host_for(identity).services
        if services is None:
            raise ValueError("Product services are unavailable")
        if kind == "describe":
            description = services.describe()
            entries = [
                entry for entry in description["services"] if entry["name"] in declaration.services
            ]
            return {
                **description,
                "services": entries,
                "resources": sorted(
                    {resource for entry in entries for resource in entry.get("resources", [])}
                ),
            }
        name = _required_string(params, "name")
        if name not in declaration.services:
            raise ValueError("Service is not declared by this page")
        operation = services.query if kind == "query" else services.execute
        result = await operation(name, arguments)
        page()
        return dict(result)
    except RpcError:
        raise
    except ExtensionServiceError as exc:
        raise RpcError(exc.code, str(exc), data=exc.data) from exc
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def presentation_handlers() -> dict[str, RpcMethodHandler]:
    return {
        "extensions.contributions": _contributions,
        "extensions.contribution_operation": _contribution_operation,
        "extensions.page_service": _page_service,
    }
