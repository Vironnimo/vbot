"""Shared agent-reference helpers for server RPC handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES
from core.utils.logging import get_logger

_LOGGER = get_logger("server.rpc.agent_refs")


@dataclass(frozen=True)
class AgentRenameCoordinationResult:
    """Outcome of one committed Identity Agent rename and reference migration."""

    agent: Any
    session_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    cron_job_ids: tuple[str, ...]
    bootstrap_job_ids: tuple[str, ...]
    policy_agent_ids: tuple[str, ...]
    session_reference_count: int


class _NoopAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


def _agent_reference_lock(state: Any) -> Any:
    return getattr(state, "agent_delete_lock", _NOOP_ASYNC_CONTEXT)


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


def _rename_agent_and_retarget_references(
    state: Any,
    agent_id: str,
    new_agent_id: str,
    loop: asyncio.AbstractEventLoop,
) -> AgentRenameCoordinationResult:
    """Rename an Identity Agent and transactionally retarget live references.

    Blocking; runs on a worker thread. Channel retargeting runs on ``loop``,
    the Event Loop that owns the Channel adapters.
    """
    runtime = state.runtime
    session_ids = tuple(
        address.session_id
        for address in runtime.chat_sessions.list_addresses(None, agent_id=agent_id)
    )
    rename_result = None
    policy_result = None
    session_updates: tuple[Any, ...] = ()
    updated_channel_ids: list[str] = []
    updated_cron_job_ids: list[str] = []
    updated_bootstrap_job_ids: list[str] = []
    prior_bootstrap_jobs: dict[str, Any] = {}
    calendar = runtime.calendar_service
    calendar_retargeted = False

    channel_service = runtime.channel_service
    channels = [
        channel for channel in channel_service.list_channels() if channel.agent_id == agent_id
    ]
    cron_service = runtime.cron_service
    cron_jobs = [
        job
        for job in cron_service.list_jobs()
        if (
            job.agent_id == agent_id
            and job.project_id is None
            and getattr(job, "status", "active") not in TERMINAL_CRON_JOB_STATUSES
        )
    ]
    bootstrap_service = runtime.bootstrap_service
    bootstrap_jobs = [
        job
        for job in bootstrap_service.list_jobs()
        if (
            job.agent_id == agent_id
            and job.project_id is None
            and getattr(job, "status", "active") not in TERMINAL_BOOTSTRAP_STATUSES
        )
    ]

    try:
        rename_result = runtime.agents.rename(agent_id, new_agent_id)
        policy_result = runtime.agents.retarget_allowed_agent_references(
            agent_id,
            new_agent_id,
        )
        session_updates = runtime.chat_sessions.retarget_identity_agent_references(
            agent_id,
            new_agent_id,
        )
        for channel in channels:
            _retarget_channel(loop, channel_service, channel.id, new_agent_id)
            updated_channel_ids.append(channel.id)
        for job in cron_jobs:
            cron_service.update_job(job.id, agent_id=new_agent_id)
            updated_cron_job_ids.append(job.id)
        for job in bootstrap_jobs:
            prior_bootstrap_jobs[job.id] = job
            bootstrap_service.retarget_agent(job.id, new_agent_id)
            updated_bootstrap_job_ids.append(job.id)
        calendar.actions.retarget_identity(agent_id, new_agent_id)
        calendar_retargeted = True
    except Exception:
        rollback_errors: list[Exception] = []
        if session_updates:
            _attempt_rollback(
                rollback_errors,
                runtime.chat_sessions.restore_identity_agent_references,
                session_updates,
            )
        if policy_result is not None:
            _attempt_rollback(
                rollback_errors,
                runtime.agents.restore_allowed_agent_references,
                policy_result,
            )
        if rename_result is not None:
            _attempt_rollback(rollback_errors, runtime.agents.restore_rename, rename_result)
        if calendar_retargeted:
            _attempt_rollback(
                rollback_errors, calendar.actions.retarget_identity, new_agent_id, agent_id
            )
        for job_id in reversed(updated_cron_job_ids):
            _attempt_rollback(
                rollback_errors,
                cron_service.update_job,
                job_id,
                agent_id=agent_id,
            )
        for job_id in reversed(updated_bootstrap_job_ids):
            _attempt_rollback(
                rollback_errors,
                bootstrap_service.restore_job,
                prior_bootstrap_jobs[job_id],
            )
        for channel_id in reversed(updated_channel_ids):
            _attempt_rollback(
                rollback_errors,
                _retarget_channel,
                loop,
                channel_service,
                channel_id,
                agent_id,
            )
        if rollback_errors:
            _LOGGER.error(
                "Agent rename rollback incomplete (agent=%s new_agent=%s errors=%s)",
                agent_id,
                new_agent_id,
                "; ".join(str(error) for error in rollback_errors),
            )
        raise

    return AgentRenameCoordinationResult(
        agent=rename_result.agent,
        session_ids=session_ids,
        channel_ids=tuple(updated_channel_ids),
        cron_job_ids=tuple(updated_cron_job_ids),
        bootstrap_job_ids=tuple(updated_bootstrap_job_ids),
        policy_agent_ids=policy_result.agent_ids,
        session_reference_count=len(session_updates),
    )


def _retarget_channel(
    loop: asyncio.AbstractEventLoop,
    channel_service: Any,
    channel_id: str,
    agent_id: str,
) -> None:
    """Point one Channel at ``agent_id`` from a worker thread and wait for the result."""
    asyncio.run_coroutine_threadsafe(
        channel_service.update_channel(channel_id, agent_id=agent_id), loop
    ).result()


def _attempt_rollback(
    errors: list[Exception],
    callback: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    try:
        callback(*args, **kwargs)
    except Exception as error:
        errors.append(error)


_NOOP_ASYNC_CONTEXT = _NoopAsyncContext()
