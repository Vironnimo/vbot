"""Pinned Memory RPC handlers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

from core.external_history import ExternalHistory, ExternalHistoryError
from core.external_history.history import memory_entries
from core.memory import (
    MemoryEntry,
    MemoryRevertIncompleteError,
    MemoryRevision,
    MemoryScope,
    MemoryWriter,
)
from core.memory._file_backend import memory_source_path
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_MEMORIES
from server.extension_services import is_extension_service_call
from server.rpc._mutations import MutationHandler, serialized_mutation
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported, _required_string, rpc_request_fields

JsonObject = dict[str, Any]
_MEMORY_SCOPES: tuple[MemoryScope, ...] = ("agent", "user")
# Memory files are read and rewritten here, never on the Event Loop.
_MEMORY_RPC_WORKERS = BoundedWorkerPool(name="memory-rpc", max_workers=2)


@dataclass(frozen=True)
class _MemoryOwner:
    workspace: Path
    agent_id: str | None = None
    project_id: str | None = None
    actor: str = "rpc"

    @property
    def fields(self) -> JsonObject:
        return (
            {"project_id": self.project_id}
            if self.project_id is not None
            else {"agent_id": self.agent_id}
        )

    @property
    def scopes(self) -> tuple[MemoryScope, ...]:
        return ("project",) if self.project_id is not None else _MEMORY_SCOPES

    @property
    def writer(self) -> MemoryWriter:
        return MemoryWriter(agent_id=self.agent_id, project_id=self.project_id, actor=self.actor)


def _guard_memory_mutation(handler: MutationHandler) -> MutationHandler:
    mutate = serialized_mutation(handler, lock_attribute="_memory_mutation_lock")

    async def guarded(state: Any, params: JsonObject) -> JsonObject:
        # Resolve the Workspace only after admission, and keep it attached to
        # its Agent through the write and publication even if the caller cancels.
        async with _agent_reference_lock(state):
            return await mutate(state, params)

    return guarded


async def _list_memories(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("memory.list"), "memory.list")
    async with _agent_reference_lock(state):
        owner = await _memory_owner(state, params)
        return await _MEMORY_RPC_WORKERS.run(
            _operate, state, owner, partial(_memory_response, state, owner)
        )


@_guard_memory_mutation
async def _add_memory(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("memory.add"), "memory.add")
    owner = await _memory_owner(state, params)
    scope = _memory_scope(params, owner)
    content = _required_string(params, "content")
    response = await _mutate_memory(
        state,
        owner,
        partial(
            state.runtime.memory.add_entry,
            owner.workspace,
            scope,
            content,
            writer=owner.writer,
        ),
    )
    return response


@_guard_memory_mutation
async def _import_memory(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("memory.import"), "memory.import")
    owner = await _memory_owner(state, params)
    scope = _memory_scope(params, owner)
    source = _required_string(params, "source")
    path = _required_string(params, "path")
    expected_digest = params.get("expected_digest")
    if "expected_digest" in params and (
        not isinstance(expected_digest, str) or not re.fullmatch("[0-9a-f]{64}", expected_digest)
    ):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "expected_digest must be a SHA-256 digest.")

    def import_entries() -> JsonObject:
        document = ExternalHistory().read_memory(source, path)
        if expected_digest is not None and document["digest"] != expected_digest:
            raise RpcError(
                "external_memory_changed",
                "The source Memory changed after preview. Read it again and review the facts.",
            )
        entries = params.get("entries")
        if entries is None:
            entries = memory_entries(document["content"])
        if (
            not isinstance(entries, list)
            or not entries
            or not all(isinstance(entry, str) and entry.strip() for entry in entries)
        ):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "entries must contain at least one non-empty fact. Nothing was imported.",
            )
        saved = state.runtime.memory.add_entries(
            owner.workspace,
            scope,
            entries,
            writer=owner.writer,
            source_document=(source, document["content"]),
        )
        return {
            **_memory_response(state, owner),
            "entries": [_entry_response(entry) for entry in saved],
            "source": source,
            "source_path": path,
            "saved_source_path": memory_source_path(
                owner.workspace, source, document["content"]
            ).as_posix(),
        }

    try:
        result: JsonObject = await _MEMORY_RPC_WORKERS.run(_operate, state, owner, import_entries)
    except ExternalHistoryError as exc:
        raise RpcError("external_history_error", str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    _publish_memory_changed(state, owner)
    return result


@_guard_memory_mutation
async def _replace_memory(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        rpc_request_fields("memory.replace"),
        "memory.replace",
    )
    owner = await _memory_owner(state, params)
    scope = _memory_scope(params, owner)
    entry_id = _positive_entry_id(params)
    content = _required_string(params, "content")
    response = await _mutate_memory(
        state,
        owner,
        partial(
            state.runtime.memory.replace_entry,
            owner.workspace,
            scope,
            entry_id,
            content,
            writer=owner.writer,
        ),
    )
    return response


@_guard_memory_mutation
async def _remove_memory(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        rpc_request_fields("memory.remove"),
        "memory.remove",
    )
    owner = await _memory_owner(state, params)
    scope = _memory_scope(params, owner)
    entry_id = _positive_entry_id(params)
    response = await _mutate_memory(
        state,
        owner,
        partial(
            state.runtime.memory.remove_entry,
            owner.workspace,
            scope,
            entry_id,
            writer=owner.writer,
        ),
    )
    return response


async def _memory_history(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("memory.history"), "memory.history")
    limit = _optional_positive_int(params, "limit")
    async with _agent_reference_lock(state):
        owner = await _memory_owner(state, params)
        scope = _memory_scope(params, owner) if params.get("scope") is not None else None
        revisions = await _MEMORY_RPC_WORKERS.run(
            _operate,
            state,
            owner,
            partial(state.runtime.memory.history, owner.workspace, **owner.fields),
        )
    if scope is not None:
        revisions = [revision for revision in revisions if revision.scope == scope]
    shown = revisions[-limit:] if limit is not None else revisions
    return {
        **owner.fields,
        "total": len(revisions),
        "revisions": [_revision_response(revision) for revision in reversed(shown)],
    }


async def _memory_show(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("memory.show"), "memory.show")
    revision = _optional_positive_int(params, "revision")
    async with _agent_reference_lock(state):
        owner = await _memory_owner(state, params)
        scopes = await _MEMORY_RPC_WORKERS.run(
            _operate,
            state,
            owner,
            partial(
                state.runtime.memory.entries_at,
                owner.workspace,
                revision_id=revision,
                **owner.fields,
            ),
        )
    return {**owner.fields, "revision": revision, "scopes": scopes}


async def _memory_diff(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("memory.diff"), "memory.diff")
    from_id = _optional_positive_int(params, "from")
    if from_id is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.from must be a positive integer")
    to_id = _optional_positive_int(params, "to")
    async with _agent_reference_lock(state):
        owner = await _memory_owner(state, params)
        changes = await _MEMORY_RPC_WORKERS.run(
            _operate,
            state,
            owner,
            partial(
                state.runtime.memory.compare,
                owner.workspace,
                owner.agent_id,
                from_id,
                to_id,
                project_id=owner.project_id,
            ),
        )
    return {
        **owner.fields,
        "from": from_id,
        "to": to_id,
        "changes": {
            scope: [change.to_dict() for change in scope_changes]
            for scope, scope_changes in changes.items()
        },
    }


@_guard_memory_mutation
async def _memory_revert(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, rpc_request_fields("memory.revert"), "memory.revert")
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
    owner = await _memory_owner(state, params)
    try:
        result = await _MEMORY_RPC_WORKERS.run(
            _operate,
            state,
            owner,
            partial(
                state.runtime.memory.revert, owner.workspace, revision_ids, writer=owner.writer
            ),
        )
    except MemoryRevertIncompleteError as exc:
        # The scopes it names keep their reverted entries, so open views refresh.
        _publish_memory_changed(state, owner)
        raise _map_expected_error(exc) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Announce every changed file, whether or not the history recorded it.
    if result.changed:
        _publish_memory_changed(state, owner)
    response: JsonObject = await _MEMORY_RPC_WORKERS.run(
        _operate, state, owner, partial(_memory_response, state, owner)
    )
    response["revisions"] = [_revision_response(revision) for revision in result.revisions]
    return response


def _operate[Result](state: Any, owner: _MemoryOwner, operation: Callable[[], Result]) -> Result:
    """Keep Project lookup and file work inside its lifecycle/snapshot guard."""
    try:
        if owner.project_id is not None:
            with state.runtime.projects.configuration_guard():
                state.runtime.projects.memory_workspace(owner.project_id)
                return operation()
        return operation()
    except MemoryRevertIncompleteError:
        # Revert publishes the files left changed before mapping this error.
        raise
    except Exception as exc:
        raise _map_expected_error(exc) from exc


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


async def _memory_owner(state: Any, params: JsonObject) -> _MemoryOwner:
    actor = "extension" if is_extension_service_call() else "rpc"
    if ("agent_id" in params) == ("project_id" in params):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params must name exactly one of agent_id or project_id",
        )
    if "project_id" in params:
        project_id = _required_string(params, "project_id")
        workspace = await _MEMORY_RPC_WORKERS.run(_project_workspace, state, project_id)
        return _MemoryOwner(workspace=workspace, project_id=project_id, actor=actor)
    agent_id = _required_string(params, "agent_id")
    try:
        agent = await state.runtime.agents.get_async(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _MemoryOwner(workspace=Path(agent.workspace), agent_id=agent_id, actor=actor)


def _project_workspace(state: Any, project_id: str) -> Path:
    try:
        with state.runtime.projects.configuration_guard():
            return cast(Path, state.runtime.projects.memory_workspace(project_id))
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _mutate_memory(
    state: Any,
    owner: _MemoryOwner,
    mutation: Callable[[], MemoryEntry],
) -> JsonObject:
    """Apply one memory file mutation off the Event Loop, then announce it."""
    response: JsonObject = await _MEMORY_RPC_WORKERS.run(
        _operate, state, owner, partial(_apply_mutation, state, owner, mutation)
    )
    _publish_memory_changed(state, owner)
    return response


def _apply_mutation(
    state: Any,
    owner: _MemoryOwner,
    mutation: Callable[[], MemoryEntry],
) -> JsonObject:
    try:
        entry = mutation()
        return _memory_response(state, owner, entry=entry)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _memory_scope(params: JsonObject, owner: _MemoryOwner) -> MemoryScope:
    scope = params.get("scope")
    if not isinstance(scope, str) or scope not in owner.scopes:
        allowed = ", ".join(repr(item) for item in owner.scopes)
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
    owner: _MemoryOwner,
    *,
    entry: MemoryEntry | None = None,
) -> JsonObject:
    try:
        scopes = {
            scope: [
                _entry_response(item)
                for item in state.runtime.memory.list_entries(owner.workspace, scope)
            ]
            for scope in owner.scopes
        }
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response: JsonObject = {**owner.fields, "scopes": scopes}
    if entry is not None:
        response["entry"] = _entry_response(entry)
    return response


def _entry_response(entry: MemoryEntry) -> JsonObject:
    return {"id": entry.id, "scope": entry.scope, "content": entry.content}


def _publish_memory_changed(state: Any, owner: _MemoryOwner) -> None:
    publish_resource_changed(
        state,
        RESOURCE_KIND_MEMORIES,
        scope=owner.fields,
    )


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return pinned Memory RPC handlers."""
    return {
        "memory.list": _list_memories,
        "memory.add": _add_memory,
        "memory.import": _import_memory,
        "memory.replace": _replace_memory,
        "memory.remove": _remove_memory,
        "memory.history": _memory_history,
        "memory.show": _memory_show,
        "memory.diff": _memory_diff,
        "memory.revert": _memory_revert,
    }
