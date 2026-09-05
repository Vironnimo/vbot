"""Shared agent-reference helpers for server RPC handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES
from core.calendar import CalendarService
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
    references: list[str] = []
    calendar = getattr(runtime, "calendar_service", None)
    if isinstance(calendar, CalendarService):
        references.extend(
            f"calendar:{action['id']}"
            for action in calendar.actions.list_actions()
            if action["target"] == agent_id
        )

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
            if (
                job.agent_id == agent_id
                and job.project_id is None
                and getattr(job, "status", "active") not in TERMINAL_CRON_JOB_STATUSES
            )
        )

    bootstrap_service = getattr(runtime, "bootstrap_service", None)
    if bootstrap_service is not None:
        references.extend(
            f"bootstrap:{job.id}"
            for job in bootstrap_service.list_jobs()
            if (
                job.agent_id == agent_id
                and job.project_id is None
                and getattr(job, "status", "active") not in TERMINAL_BOOTSTRAP_STATUSES
            )
        )

    return sorted(references)


def _subagents_reference_identity_agent(state: Any, agent_id: str) -> bool:
    """Return whether live Sub-Agent coordination still addresses an identity."""
    coordinator = getattr(state.runtime, "subagents", None)
    tracker = getattr(coordinator, "batch_tracker", None)
    references = getattr(tracker, "references_identity_agent", None)
    return bool(callable(references) and references(agent_id))


def _rename_agent_and_retarget_references(
    state: Any,
    agent_id: str,
    new_agent_id: str,
) -> AgentRenameCoordinationResult:
    """Rename an Identity Agent and transactionally retarget live references."""
    runtime = state.runtime
    session_ids = tuple(
        session.id for session in runtime.chat_sessions.list(agent_id, project_id=None)
    )
    rename_result = None
    policy_result = None
    session_updates: tuple[Any, ...] = ()
    updated_channel_ids: list[str] = []
    updated_cron_job_ids: list[str] = []
    updated_bootstrap_job_ids: list[str] = []
    prior_bootstrap_jobs: dict[str, Any] = {}
    calendar = getattr(runtime, "calendar_service", None)
    calendar_retargeted = False

    channel_service = getattr(runtime, "channel_service", None)
    channels = (
        [channel for channel in channel_service.list_channels() if channel.agent_id == agent_id]
        if channel_service is not None
        else []
    )
    cron_service = getattr(runtime, "cron_service", None)
    cron_jobs = (
        [
            job
            for job in cron_service.list_jobs()
            if (
                job.agent_id == agent_id
                and job.project_id is None
                and getattr(job, "status", "active") not in TERMINAL_CRON_JOB_STATUSES
            )
        ]
        if cron_service is not None
        else []
    )
    bootstrap_service = getattr(runtime, "bootstrap_service", None)
    bootstrap_jobs = (
        [
            job
            for job in bootstrap_service.list_jobs()
            if (
                job.agent_id == agent_id
                and job.project_id is None
                and getattr(job, "status", "active") not in TERMINAL_BOOTSTRAP_STATUSES
            )
        ]
        if bootstrap_service is not None
        else []
    )

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
        if channel_service is not None:
            for channel in channels:
                channel_service.update_channel(channel.id, agent_id=new_agent_id)
                updated_channel_ids.append(channel.id)
        if cron_service is not None:
            for job in cron_jobs:
                cron_service.update_job(job.id, agent_id=new_agent_id)
                updated_cron_job_ids.append(job.id)
        if bootstrap_service is not None:
            for job in bootstrap_jobs:
                prior_bootstrap_jobs[job.id] = job
                bootstrap_service.update_job(job.id, agent_id=new_agent_id)
                updated_bootstrap_job_ids.append(job.id)
        if isinstance(calendar, CalendarService):
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
        if calendar_retargeted and isinstance(calendar, CalendarService):
            _attempt_rollback(
                rollback_errors, calendar.actions.retarget_identity, new_agent_id, agent_id
            )
        if cron_service is not None:
            for job_id in reversed(updated_cron_job_ids):
                _attempt_rollback(
                    rollback_errors,
                    cron_service.update_job,
                    job_id,
                    agent_id=agent_id,
                )
        if bootstrap_service is not None:
            for job_id in reversed(updated_bootstrap_job_ids):
                restore_job = getattr(bootstrap_service, "restore_job", None)
                if callable(restore_job):
                    _attempt_rollback(
                        rollback_errors,
                        restore_job,
                        prior_bootstrap_jobs[job_id],
                    )
                else:
                    _attempt_rollback(
                        rollback_errors,
                        bootstrap_service.update_job,
                        job_id,
                        agent_id=agent_id,
                    )
        if channel_service is not None:
            for channel_id in reversed(updated_channel_ids):
                _attempt_rollback(
                    rollback_errors,
                    channel_service.update_channel,
                    channel_id,
                    agent_id=agent_id,
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
