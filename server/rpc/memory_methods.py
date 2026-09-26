"""Pinned Memory RPC handlers."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, cast

from core.memory import MemoryEntry, MemoryScope
from core.utils.logging import get_logger
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
_LOGGER = get_logger("server.rpc.memory")
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
        partial(state.runtime.memory.add_entry, workspace, scope, content),
    )
    _LOGGER.info("Memory entry added (agent=%s scope=%s)", agent_id, scope)
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
        partial(state.runtime.memory.replace_entry, workspace, scope, entry_id, content),
    )
    _LOGGER.info("Memory entry replaced (agent=%s scope=%s)", agent_id, scope)
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
        partial(state.runtime.memory.remove_entry, workspace, scope, entry_id),
    )
    _LOGGER.info("Memory entry removed (agent=%s scope=%s)", agent_id, scope)
    return response


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
    }
