"""Shared agent-reference helpers for server RPC handlers."""

from __future__ import annotations

import asyncio
from typing import Any


def _agent_reference_lock(state: Any) -> asyncio.Lock:
    lock: asyncio.Lock = state.agent_delete_lock
    return lock


def _subagents_reference_identity_agent(state: Any, agent_id: str) -> bool:
    """Return whether live Sub-Agent coordination still addresses an identity."""
    return bool(state.runtime.subagents.batch_tracker.references_identity_agent(agent_id))
