"""Registration-scoped UI contributions and declared product services."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from core.extensions import API_VERSION, ExtensionRegistrationIdentity
from core.extensions._callbacks import ExtensionHandlerTimeoutError, invoke_isolated_handler
from core.extensions._declarations import ExtensionUnavailableError
from core.extensions.contributions import EXTENSION_FEATURES, ContributionDeclaration
from core.extensions.operations import ExtensionOperationError
from core.extensions.services import ExtensionServiceError
from server.file_delivery import FILE_PREVIEW_WORKERS
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string

_LOGGER = logging.getLogger("vbot.server.rpc.extension_presentation")
_SOURCE_TIMEOUT_SECONDS = 5.0


@dataclass
class _SourceOwnerAdmission:
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    users: int = 0


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
    return await _invoke_contribution(state, params, source_only=False)


async def _contribution_sources(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Read a bounded batch through the same registration-scoped source owner.

    All batches on this application share admission, so multiple clients cannot
    multiply the read fan-out. Per-entry failures preserve successful siblings.
    """
    _reject_unsupported(params, {"requests"}, "extensions.contribution_sources")
    requests = params.get("requests")
    if not isinstance(requests, list) or not 1 <= len(requests) <= 32:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Source requests must contain 1 to 32 entries")
    slots = getattr(state, "extension_contribution_source_slots", None)
    if slots is None:
        slots = state.extension_contribution_source_slots = asyncio.Semaphore(4)
        state.extension_contribution_source_owners = {}
    owners: dict[str, _SourceOwnerAdmission] = state.extension_contribution_source_owners

    async def read(request: Any) -> dict[str, Any]:
        owner = None
        name = ""
        try:
            if not isinstance(request, dict):
                raise RpcError(RPC_ERROR_INVALID_REQUEST, "Source request must be an object")
            name = _required_string(request, "extension")
            owner = owners.setdefault(name, _SourceOwnerAdmission())
            owner.users += 1
            # Wait for this owner's turn before global admission, reserving
            # capacity for other Extensions even when one has many slow rows.
            async with owner.slots, slots:
                result = await invoke_isolated_handler(
                    _invoke_contribution,
                    state,
                    request,
                    source_only=True,
                    extension_name=name,
                    description="contribution source read",
                    timeout_seconds=_SOURCE_TIMEOUT_SECONDS,
                )
                # A list must not amplify a large single-operation result into
                # an unbounded batch response. 32 entries total at most 8 MiB.
                if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > 262_144:
                    raise RpcError(
                        RPC_ERROR_INVALID_REQUEST, "Contribution source result exceeds 256 KiB"
                    )
            return {"ok": True, "result": result}
        except RpcError as exc:
            return {"ok": False, "error": exc.to_dict()}
        except ExtensionHandlerTimeoutError:
            return {
                "ok": False,
                "error": {"code": "callback_timeout", "message": "Extension source read timed out"},
            }
        except ExtensionUnavailableError:
            return {
                "ok": False,
                "error": {
                    "code": "callback_unavailable",
                    "message": "An earlier Extension callback is still running",
                },
            }
        except Exception:
            _LOGGER.exception("Unexpected Extension source read failure")
            return {
                "ok": False,
                "error": {"code": "internal_error", "message": "Extension source read failed"},
            }
        finally:
            if owner is not None:
                owner.users -= 1
                if not owner.users:
                    owners.pop(name)

    return {"results": await asyncio.gather(*(read(request) for request in requests))}


async def _invoke_contribution(
    state: Any, params: dict[str, Any], *, source_only: bool
) -> dict[str, Any]:
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

    def check() -> ContributionDeclaration:
        declaration: ContributionDeclaration | None = (
            registry.current_contribution(identity, contribution_id) if registry else None
        )
        if state.runtime.extensions is not registry or declaration is None:
            raise ValueError("Extension contribution is unavailable; refresh its catalog")
        if operation not in declaration.spec["operations"]:
            raise ValueError("Operation is not declared by this contribution")
        if source_only and declaration.spec.get("source", {}).get("operation") != operation:
            raise ValueError("Batch reads require the declared source operation")
        return declaration

    try:
        declaration = check()
        result = dict(await registry.management(name).invoke(operation, arguments))
        # Sources are read projections. Other operations are admitted actions:
        # their definitive result survives a reload or retirement they trigger.
        if declaration.spec.get("source", {}).get("operation") == operation:
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
            resources = {resource for entry in entries for resource in entry.get("resources", [])}
            return {
                **description,
                "services": entries,
                "resources": sorted(resources),
                "resource_definitions": [
                    entry
                    for entry in description.get("resource_definitions", [])
                    if entry["name"] in resources
                ],
            }
        name = _required_string(params, "name")
        if name not in declaration.services:
            raise ValueError("Service is not declared by this page")
        operation = services.query if kind == "query" else services.execute
        result = await operation(name, arguments)
        # An admitted execute can reload its own page. Its committed result is
        # definitive; only reads must still belong to the original registration.
        if kind == "query":
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
        "extensions.contribution_sources": _contribution_sources,
        "extensions.page_service": _page_service,
    }
