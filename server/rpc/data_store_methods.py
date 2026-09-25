"""RPC projections for the data directory's canonical databases.

Health, snapshots, incidents, and the operator's release of a removed
Extension's database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.database import (
    DatabaseUnavailableError,
    acknowledge_incident,
    create_data_snapshot,
    data_store_status,
)
from core.database.snapshots import read_snapshot_health, read_snapshot_summary
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_DATA_STORE
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]
_DATA_STORE_WORKERS = BoundedWorkerPool(name="data-store", max_workers=2)
_SNAPSHOT_REASONS = frozenset({"manual", "rpc", "update", "recovery"})


def _runtime(state: Any) -> tuple[Any, Path]:
    runtime = getattr(state, "runtime", None)
    storage = getattr(runtime, "storage", None)
    data_dir = getattr(storage, "data_dir", None)
    if runtime is None or data_dir is None:
        raise DatabaseUnavailableError("data-store health is unavailable")
    return runtime, Path(data_dir)


def _status(runtime: Any, data_dir: Path) -> JsonObject:
    return data_store_status(data_dir, databases=runtime.canonical_databases())


async def _data_store_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "data_store.status")
    try:
        runtime, data_dir = _runtime(state)
        return await _DATA_STORE_WORKERS.run(_status, runtime, data_dir)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _create_snapshot(runtime: Any, data_dir: Path, reason: str) -> JsonObject:
    before = read_snapshot_health(data_dir)
    snapshot = create_data_snapshot(
        data_dir, reason=reason, databases=runtime.canonical_databases()
    )
    if snapshot is None:
        after = read_snapshot_health(data_dir)
        if after != before and after.get("state") == "degraded" and after.get("reason"):
            raise DatabaseUnavailableError(f"the data snapshot was not created: {after['reason']}")
        raise DatabaseUnavailableError("the data snapshot was not created")
    summary = read_snapshot_summary(data_dir, snapshot)
    if summary is None:
        raise DatabaseUnavailableError("the data snapshot cannot be read back")
    return summary


async def _data_store_snapshot_create(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"reason"}, "data_store.snapshot_create")
    reason = params.get("reason", "rpc")
    if not isinstance(reason, str) or reason not in _SNAPSHOT_REASONS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.reason must be one of manual, rpc, update, or recovery",
        )
    try:
        runtime, data_dir = _runtime(state)
        summary = await _DATA_STORE_WORKERS.run(_create_snapshot, runtime, data_dir, reason)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
    return {"snapshot": summary}


def _acknowledge(runtime: Any, data_dir: Path, incident_id: str) -> JsonObject | None:
    if not acknowledge_incident(data_dir, incident_id):
        return None
    return _status(runtime, data_dir)


async def _data_store_incident_acknowledge(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"incident_id"}, "data_store.incident_acknowledge")
    incident_id = params.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.incident_id must be a non-empty string")
    try:
        runtime, data_dir = _runtime(state)
        status = await _DATA_STORE_WORKERS.run(_acknowledge, runtime, data_dir, incident_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if status is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "recovery incident does not exist")
    publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
    return status


async def _data_store_unregister(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"name"}, "data_store.unregister")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.name must be a non-empty string")
    try:
        runtime, _data_dir = _runtime(state)
        released = await runtime.unregister_extension_database(name)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
    return {
        "unregistered": {
            "name": released.name,
            "database_id": released.database_id,
            "quarantine": None if released.quarantine is None else str(released.quarantine),
        }
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the public data-store handlers."""
    return {
        "data_store.status": _data_store_status,
        "data_store.snapshot_create": _data_store_snapshot_create,
        "data_store.incident_acknowledge": _data_store_incident_acknowledge,
        "data_store.unregister": _data_store_unregister,
    }
