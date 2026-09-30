"""Pinned Memory RPC handlers."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, TypeVar, cast

from core.memory import (
    MemoryEntry,
    MemoryRevertIncompleteError,
    MemoryRevision,
    MemoryScope,
    MemoryWriter,
)
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_MEMORIES
from server.rpc._mutations import MutationHandler, serialized_mutation
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported, _required_string

JsonObject = dict[str, Any]
_Result = TypeVar("_Result")
_MEMORY_SCOPES: tuple[MemoryScope, ...] = ("agent", "user")
# Memory files are read and rewritten here, never on the Event Loop.
_MEMORY_RPC_WORKERS = BoundedWorkerPool(name="memory-rpc", max_workers=2)


def _guard_memory_mutation(handler: MutationHandler) -> MutationHandler:
    mutate = serialized_mutation(handler, lock_attribute="_memory_mutation_lock")

    async def guarded(state: Any, params: JsonObject) -> JsonObject:
        # Resolve the Workspace only after admission, and keep it attached to
        # its Agent through the write and publication even if the caller cancels.
        async with _agent_reference_lock(state):
            return await mutate(state, params)

    return guarded


async def _list_memories(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id"}, "memory.list")
    async with _agent_reference_lock(state):
        agent_id, workspace = await _agent_workspace(state, params)
        return await _MEMORY_RPC_WORKERS.run(_memory_response, state, agent_id, workspace)


@_guard_memory_mutation
async def _add_memory(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "scope", "content"}, "memory.add")
    agent_id, workspace = await _agent_workspace(state, params)
    scope = _memory_scope(params)
    content = _required_string(params, "content")
    response = await _mutate_memory(
        state,
        agent_id,
        workspace,
        partial(
            state.runtime.memory.add_entry,
            workspace,
            scope,
            content,
            writer=MemoryWriter(agent_id=agent_id, actor="rpc"),
        ),
    )
    return response


@_guard_memory_mutation
async def _replace_memory(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"agent_id", "scope", "entry_id", "content"},
        "memory.replace",
    )
    agent_id, workspace = await _agent_workspace(state, params)
    scope = _memory_scope(params)
    entry_id = _positive_entry_id(params)
    content = _required_string(params, "content")
    response = await _mutate_memory(
        state,
        agent_id,
        workspace,
        partial(
            state.runtime.memory.replace_entry,
            workspace,
            scope,
            entry_id,
            content,
            writer=MemoryWriter(agent_id=agent_id, actor="rpc"),
        ),
    )
    return response


@_guard_memory_mutation
async def _remove_memory(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {"agent_id", "scope", "entry_id"},
        "memory.remove",
    )
    agent_id, workspace = await _agent_workspace(state, params)
    scope = _memory_scope(params)
    entry_id = _positive_entry_id(params)
    response = await _mutate_memory(
        state,
        agent_id,
        workspace,
        partial(
            state.runtime.memory.remove_entry,
            workspace,
            scope,
            entry_id,
            writer=MemoryWriter(agent_id=agent_id, actor="rpc"),
        ),
    )
    return response


async def _memory_history(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "scope", "limit"}, "memory.history")
    scope = _memory_scope(params) if params.get("scope") is not None else None
    limit = _optional_positive_int(params, "limit")
    async with _agent_reference_lock(state):
        agent_id, workspace = await _agent_workspace(state, params)
        revisions = await _MEMORY_RPC_WORKERS.run(
            _expected(state.runtime.memory.history), workspace, agent_id
        )
    if scope is not None:
        revisions = [revision for revision in revisions if revision.scope == scope]
    shown = revisions[-limit:] if limit is not None else revisions
    return {
        "agent_id": agent_id,
        "total": len(revisions),
        "revisions": [_revision_response(revision) for revision in reversed(shown)],
    }


async def _memory_show(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "revision"}, "memory.show")
    revision = _optional_positive_int(params, "revision")
    async with _agent_reference_lock(state):
        agent_id, workspace = await _agent_workspace(state, params)
        scopes = await _MEMORY_RPC_WORKERS.run(
            _expected(state.runtime.memory.entries_at), workspace, agent_id, revision
        )
    return {"agent_id": agent_id, "revision": revision, "scopes": scopes}


async def _memory_diff(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "from", "to"}, "memory.diff")
    from_id = _optional_positive_int(params, "from")
    if from_id is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.from must be a positive integer")
    to_id = _optional_positive_int(params, "to")
    async with _agent_reference_lock(state):
        agent_id, workspace = await _agent_workspace(state, params)
        changes = await _MEMORY_RPC_WORKERS.run(
            _expected(state.runtime.memory.compare), workspace, agent_id, from_id, to_id
        )
    return {
        "agent_id": agent_id,
        "from": from_id,
        "to": to_id,
        "changes": {
            scope: [change.to_dict() for change in scope_changes]
            for scope, scope_changes in changes.items()
        },
    }


@_guard_memory_mutation
async def _memory_revert(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "revisions"}, "memory.revert")
    revision_ids = params.get("revisions")
    if (
        not isinstance(revision_ids, list)
        or not revision_ids
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in revision_ids
        )
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.revisions must be a non-empty list of positive integers",
        )
    agent_id, workspace = await _agent_workspace(state, params)
    try:
        result = await _MEMORY_RPC_WORKERS.run(
            state.runtime.memory.revert,
            workspace,
            revision_ids,
            writer=MemoryWriter(agent_id=agent_id, actor="rpc"),
        )
    except MemoryRevertIncompleteError as exc:
        # The scopes it names keep their reverted entries, so open views refresh.
        _publish_memory_changed(state, agent_id)
        raise _map_expected_error(exc) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Announce every changed file, whether or not the history recorded it.
    if result.changed:
        _publish_memory_changed(state, agent_id)
    response = await _MEMORY_RPC_WORKERS.run(_memory_response, state, agent_id, workspace)
    response["revisions"] = [_revision_response(revision) for revision in result.revisions]
    return response


def _expected(operation: Callable[..., _Result]) -> Callable[..., _Result]:
    """Run a Memory operation, mapping its expected failures to RPC errors."""

    def run(*args: Any, **kwargs: Any) -> _Result:
        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            raise _map_expected_error(exc) from exc

    return run


def _optional_positive_int(params: JsonObject, name: str) -> int | None:
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{name} must be a positive integer")
    return value


def _revision_response(revision: MemoryRevision) -> JsonObject:
    data = revision.to_dict()
    data.pop("entries", None)
    return data


async def _agent_workspace(state: Any, params: JsonObject) -> tuple[str, Path]:
    agent_id = _required_string(params, "agent_id")
    try:
        agent = await state.runtime.agents.get_async(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return agent_id, Path(agent.workspace)


async def _mutate_memory(
    state: Any,
    agent_id: str,
    workspace: Path,
    mutation: Callable[[], MemoryEntry],
) -> JsonObject:
    """Apply one memory file mutation off the Event Loop, then announce it."""
    response = await _MEMORY_RPC_WORKERS.run(_apply_mutation, state, agent_id, workspace, mutation)
    _publish_memory_changed(state, agent_id)
    return response


def _apply_mutation(
    state: Any,
    agent_id: str,
    workspace: Path,
    mutation: Callable[[], MemoryEntry],
) -> JsonObject:
    try:
        entry = mutation()
        return _memory_response(state, agent_id, workspace, entry=entry)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _memory_scope(params: JsonObject) -> MemoryScope:
    scope = params.get("scope")
    if not isinstance(scope, str) or scope not in _MEMORY_SCOPES:
        allowed = ", ".join(repr(item) for item in _MEMORY_SCOPES)
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.scope must be one of: {allowed}",
        )
    return cast(MemoryScope, scope)


def _positive_entry_id(params: JsonObject) -> int:
    entry_id = params.get("entry_id")
    if isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id <= 0:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.entry_id must be a positive integer",
        )
    return entry_id


def _memory_response(
    state: Any,
    agent_id: str,
    workspace: Path,
    *,
    entry: MemoryEntry | None = None,
) -> JsonObject:
    try:
        scopes = {
            scope: [
                _entry_response(item)
                for item in state.runtime.memory.list_entries(workspace, scope)
            ]
            for scope in _MEMORY_SCOPES
        }
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response: JsonObject = {"agent_id": agent_id, "scopes": scopes}
    if entry is not None:
        response["entry"] = _entry_response(entry)
    return response


def _entry_response(entry: MemoryEntry) -> JsonObject:
    return {"id": entry.id, "scope": entry.scope, "content": entry.content}


def _publish_memory_changed(state: Any, agent_id: str) -> None:
    publish_resource_changed(
        state,
        RESOURCE_KIND_MEMORIES,
        scope={"agent_id": agent_id},
    )


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return pinned Memory RPC handlers."""
    return {
        "memory.list": _list_memories,
        "memory.add": _add_memory,
        "memory.replace": _replace_memory,
        "memory.remove": _remove_memory,
        "memory.history": _memory_history,
        "memory.show": _memory_show,
        "memory.diff": _memory_diff,
        "memory.revert": _memory_revert,
    }
