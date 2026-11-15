"""External source reads and Session import through the existing lifecycle boundary."""

from __future__ import annotations

from typing import Any

from core.external_history import ExternalHistory, ExternalHistoryError
from core.external_history.history import import_metadata
from core.utils.workers import BoundedWorkerPool
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RpcError
from server.rpc.session_methods import _create_session
from server.rpc.validation import (
    _optional_string,
    _reject_unsupported,
    _required_string,
    rpc_request_fields,
)

_WORKERS = BoundedWorkerPool(name="external-history", max_workers=2)


def _page(params: dict[str, Any]) -> dict[str, int]:
    offset, limit = params.get("offset", 0), params.get("limit", 30)
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
    ):
        raise RpcError("invalid_request", "Use offset >= 0 and limit between 1 and 100.")
    return {"offset": offset, "limit": limit}


async def _read(state: Any, params: dict[str, Any], action: str) -> dict[str, Any]:
    method = "external." + action
    _reject_unsupported(params, rpc_request_fields(method), method)
    source, path = _required_string(params, "source"), _optional_string(params, "path")
    arguments: dict[str, Any] = {"path": path, **_page(params)}
    if action in {"show", "memories"}:
        value = params.get("char_offset", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RpcError("invalid_request", "Use char_offset >= 0.")
        arguments["char_offset"] = value
    if action in {"list", "show"}:
        arguments["query"] = _optional_string(params, "query") or ""
    if action == "show":
        arguments["session_id"] = _required_string(params, "source_session_id")
    try:
        return await _WORKERS.run(getattr(ExternalHistory(), action), source, **arguments)
    except ExternalHistoryError as exc:
        raise RpcError("external_history_error", str(exc)) from exc


async def _sources(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(params, rpc_request_fields("external.sources"), "external.sources")
    return {"items": await _WORKERS.run(ExternalHistory().sources)}


async def _list(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    return await _read(state, params, "list")


async def _show(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    return await _read(state, params, "show")


async def _memories(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    return await _read(state, params, "memories")


async def _import_session(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(
        params,
        rpc_request_fields("external.import_session"),
        "external.import_session",
    )
    source, identity = (
        _required_string(params, "source"),
        _required_string(params, "source_session_id"),
    )
    try:
        session = await _WORKERS.run(
            ExternalHistory().read, source, identity, path=_optional_string(params, "path")
        )
        messages, provenance = await _WORKERS.run(
            lambda: (session.messages(), import_metadata(session))
        )
    except ExternalHistoryError as exc:
        raise RpcError("external_history_error", str(exc)) from exc
    target = {
        key: value
        for key, value in params.items()
        if key in {"agent_id", "working_project_id", "make_current"}
    }
    result = await _create_session(
        state,
        target,
        initial_messages=messages,
        initial_metadata={"title": session.title, "external_import": provenance},
    )
    return {
        **result,
        "agent_id": target["agent_id"],
        "source": source,
        "entry_count": len(messages),
        "warnings": session.warnings,
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {
        "external.sources": _sources,
        "external.list": _list,
        "external.show": _show,
        "external.memories": _memories,
        "external.import_session": _import_session,
    }
