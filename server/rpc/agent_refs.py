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


def _agent_reference_ids(state: Any, agent_id: str) -> list[str]:
    runtime = state.runtime
    references: list[str] = []

    channel_service = getattr(runtime, "channel_service", None)
    if channel_service is not None:
        references.extend(
            f"channel:{channel.id}"
            for channel in channel_service.list_channels()
            if channel.agent_id == agent_id
        )

    cron_service = getattr(runtime, "cron_service", None)
    if cron_service is not None:
        # Only bare (``project_id is None``) cron jobs count against the identity
        # agent. A job qualified with a ``project_id`` targets that project's
        # Team agent, not the same-named identity agent, so it must not block the
        # identity delete (the project removal guard owns that lock instead).
        references.extend(
            f"cron:{job.id}"
            for job in cron_service.list_jobs()
            if job.agent_id == agent_id and job.project_id is None
        )

    return sorted(references)


_NOOP_ASYNC_CONTEXT = _NoopAsyncContext()
