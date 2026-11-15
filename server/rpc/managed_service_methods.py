"""Operator surface for installation-owned optional services."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cli.application._service_prerequisites import ActionRequiredError
from cli.application.services import ManagedService, search_restore_operations
from cli.application.state import ApplicationError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool
from server.rpc._mutations import require_mutation_available, state_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string
from server.update_availability import owning_installation

JsonObject = dict[str, Any]
_WORKERS = BoundedWorkerPool(name="managed-service", max_workers=2)
_LOGGER = get_logger("server.rpc.managed_services")


class ServiceOperations:
    def __init__(self, state: Any) -> None:
        self.state = state
        installation = owning_installation(Path(state.runtime.storage.data_dir))
        self.service = ManagedService(installation) if installation else None
        self.task: asyncio.Task[None] | None = None

    async def status(self) -> JsonObject:
        if self.service is None:
            return {
                "id": "searxng",
                "name": "SearXNG",
                "available": False,
                "installed": False,
                "state": "idle",
                "phase": "",
                "error": None,
                "message": (
                    "Managed services require an installed vBot server. An existing "
                    "SearXNG URL can be configured in Settings."
                ),
                "url": None,
                "runtime": None,
                "plan": None,
                "actions": [],
            }
        return await _WORKERS.run(
            self.service.status, active=self.task is not None and not self.task.done()
        )

    async def start(
        self, action: str, plan_id: str | None, allow_prerequisites: bool
    ) -> JsonObject:
        require_mutation_available(self.state, "_settings_mutation_lock")
        service = self.service
        if service is None:
            raise ApplicationError("Managed services require an installed vBot server")
        if self.task is not None and not self.task.done():
            raise ApplicationError(
                "A managed service operation is already running; inspect its status"
            )
        await _WORKERS.run(
            service.admit, action, plan_id=plan_id, allow_prerequisites=allow_prerequisites
        )
        if action in {"install", "update"}:
            async with state_lock(self.state, "_settings_mutation_lock"):
                raw = await _WORKERS.run(self.state.runtime.storage.load_settings)
                record = await _WORKERS.run(service.read)
                search = _search_selection(raw)
                fields: JsonObject = {"search_expected": search}
                owned = "port" in record and search == {
                    "provider": "searxng",
                    "base_url": service.url(record),
                }
                if not owned or "search_before" not in record:
                    fields["search_before"] = search
                await _WORKERS.run(service.save, record, **fields)
        self.task = asyncio.create_task(
            self._execute(action, allow_prerequisites), name="managed-service:searxng"
        )
        return await self.status()

    async def _execute(self, action: str, allow_prerequisites: bool) -> None:
        service = self.service
        assert service is not None
        try:
            url = await _WORKERS.run(
                service.execute, action, allow_prerequisites=allow_prerequisites
            )
            if action in {"install", "update"} and url is not None:
                await self._select_search(url)
            elif action == "remove":
                await self._restore_search()
            await _WORKERS.run(service.finish)
        except ActionRequiredError as exc:
            record = await _WORKERS.run(service.read)
            await _WORKERS.run(
                service.save, record, state="action_required", error=None, message=str(exc)
            )
        except asyncio.CancelledError:
            # Worker cancellation settles admitted work; keep its durable phase for resume.
            raise
        except Exception:
            _LOGGER.exception(
                "Managed service coordination failed (service=searxng action=%s)", action
            )
            await _WORKERS.run(
                service.finish,
                error="The service operation could not finish. Inspect the logs, then resume.",
            )

    async def _select_search(self, url: str) -> None:
        service = self.service
        assert service is not None
        # Coordinate with every other Settings writer through the same lock/transaction.
        async with state_lock(self.state, "_settings_mutation_lock"):
            raw = await _WORKERS.run(self.state.runtime.storage.load_settings)
            record = await _WORKERS.run(service.read)
            if _search_selection(raw) != record.get("search_expected"):
                raise ActionRequiredError(
                    "SearXNG is ready, but web search settings changed during installation. "
                    "They were kept. Resume installation to select SearXNG, or keep your selection."
                )
            # The handler uses this lock and retains normal validation and live effects.
            from server.rpc.settings_methods import _patch_setting_paths

            await _patch_setting_paths(
                self.state,
                {
                    "operations": [
                        {"op": "set", "path": "web_search.provider", "value": "searxng"},
                        {"op": "set", "path": "web_search.searxng.base_url", "value": url},
                    ]
                },
            )
            record.pop("search_expected", None)
            await _WORKERS.run(service.save, record)

    async def _restore_search(self) -> None:
        service = self.service
        assert service is not None
        async with state_lock(self.state, "_settings_mutation_lock"):
            raw = await _WORKERS.run(self.state.runtime.storage.load_settings)
            record = await _WORKERS.run(service.read)
            operations = search_restore_operations(record, raw)
            if not operations:
                return
            from server.rpc.settings_methods import _patch_setting_paths

            await _patch_setting_paths(self.state, {"operations": operations})
            record.pop("search_before", None)
            await _WORKERS.run(service.save, record)

    async def close(self) -> None:
        if self.task is not None:
            # Do not abandon an admitted package install or leave a replacement halfway.
            await self.task

    async def resume_start(self) -> None:
        service = self.service
        assert service is not None
        try:
            await _WORKERS.run(service.admit, "start", plan_id=None, allow_prerequisites=False)
            await self._execute("start", False)
        except ApplicationError, OSError:
            _LOGGER.warning("Managed service startup was refused (service=searxng)", exc_info=True)


def _operations(state: Any) -> ServiceOperations:
    service = getattr(state, "_managed_service_operations", None)
    if service is None:
        service = ServiceOperations(state)
        state._managed_service_operations = service
    return service


def _search_selection(raw: JsonObject) -> JsonObject:
    search = raw.get("web_search", {})
    return {
        "provider": search.get("provider"),
        "base_url": search.get("searxng", {}).get("base_url"),
    }


def _service_id(params: JsonObject) -> None:
    if _required_string(params, "id") != "searxng":
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Unknown managed service; available: searxng")


async def _get(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "managed_service.get")
    _service_id(params)
    try:
        return await _operations(state).status()
    except (ApplicationError, OSError) as exc:
        raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc


async def _apply(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"id", "action", "plan_id", "allow_prerequisites"}, "managed_service.apply"
    )
    _service_id(params)
    action = _required_string(params, "action")
    plan_id = params.get("plan_id")
    allow = params.get("allow_prerequisites", False)
    if (plan_id is not None and not isinstance(plan_id, str)) or not isinstance(allow, bool):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "plan_id must be a string and allow_prerequisites a boolean"
        )
    # Check before taking the operation lock: another admitted operation may be
    # waiting for Settings while its publisher awaits this lifecycle callback.
    require_mutation_available(state, "_settings_mutation_lock")
    async with state_lock(state, "_managed_service_lock"):
        try:
            return await _operations(state).start(action, plan_id, allow)
        except (ApplicationError, OSError) as exc:
            raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc


async def close_services(state: Any) -> None:
    operations = getattr(state, "_managed_service_operations", None)
    if operations is not None:
        await operations.close()


async def start_services(state: Any) -> None:
    """Restart only a previously installed service the operator left enabled."""
    operations = _operations(state)
    service = operations.service
    if service is None:
        return
    try:
        record = await _WORKERS.run(service.read)
        if (
            record.get("installed")
            and record.get("desired_running")
            and record.get("state") != "running"
        ):
            operations.task = asyncio.create_task(
                operations.resume_start(), name="managed-service:searxng"
            )
    except ApplicationError, OSError:
        _LOGGER.warning(
            "Managed service startup could not read its record (service=searxng)", exc_info=True
        )


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {"managed_service.get": _get, "managed_service.apply": _apply}
