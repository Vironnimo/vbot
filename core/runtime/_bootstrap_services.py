"""Compose resource and automation services inside the Runtime owner."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from core.agents.agents import AgentStore
from core.archive import ArchiveRetentionUnknownError, ArchiveServices
from core.automation import LibrarianService
from core.performance import PerformanceService
from core.runs import ChatRunManager
from core.sessions import SessionAddress
from core.sessions.errors import SessionNotFoundError
from core.statistics import StatisticsService
from core.storage.errors import StorageError
from core.storage.storage import StorageManager
from core.subagents.links import ancestors
from core.tools.scratch_areas import ScratchAreas
from core.tools.terminal_manager import (
    TerminalManager,
    TerminalOwner,
    TerminalSessionView,
    observe_process_tree,
    resolve_root_view,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.runtime.runtime import Runtime


def _agent_names(agents: AgentStore) -> list[str]:
    """The ids and names of the Agents, which Terminals are not called by."""
    return [name for agent in agents.list() for name in (agent.id, agent.name)]


def _terminal_session_view(sessions: Any, scope: TerminalOwner) -> TerminalSessionView:
    """Name the conversation a Terminal's Session belongs to: the root Session
    of its Sub-Agent tree, with that Session's title."""

    def address(owner: TerminalOwner) -> SessionAddress:
        return SessionAddress(owner.project_id, owner.agent_id, owner.session_id)

    def parent(owner: TerminalOwner) -> TerminalOwner | None:
        value = sessions.metadata_value(address(owner), "subagent_parent")
        if not isinstance(value, dict):
            return None
        agent_id, session_id = value.get("agent_id"), value.get("session_id")
        if not isinstance(agent_id, str) or not isinstance(session_id, str):
            return None
        project_id = value.get("project_id")
        return TerminalOwner(
            project_id=project_id if isinstance(project_id, str) and project_id else None,
            agent_id=agent_id,
            session_id=session_id,
        )

    def title(owner: TerminalOwner) -> str | None:
        for key in ("title", "auto_title"):
            value = sessions.metadata_value(address(owner), key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    return resolve_root_view(scope, parent, title)


def _agent_default_project(agents: AgentStore, agent_id: str) -> str | None:
    """Return the default Project of an Identity Agent, ``None`` for none or no Agent."""
    agent = agents.find(agent_id)
    return None if agent is None else agent.root_project_id


async def _goal_work_pending(runtime: Runtime, address: SessionAddress) -> bool:
    """Whether work runs whose result wakes the Session: a Sub-Agent or a delivering command."""
    sessions = runtime._chat_sessions
    runs = runtime._chat_run_manager
    if sessions is None or runs is None:
        return False
    children = await sessions.run_async(sessions.subagent_children, address)
    if any(
        runs.has_activity_for_session(child.agent_id, child.session_id, project_id=child.project_id)
        for child in children
    ):
        return True
    commands = runtime._command_manager
    return commands is not None and commands.has_pending_delivery(address)


def _librarian_service(runtime: Runtime) -> LibrarianService:
    """The Librarian over the private Skill homes, Skill use and live automations."""
    authoring = runtime._skill_authoring
    automation = runtime._automation_references
    sessions = runtime._chat_sessions
    agents = runtime._agents
    if authoring is None or automation is None or sessions is None or agents is None:
        raise RuntimeError("Librarian services are not available")
    # The Agent and Project stores satisfy the directory protocols structurally.
    usage = StatisticsService(
        sessions,
        cast(Any, agents),
        cast(Any, runtime._projects),
        index=runtime._statistics_index,
        usage_recorder=runtime._usage_recorder,
    )

    def skills_changed(agent_id: str) -> None:
        runtime.invalidate_agent_skills(agent_id)
        runtime._notify_skills_changed()

    return LibrarianService(
        runtime,
        authoring=authoring,
        skills_dir=runtime.agent_skills_dir,
        skill_usage=usage.skill_package_usage_async,
        triggered_skill_names=automation.agent_triggered_skill_names,
        shared_skill_receivers=runtime.shared_skill_receivers,
        skills_changed=skills_changed,
        # The Skill manager shows the Librarian; its observers reload on Skill changes.
        status_changed=runtime._notify_skills_changed,
        reviewing=lambda agent_id: (
            runtime._reflection_service is not None
            and runtime._reflection_service.reviewing(agent_id)
        ),
    )


def _archive_services(runtime: Runtime) -> ArchiveServices:
    """The owners archive operations compose; startup builds them before this."""
    storage = runtime._storage
    sessions = runtime._chat_sessions
    agents = runtime._agents
    projects = runtime._projects
    resolver = runtime._agent_resolver
    runs = runtime._chat_run_manager
    automation = runtime._automation_references
    terminals = runtime._terminal_manager
    commands = runtime._command_manager
    scratch_areas = runtime._scratch_areas
    if (
        storage is None
        or sessions is None
        or agents is None
        or projects is None
        or resolver is None
        or runs is None
        or automation is None
        or terminals is None
        or commands is None
        or scratch_areas is None
    ):
        raise RuntimeError("Archive services are not available")

    def import_usage() -> None:
        # A purge deletes Sessions only after their usage reached the usage ledger.
        if runtime._usage_recorder is None:
            raise RuntimeError("the usage ledger is not available")
        runtime._usage_recorder.import_session_history(sessions)

    async def remove_agent_from_recall(agent_id: str) -> None:
        await runtime.recall.remove_agent_from_recall(agent_id)

    def invalidate_project(project_id: str) -> None:
        resolver.invalidate_team_cache(project_id)
        runtime.invalidate_project_skills(project_id)

    async def remove_session_secrets(addresses: Sequence[SessionAddress]) -> None:
        secrets = runtime._agent_secrets
        if secrets is None:
            return
        for address in addresses:
            await asyncio.to_thread(secrets.remove_session, address.session_id)

    async def close_session_transports(addresses: Sequence[SessionAddress]) -> None:
        await runtime.close_session_transports(
            (address.agent_id, address.session_id) for address in addresses
        )

    def retention_days() -> int | None:
        # Strict: a degraded settings file must never widen retention to the default.
        try:
            days: int | None = storage.load_archive_settings(strict=True)["retention_days"]
        except StorageError as error:
            raise ArchiveRetentionUnknownError(str(error)) from error
        return days

    return ArchiveServices(
        data_dir=storage.data_dir,
        sessions=sessions,
        agents=agents,
        projects=projects,
        agent_resolver=resolver,
        runs=runs,
        automation=automation,
        terminals=terminals,
        commands=commands,
        scratch_areas=scratch_areas,
        snapshot_barrier=runtime._snapshot_barrier,
        agent_references=runtime.agent_references,
        import_usage=import_usage,
        remove_agent_from_recall=remove_agent_from_recall,
        remove_session_from_recall=runtime.remove_session_from_recall,
        invalidate_agent_skills=runtime.invalidate_agent_skills,
        invalidate_project=invalidate_project,
        retention_days=retention_days,
        close_session_transports=close_session_transports,
        remove_session_secrets=remove_session_secrets,
    )


def _build_scratch_areas(runtime: Runtime) -> ScratchAreas:
    """Compose live resource activity without making the Tools owner depend on Runtime."""
    storage = runtime._storage
    sessions = runtime._chat_sessions
    runs = runtime._chat_run_manager
    if storage is None or sessions is None or runs is None:
        raise RuntimeError("Scratch Area dependencies are not available")

    def identity(address: SessionAddress) -> str | None:
        try:
            return sessions.get(address).generation_id
        except SessionNotFoundError:
            return None

    def busy_sessions() -> set[SessionAddress]:
        busy = {
            SessionAddress(run.project_id, run.agent_id, run.session_id)
            for run in runs.active_runs()
        }
        busy.update(address for address, _item in runs.all_queued())
        if runtime._command_manager is not None:
            busy.update(runtime._command_manager.busy_addresses())
        if runtime._terminal_manager is not None:
            busy.update(runtime._terminal_manager.busy_addresses())
        return busy

    scratch = ScratchAreas(
        storage.data_dir,
        identity=identity,
        ancestors=partial(ancestors, sessions, strict=True),
        busy_sessions=busy_sessions,
    )
    runs.add_run_started_callback(
        lambda run: scratch.record_run_start(
            SessionAddress(run.project_id, run.agent_id, run.session_id)
        )
    )
    return scratch


def _build_performance_service(
    storage: StorageManager, run_manager: ChatRunManager
) -> PerformanceService:
    """Build the process measurement owner with the Run gauges it samples."""
    return PerformanceService(
        storage.layout.performance,
        samplers={
            "runs.active": lambda: len(run_manager.active_runs()),
            "runs.queued": lambda: len(run_manager.all_queued()),
        },
    )


def _build_terminal_manager(runtime: Runtime) -> TerminalManager:
    """Wire Terminal ownership, views and title invalidation together."""
    agents, storage, sessions = runtime._agents, runtime._storage, runtime._chat_sessions
    assert agents is not None and storage is not None and sessions is not None
    terminal_manager = TerminalManager(
        runtime._trigger_service,
        temporary_files=storage.temporary_files,
        launch_history_path=storage.layout.terminals / "launch-history.json",
        groups_path=storage.layout.terminals / "groups.json",
        data_dir=storage.data_dir,
        process_observer=observe_process_tree,
        session_view=partial(_terminal_session_view, sessions),
        reserved_call_names=partial(_agent_names, agents),
    )
    sessions.add_title_changed_callback(lambda _address: terminal_manager.forget_session_views())
    return terminal_manager
