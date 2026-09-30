"""Shared agent-reference helpers for server RPC handlers."""

from __future__ import annotations

import asyncio
from typing import Any

from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES


def _agent_reference_lock(state: Any) -> asyncio.Lock:
    lock: asyncio.Lock = state.agent_delete_lock
    return lock


def _agent_reference_ids(state: Any, agent_id: str) -> list[str]:
    runtime = state.runtime
    references: list[str] = [
        f"calendar:{action['id']}"
        for action in runtime.calendar_service.actions.list_actions()
        if action["target"] == agent_id
    ]
    references.extend(
        f"channel:{channel.id}"
        for channel in runtime.channel_service.list_channels()
        if channel.agent_id == agent_id
    )
    # Only bare (``project_id is None``) cron jobs count against the identity
    # agent. A job qualified with a ``project_id`` targets that project's
    # Team agent, not the same-named identity agent, so it must not block the
    # identity delete (the project removal guard owns that lock instead).
    references.extend(
        f"cron:{job.id}"
        for job in runtime.cron_service.list_jobs()
        if (
            job.agent_id == agent_id
            and job.project_id is None
            and getattr(job, "status", "active") not in TERMINAL_CRON_JOB_STATUSES
        )
    )
    references.extend(
        f"bootstrap:{job.id}"
        for job in runtime.bootstrap_service.list_jobs()
        if (
            job.agent_id == agent_id
            and job.project_id is None
            and getattr(job, "status", "active") not in TERMINAL_BOOTSTRAP_STATUSES
        )
    )
    return sorted(references)


def _subagents_reference_identity_agent(state: Any, agent_id: str) -> bool:
    """Return whether live Sub-Agent coordination still addresses an identity."""
    return bool(state.runtime.subagents.batch_tracker.references_identity_agent(agent_id))

