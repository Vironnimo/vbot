"""Identity Agent rename across every owner that references the Agent id.

The Agent store owns the rename record and the Agent-owned half of a rename: the
Agent tree and config, its Sessions, Sub-Agent parent links, the roster order and
delegation allow-lists (``AgentStore.rename``). Channels, Cron, Bootstrap and
Calendar each hold references to the Agent id that their owners retarget. Runtime
owns every one of these services, so it orders the two halves: the Agent-owned
half first, so references always name an existing Agent, then the references,
then the record is finished. The running Channel, Cron, Bootstrap and Calendar
services keep their state on the Event Loop, so a live rename reads and changes
their references there, while its blocking work runs on a worker.

Every step selects only what still names the id it replaces, so repeating a
direction converges and reversing the ids reverts it. A failure reverts the whole
rename; a process that dies mid-rename leaves the record, and the next start
completes its direction (:func:`complete_pending_rename`). Because a revert moves
everything that names the new id back, a rename is refused while any reference
still names the new id (:func:`identity_agent_references`, ``AgentStore.rename``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from core.agents import Agent, AgentRename, AgentStore
from core.automation import BootstrapService, CronService
from core.automation.bootstrap import TERMINAL_BOOTSTRAP_STATUSES
from core.automation.cron import TERMINAL_CRON_JOB_STATUSES
from core.calendar import CalendarService
from core.channels import ChannelService
from core.database import SnapshotBarrier
from core.sessions import ChatSessionManager
from core.utils.logging import get_logger

_LOGGER = get_logger("runtime.agent_rename")

_Result = TypeVar("_Result")


@dataclass(frozen=True)
class AgentRenameServices:
    """The owners one Identity Agent rename changes."""

    agents: AgentStore
    sessions: ChatSessionManager
    channels: ChannelService
    cron: CronService
    bootstrap: BootstrapService
    calendar: CalendarService
    snapshot_barrier: SnapshotBarrier


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

    Blocking work runs on the Session database's pool; the Channel, Cron, Bootstrap
    and Calendar references are read and changed on this Event Loop, which owns
    those running services. The caller holds the Run admission guards of both ids.
    A failure reverts every change before it is raised. The rename, every
    reference change and any revert form one compound mutation, so a data
    snapshot copies the Sessions and the documents either before or after all of
    it.
    """
    loop = asyncio.get_running_loop()
    return await services.sessions.run_async(_rename, services, agent_id, new_agent_id, loop)


async def identity_agent_references(
    services: AgentRenameServices, agent_id: str
) -> tuple[str, ...]:
    """Name the Channels, jobs and Calendar actions outside the Agent store that address an id.

    This is the selection a rename retargets, labelled ``channel:<id>``,
    ``cron:<id>``, ``bootstrap:<id>`` and ``calendar:<action id>`` and sorted:
    Channels that answer as the Identity Agent, its non-terminal identity Cron and
    Bootstrap jobs, and the actions of live Calendar events that target it. Runs
    on the Event Loop that owns the jobs and actions; the Channel configs are
    read off it.
    """
    references = [
        f"channel:{channel.id}"
        for channel in await services.channels.list_channels_async()
        if channel.agent_id == agent_id
    ]
    references.extend(
        f"cron:{job_id}"
        for job_id in _identity_job_ids(services.cron, agent_id, TERMINAL_CRON_JOB_STATUSES)
    )
    references.extend(
        f"bootstrap:{job_id}"
        for job_id in _identity_job_ids(services.bootstrap, agent_id, TERMINAL_BOOTSTRAP_STATUSES)
    )
    references.extend(
        f"calendar:{action['id']}"
        for action in services.calendar.actions.list_actions()
        if action["target"] == agent_id
    )
    return tuple(sorted(references))


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
    with services.snapshot_barrier.compound_mutation():
        return _rename_and_retarget(services, agent_id, new_agent_id, loop)


def _rename_and_retarget(
    services: AgentRenameServices,
    agent_id: str,
    new_agent_id: str,
    loop: asyncio.AbstractEventLoop,
) -> AgentRenameOutcome:
    result = services.agents.rename(
        agent_id,
        new_agent_id,
        external_references=_on_loop(loop, identity_agent_references(services, new_agent_id)),
    )
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
    Team Agent. Without ``loop`` (startup), no owner has started yet and each
    changes its stored references directly; with it, the changes run on that
    loop, which owns the state of the started owners.
    """
    source, target = rename.source_id, rename.target_id
    if loop is not None:
        return _on_loop(loop, _retarget_on_loop(services, source, target))
    channel_ids = services.channels.retarget_agent(source, target)
    cron_job_ids = _identity_job_ids(services.cron, source, TERMINAL_CRON_JOB_STATUSES)
    for job_id in cron_job_ids:
        services.cron.retarget_agent(job_id, target)
    bootstrap_job_ids = _identity_job_ids(services.bootstrap, source, TERMINAL_BOOTSTRAP_STATUSES)
    for job_id in bootstrap_job_ids:
        services.bootstrap.retarget_agent(job_id, target)
    return _References(
        channel_ids=channel_ids,
        cron_job_ids=cron_job_ids,
        bootstrap_job_ids=bootstrap_job_ids,
        calendar_action_count=services.calendar.actions.retarget_identity(source, target),
    )


async def _retarget_on_loop(services: AgentRenameServices, source: str, target: str) -> _References:
    """:func:`_retarget_references` on the Event Loop of the started owners."""
    # A running Channel service rebuilds each adapter; a stopped one only
    # rewrites the configs and starts none.
    channel_ids = await services.channels.retarget_agent_async(source, target)
    cron_job_ids = _identity_job_ids(services.cron, source, TERMINAL_CRON_JOB_STATUSES)
    for job_id in cron_job_ids:
        await services.cron.retarget_agent_async(job_id, target)
    bootstrap_job_ids = _identity_job_ids(services.bootstrap, source, TERMINAL_BOOTSTRAP_STATUSES)
    for job_id in bootstrap_job_ids:
        # Bootstrap changes its jobs synchronously on the Event Loop.
        services.bootstrap.retarget_agent(job_id, target)
    return _References(
        channel_ids=channel_ids,
        cron_job_ids=cron_job_ids,
        bootstrap_job_ids=bootstrap_job_ids,
        calendar_action_count=await services.calendar.actions.retarget_identity_async(
            source, target
        ),
    )


def _on_loop(loop: asyncio.AbstractEventLoop, step: Coroutine[Any, Any, _Result]) -> _Result:
    """Run ``step`` on ``loop`` from the rename's worker and wait for its result."""
    return asyncio.run_coroutine_threadsafe(step, loop).result()


def _identity_job_ids(
    service: CronService | BootstrapService, agent_id: str, terminal_statuses: frozenset[str]
) -> tuple[str, ...]:
    """Return the ids of the non-terminal jobs that target the Identity Agent ``agent_id``."""
    return tuple(
        job.id
        for job in service.list_jobs()
        if job.agent_id == agent_id
        and job.project_id is None
        and getattr(job, "status", "active") not in terminal_statuses
    )
