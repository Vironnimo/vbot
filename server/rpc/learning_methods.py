"""RPC handlers for the Memory and Skill changes of one Run and their undo."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from core.automation import LearningUndoConflictError, LearningUndoFailedError
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_MEMORIES, RESOURCE_KIND_SKILLS
from server.rpc._mutations import MutationHandler, serialized_mutation
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_LEARNING_UNDO_CONFLICT,
    RpcError,
)
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported, _required_string

JsonObject = dict[str, Any]
# Both histories are read, and an undo rewrites Memory files and Skill packages,
# here, never on the Event Loop.
_LEARNING_RPC_WORKERS = BoundedWorkerPool(name="learning-rpc", max_workers=2)


def _guard_learning_mutation(handler: MutationHandler) -> MutationHandler:
    mutate = serialized_mutation(handler, lock_attribute="_learning_mutation_lock")

    async def guarded(state: Any, params: JsonObject) -> JsonObject:
        # Keep the Agent and its Workspace in place from the check through the
        # writes and their publication, even if the caller cancels.
        async with _agent_reference_lock(state):
            return await mutate(state, params)

    return guarded


async def _learning_changes(state: Any, params: JsonObject) -> JsonObject:
    """Return every Memory and Skill change one Run of an Identity Agent made."""
    _reject_unsupported(params, {"agent_id", "run_id"}, "learning.changes")
    agent_id = _required_string(params, "agent_id")
    run_id = _required_string(params, "run_id")
    async with _agent_reference_lock(state):
        await _agent_workspace(state, agent_id)
        try:
            changes = await _LEARNING_RPC_WORKERS.run(
                state.runtime.learning_changes.of_run, agent_id, run_id
            )
        except Exception as exc:
            raise _map_expected_error(exc) from exc
    result: JsonObject = changes.to_dict()
    return result


@_guard_learning_mutation
async def _learning_undo(state: Any, params: JsonObject) -> JsonObject:
    """Take back every change of one Run as a person, all of them or none."""
    _reject_unsupported(params, {"agent_id", "run_id"}, "learning.undo")
    agent_id = _required_string(params, "agent_id")
    run_id = _required_string(params, "run_id")
    workspace = await _agent_workspace(state, agent_id)
    try:
        result = await _LEARNING_RPC_WORKERS.run(
            partial(
                state.runtime.learning_changes.undo,
                agent_id,
                run_id,
                workspace=workspace,
                actor="rpc",
            )
        )
    except LearningUndoConflictError as exc:
        raise RpcError(RPC_ERROR_LEARNING_UNDO_CONFLICT, str(exc), data=exc.to_dict()) from exc
    except LearningUndoFailedError as exc:
        # It may have written before it failed, so open views refresh.
        _announce(state, agent_id, memory=True, skills=True)
        raise _map_expected_error(exc) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    _announce(state, agent_id, memory=result.memory_changed, skills=result.skills_changed)
    response: JsonObject = result.changes.to_dict()
    return response


async def _agent_workspace(state: Any, agent_id: str) -> Path:
    try:
        agent = await state.runtime.agents.get_async(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if not agent.workspace:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"Agent '{agent_id}' has no Workspace")
    return Path(agent.workspace)


def _announce(state: Any, agent_id: str, *, memory: bool, skills: bool) -> None:
    if memory:
        publish_resource_changed(state, RESOURCE_KIND_MEMORIES, scope={"agent_id": agent_id})
    if skills:
        state.runtime.invalidate_agent_skills(agent_id)
        publish_resource_changed(state, RESOURCE_KIND_SKILLS)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the learning-change RPC handlers."""
    return {
        "learning.changes": _learning_changes,
        "learning.undo": _learning_undo,
    }
