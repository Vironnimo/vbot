"""Shared agent-reference helpers for server RPC handlers."""

from __future__ import annotations

import asyncio
from typing import Any

from server.rpc._mutations import JsonObject, MutationHandler, serialized_mutation


def _agent_reference_lock(state: Any) -> asyncio.Lock:
    lock: asyncio.Lock = state.agent_delete_lock
    return lock


def _guard_agent_lifecycle(handler: MutationHandler) -> MutationHandler:
    """Serialize a handler that creates, renames, deletes or restores an Agent or Project id.

    It holds the automation reference lock, so reference checks and the change
    they admit are one step, and runs inside the Agent lifecycle mutation.
    """
    mutate = serialized_mutation(handler, lock_attribute="_agent_lifecycle_mutation_lock")

    async def guarded(state: Any, params: JsonObject) -> JsonObject:
        # Waiting for the shared reference lock admits no mutation. Once admitted,
        # keep Run guards, worker persistence and publication together on cancel.
        async with _agent_reference_lock(state):
            return await mutate(state, params)

    return guarded


def _subagents_reference_identity_agent(state: Any, agent_id: str) -> bool:
    """Return whether live Sub-Agent coordination still addresses an identity."""
    return bool(state.runtime.subagents.references_identity_agent(agent_id))
