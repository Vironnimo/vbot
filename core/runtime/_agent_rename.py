"""Identity Agent rename across every owner that references the Agent id.

The Agent store owns the rename record and the Agent-owned half of a rename: the
Agent tree and config, its Sessions, Sub-Agent parent links, the roster order and
delegation allow-lists (``AgentStore.rename``). Channels, Cron, Bootstrap and
Calendar each hold references to the Agent id that their owners retarget. Runtime
owns every one of these services, so it orders the two halves: the Agent-owned
half first, so references always name an existing Agent, then the references,
then the record is finished.

Every step selects only what still names the id it replaces, so repeating a
direction converges and reversing the ids reverts it. A failure reverts the whole
rename; a process that dies mid-rename leaves the record, and the next start
completes its direction (:func:`complete_pending_rename`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from core.agents import Agent, AgentRename, AgentStore
from core.automation import BootstrapService, CronService
from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES
from core.calendar import CalendarService
from core.channels import ChannelService
from core.sessions import ChatSessionManager
from core.utils.logging import get_logger

_LOGGER = get_logger("runtime.agent_rename")


@dataclass(frozen=True)
class AgentRenameServices:
    """The owners one Identity Agent rename changes."""

    agents: AgentStore
    sessions: ChatSessionManager
    channels: ChannelService
    cron: CronService
    bootstrap: BootstrapService
    calendar: CalendarService


@dataclass(frozen=True)
class AgentRenameOutcome:
    """One completed Identity Agent rename and the references it moved."""

    agent: Agent
    session_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    cron_job_ids: tuple[str, ...]
    bootstrap_job_ids: tuple[str, ...]
    policy_agent_ids: tuple[str, ...]
    session_link_count: int
    calendar_action_count: int


@dataclass(frozen=True)
class _References:
    channel_ids: tuple[str, ...]
    cron_job_ids: tuple[str, ...]
    bootstrap_job_ids: tuple[str, ...]
    calendar_action_count: int


async def rename_identity_agent(
    services: AgentRenameServices, agent_id: str, new_agent_id: str
) -> AgentRenameOutcome:
    """Rename one Identity Agent and every reference to it as one recoverable change.

    Blocking work runs on the Session database's pool; Channel changes run on this
    Event Loop, which owns the Channel adapters. The caller holds the Run admission
    guards of both ids. A failure reverts every change before it is raised.
    """
    loop = asyncio.get_running_loop()
    return await services.sessions.run_async(_rename, services, agent_id, new_agent_id, loop)


def complete_pending_rename(services: AgentRenameServices, rename: AgentRename) -> None:
    """Finish a rename whose Agent-owned half ``AgentStore.recover_rename`` completed.

    Runs during startup, before Channels, Cron and Calendar start. References are
    retargeted in the record's direction; when that fails, the rename is reverted
    instead. A rename that reaches neither end keeps its record for the next start.
    """
    try:
        _retarget_references(services, rename, None)
    except Exception as error:
        _LOGGER.warning(
            "Agent rename could not be completed after restart; reverting it "
            "(agent=%s new_agent=%s): %s",
            rename.old_id,
            rename.new_id,
            error,
        )
        try:
            rename = services.agents.revert_rename(rename)
            _retarget_references(services, rename, None)
        except Exception as revert_error:
            _LOGGER.error(
                "Agent rename recovery failed; the next start retries it (agent=%s new_agent=%s)",
                rename.old_id,
                rename.new_id,
                exc_info=(type(revert_error), revert_error, revert_error.__traceback__),
            )
            return
    if not _finish(services, rename):
        return
    _LOGGER.info(
        "Agent rename %s after restart (agent=%s new_agent=%s)",
        "rolled back" if rename.rollback else "completed",
        rename.old_id,
        rename.new_id,
    )


def _rename(
    services: AgentRenameServices,
    agent_id: str,
    new_agent_id: str,
    loop: asyncio.AbstractEventLoop,
) -> AgentRenameOutcome:
    result = services.agents.rename(agent_id, new_agent_id)
    try:
        references = _retarget_references(services, result.rename, loop)
    except Exception as error:
        _revert(services, result.rename, loop, error)
        raise
    _finish(services, result.rename)
    outcome = AgentRenameOutcome(
        agent=result.agent,
        session_ids=result.session_ids,
        channel_ids=references.channel_ids,
        cron_job_ids=references.cron_job_ids,
        bootstrap_job_ids=references.bootstrap_job_ids,
        policy_agent_ids=result.policy_agent_ids,
        session_link_count=result.session_link_count,
        calendar_action_count=references.calendar_action_count,
    )
    _LOGGER.info(
        "Agent renamed (agent=%s new_agent=%s sessions=%s channels=%s cron=%s "
        "bootstrap=%s calendar_actions=%s policies=%s session_links=%s)",
        agent_id,
        new_agent_id,
        len(outcome.session_ids),
        len(outcome.channel_ids),
        len(outcome.cron_job_ids),
        len(outcome.bootstrap_job_ids),
        outcome.calendar_action_count,
        len(outcome.policy_agent_ids),
        outcome.session_link_count,
    )
    return outcome


def _revert(
    services: AgentRenameServices,
    rename: AgentRename,
    loop: asyncio.AbstractEventLoop,
    error: Exception,
) -> None:
    """Revert a rename whose references failed; the next start finishes a failed revert."""
    try:
        reverse = services.agents.revert_rename(rename)
        _retarget_references(services, reverse, loop)
    except Exception as revert_error:
        _LOGGER.error(
            "Agent rename rollback incomplete; the next start finishes it "
            "(agent=%s new_agent=%s error=%s)",
            rename.old_id,
            rename.new_id,
            error,
            exc_info=(type(revert_error), revert_error, revert_error.__traceback__),
        )
        return
    _finish(services, reverse)


def _finish(services: AgentRenameServices, rename: AgentRename) -> bool:
    """Remove the record of a rename that reached one end; a failure only defers that."""
    try:
        services.agents.finish_rename(rename)
    except Exception as error:
        _LOGGER.error(
            "Agent rename record could not be removed; the next start removes it "
            "(agent=%s new_agent=%s)",
            rename.old_id,
            rename.new_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return False
    return True


def _retarget_references(
    services: AgentRenameServices,
    rename: AgentRename,
    loop: asyncio.AbstractEventLoop | None,
) -> _References:
    """Point every Channel, Cron job, Bootstrap job and Calendar action at the target id.

    Only non-terminal jobs that target the Identity Agent itself move: completed
    history stays as it ran, and a Project-qualified job targets that Project's
    Team Agent. Without ``loop`` (startup), Channels are rewritten before their
    service starts; with it, the Channel service decides on that loop whether its
    adapters must follow.
    """
    source, target = rename.source_id, rename.target_id
    channel_ids = _retarget_channels(services.channels, source, target, loop)
    cron_job_ids: list[str] = []
    for cron_job in services.cron.list_jobs():
        if _targets_identity(cron_job, source, TERMINAL_CRON_JOB_STATUSES):
            services.cron.retarget_agent(cron_job.id, target)
            cron_job_ids.append(cron_job.id)
    bootstrap_job_ids: list[str] = []
    for bootstrap_job in services.bootstrap.list_jobs():
        if _targets_identity(bootstrap_job, source, TERMINAL_BOOTSTRAP_STATUSES):
            services.bootstrap.retarget_agent(bootstrap_job.id, target)
            bootstrap_job_ids.append(bootstrap_job.id)
    calendar_action_count = services.calendar.actions.retarget_identity(source, target)
    return _References(
        channel_ids=channel_ids,
        cron_job_ids=tuple(cron_job_ids),
        bootstrap_job_ids=tuple(bootstrap_job_ids),
        calendar_action_count=calendar_action_count,
    )


def _retarget_channels(
    channels: ChannelService,
    source: str,
    target: str,
    loop: asyncio.AbstractEventLoop | None,
) -> tuple[str, ...]:
    if loop is None:
        return channels.retarget_agent(source, target)
    # A running service rebuilds each adapter on the Event Loop that owns it; a
    # stopped one only rewrites the configs and starts none.
    return asyncio.run_coroutine_threadsafe(
        channels.retarget_agent_async(source, target), loop
    ).result()


def _targets_identity(job: Any, agent_id: str, terminal_statuses: frozenset[str]) -> bool:
    return bool(
        job.agent_id == agent_id
        and job.project_id is None
        and getattr(job, "status", "active") not in terminal_statuses
    )
