"""Shared agent-reference helpers for server RPC handlers."""

from __future__ import annotations

from typing import Any


class _NoopAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


def _agent_reference_lock(state: Any) -> Any:
    return getattr(state, "agent_delete_lock", _NOOP_ASYNC_CONTEXT)


def _subagents_reference_identity_agent(state: Any, agent_id: str) -> bool:
    """Return whether live Sub-Agent coordination still addresses an identity."""
    return bool(state.runtime.subagents.batch_tracker.references_identity_agent(agent_id))


_NOOP_ASYNC_CONTEXT = _NoopAsyncContext()
