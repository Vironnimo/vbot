"""RPC projections for current-format Session-store health operations."""

from __future__ import annotations

from typing import Any

from core.sessions.errors import SessionRecoveryConflictError, SessionStoreUnavailableError
from core.sessions.snapshots import acknowledge_recovery_incident, snapshot_summaries
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_SESSION_STORE
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]
_SESSION_STORE_WORKERS = BoundedWorkerPool(name="session-store", max_workers=2)
_SNAPSHOT_REASONS = frozenset({"manual", "rpc", "update", "recovery"})


def _sessions(state: Any) -> Any:
    runtime = getattr(state, "runtime", None)
    sessions = getattr(runtime, "chat_sessions", None)
    if sessions is None or not callable(getattr(sessions, "status_projection", None)):
        raise SessionStoreUnavailableError("Session-store health is unavailable")
    return sessions


async def _session_store_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "session_store.status")
    try:
        return await _SESSION_STORE_WORKERS.run(_sessions(state).status_projection)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _session_store_snapshot_create(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"reason"}, "session_store.snapshot_create")
    reason = params.get("reason", "rpc")
    if not isinstance(reason, str) or reason not in _SNAPSHOT_REASONS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.reason must be one of manual, rpc, update, or recovery",
        )
    runtime = getattr(state, "runtime", None)
    capture = getattr(runtime, "_capture_session_snapshot", None)
    if not callable(capture):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Session snapshot creation is unavailable")
    try:
        snapshot = await _SESSION_STORE_WORKERS.run(capture, reason=reason)
        if snapshot is None:
            raise SessionStoreUnavailableError("Session snapshot was not created")
        storage = getattr(runtime, "storage", None)
        data_dir = getattr(storage, "data_dir", None)
        if data_dir is None:
            raise SessionStoreUnavailableError("Session-store snapshot verification is unavailable")
        summaries = snapshot_summaries(data_dir)
        summary = next((item for item in summaries if item["snapshot_id"] == snapshot.name), None)
        if summary is None:
            raise SessionStoreUnavailableError("Session snapshot verification is unavailable")
        publish_resource_changed(state, RESOURCE_KIND_SESSION_STORE)
        return {"snapshot": summary}
    except RpcError:
        raise
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _session_store_incident_acknowledge(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"incident_id"}, "session_store.incident_acknowledge")
    incident_id = params.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.incident_id must be a non-empty string")
    runtime = getattr(state, "runtime", None)
    storage = getattr(runtime, "storage", None)
    data_dir = getattr(storage, "data_dir", None)
    if data_dir is None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "Session-store incident acknowledgement is unavailable"
        )
    sessions = _sessions(state)
    try:
        acknowledged = await _SESSION_STORE_WORKERS.run(
            acknowledge_recovery_incident, data_dir, incident_id
        )
        if not acknowledged:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "recovery incident does not exist")
        publish_resource_changed(state, RESOURCE_KIND_SESSION_STORE)
        return await _SESSION_STORE_WORKERS.run(sessions.status_projection)
    except RpcError:
        raise
    except SessionRecoveryConflictError as exc:
        raise _map_expected_error(exc) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the public current-format Session-store handlers."""
    return {
        "session_store.status": _session_store_status,
        "session_store.snapshot_create": _session_store_snapshot_create,
        "session_store.incident_acknowledge": _session_store_incident_acknowledge,
    }
