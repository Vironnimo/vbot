"""Archiving an Identity Agent or a Project into one archive entry.

Blocking steps for the Session database's pool. Each archive records the entry
first (``archiving``, with the payload tree it will create), moves the files,
then archives the Sessions and marks the entry ``archived`` in one transaction.
A failure before that commit moves the files back and deletes the entry; when a
compensation step fails, the entry stays ``archiving`` for startup recovery. Once
that commit succeeds, the archive has succeeded: a failed cleanup afterwards is
logged and left ``cleanup_pending`` for startup recovery.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import AGENT_FORMAT_VERSION, Agent, AgentError, AgentUpdateResult, ArchivedAgent
from core.archive._types import AgentArchiveOutcome, ProjectArchiveOutcome
from core.projects import PROJECT_FORMAT_VERSION, ProjectError
from core.sessions import (
    ARCHIVE_ENTRIES_DIR,
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_ROOT,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_PROJECT,
    ArchiveEntry,
    ArchiveEntryFilter,
    ArchiveEntryRef,
    ArchiveScope,
    ArchiveTree,
)
from core.utils.file_status import exists_strict
from core.utils.logging import get_logger
from core.utils.timestamps import canonical_timestamp

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_LOGGER = get_logger("archive")


def payload_path(entry_id: str, name: str) -> str:
    """The data-dir relative payload tree ``name`` of a new entry."""
    return f"{ARCHIVE_ENTRIES_DIR}/{entry_id}/{name}"


def stored_path(services: ArchiveServices, path: str) -> Path:
    """Resolve a recorded path: data-dir relative POSIX, or absolute."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else services.data_dir / candidate


def prune_empty_parents(services: ArchiveServices, path: Path) -> None:
    """Remove the empty directories from ``path``'s parent up to, not including, ``archive/``."""
    root = (services.data_dir / ARCHIVE_ROOT).resolve()
    current = path.parent
    while current.resolve() != root and current.resolve().is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def archive_agent(services: ArchiveServices, agent_id: str) -> AgentArchiveOutcome:
    """Move an Identity Agent and its live global Sessions into a new entry."""
    ledger = services.sessions.archive_ledger
    with services.snapshot_barrier.compound_mutation():
        ref = ledger.begin(
            ARCHIVE_KIND_AGENT,
            subject_id=agent_id,
            agent_id=agent_id,
            trees=lambda entry_id: (
                ArchiveTree(
                    payload_path(entry_id, "agent"), ARCHIVE_TREE_AGENT, f"agents/{agent_id}"
                ),
            ),
        )
        tree = services.data_dir / payload_path(ref.entry_id, "agent")
        try:
            with services.agents.archive_files(agent_id, tree) as archived:
                external_workspace = (
                    archived.agent.workspace if archived.workspace_external else None
                )
                session_count = ledger.commit_scope(
                    ref.entry_key,
                    ArchiveScope(agent_id=agent_id),
                    facts=_agent_facts(archived),
                    cleanup_pending=True,
                )
        except BaseException:
            _abandon_unless_retained(services, ref, (tree,))
            raise
    policy_agent_ids: tuple[str, ...] = ()
    try:
        entry = ledger.entry_by_key(ref.entry_key)
        if entry is not None:
            policy_agent_ids = cleanup_agent(services, entry)
    except Exception:
        _LOGGER.exception(
            "Archive cleanup failed; it completes at the next start (entry=%s)", ref.entry_id
        )
    return AgentArchiveOutcome(
        ref.entry_id, agent_id, session_count, policy_agent_ids, external_workspace
    )


def holds_agent_id_grants(services: ArchiveServices, entry: ArchiveEntry) -> bool:
    """Whether the Agent Secret grants naming the archived Agent's id are its own.

    Grants name only an id, and a new Agent is never created under an id they
    name; so they are the archived Agent's unless a live Agent has its id again
    or another entry archived an Agent of that id, which keep them.
    """
    agent_id = entry.subject_id
    if services.agents.exists(agent_id):
        return False
    others = services.sessions.archive_ledger.page(
        ArchiveEntryFilter(kind=ARCHIVE_KIND_AGENT, agent_id=agent_id), limit=2
    )
    return all(other.entry_id == entry.entry_id for other in others.entries)


def _repair_review_currents(services: ArchiveServices, entry_key: int, scope: ArchiveScope) -> None:
    """Re-aim surviving review owners before grant/roster reads repair their pointers."""
    for member in services.sessions.archive_ledger.members(entry_key, limit=None):
        address = member.address
        if (
            address.project_id is None
            and (scope.project_id is not None or address.agent_id != scope.agent_id)
            and services.agents.exists(address.agent_id)
        ):
            services.agents.reset_current_after_session_removed(
                address.agent_id, address.session_id
            )


def _agent_facts(archived: ArchivedAgent) -> dict[str, Any]:
    return {
        "name": archived.agent.name,
        "payload_format": AGENT_FORMAT_VERSION,
        "roster_index": archived.roster_index,
        "workspace": {
            "path": archived.workspace,
            "external": archived.workspace_external,
            "moved": False,
        },
        "root_project_id": archived.agent.root_project_id,
    }


def cleanup_agent(services: ArchiveServices, entry: ArchiveEntry) -> tuple[str, ...]:
    """Finish an archived Agent: its id leaves delegation lists and the roster.

    The removed grants are recorded in the entry before any config changes, and
    merged with grants an interrupted earlier run recorded, so a restore can add
    every one back. Repeating it is harmless. When a live Agent has meanwhile
    taken the id, the delegation lists naming it are that Agent's grants: the
    cleanup finishes without touching or recording them.
    """
    ledger = services.sessions.archive_ledger
    _repair_review_currents(services, entry.entry_key, ArchiveScope(agent_id=entry.agent_id))
    if services.agents.exists(entry.subject_id):
        ledger.finish_cleanup(entry.entry_key)
        return ()
    recorded = [grant for grant in entry.facts.get("grants") or () if isinstance(grant, Mapping)]

    def record(grants: Iterable[Mapping[str, Any]]) -> None:
        known = {grant.get("agent_id") for grant in recorded}
        merged = [*recorded, *(grant for grant in grants if grant.get("agent_id") not in known)]
        ledger.update_facts(entry.entry_key, {"grants": merged})

    policy_agent_ids = services.agents.remove_delegation_grants(entry.subject_id, record)
    try:
        # Reading the roster drops the archived id from the stored order.
        services.agents.list_with_order()
    except (AgentError, OSError) as error:
        _LOGGER.warning(
            "Could not update the Agent roster after an archive (entry=%s): %s",
            entry.entry_id,
            error,
        )
    ledger.finish_cleanup(entry.entry_key)
    return policy_agent_ids


def archive_project(
    services: ArchiveServices, project_id: str, *, copy_identity_files: bool
) -> ProjectArchiveOutcome:
    """Unroot the Project's Identity Agents, then move its Anchor and Sessions into a new entry.

    Each unroot is recorded in the entry before it happens, so a restore (or
    recovery of an interrupted archive) can root the Agent again.
    """
    ledger = services.sessions.archive_ledger
    agents = services.agents
    with services.snapshot_barrier.compound_mutation():
        project = services.projects.get(project_id)
        ref = ledger.begin(
            ARCHIVE_KIND_PROJECT,
            subject_id=project_id,
            project_id=project_id,
            facts={
                "name": project.display_name,
                "cwd": project.cwd,
                "payload_format": PROJECT_FORMAT_VERSION,
            },
            trees=lambda entry_id: (
                ArchiveTree(
                    payload_path(entry_id, "project"),
                    ARCHIVE_TREE_PROJECT,
                    f"projects/{project_id}",
                ),
            ),
        )
        tree = services.data_dir / payload_path(ref.entry_id, "project")
        unrooted: list[dict[str, Any]] = []
        completed: list[tuple[Agent, AgentUpdateResult]] = []
        copied_files: dict[str, tuple[str, ...]] = {}
        backed_up_files: dict[str, tuple[str, ...]] = {}
        try:
            for agent in agents.agents_rooted_in(project_id):
                default_workspace = agents.default_workspace(agent.id)
                workspace_reset = agent.workspace != default_workspace
                unrooted.append(
                    {
                        "agent_id": agent.id,
                        "agent_created_at": agent.created_at,
                        "workspace_before": agent.workspace,
                        "workspace_reset": workspace_reset,
                    }
                )
                ledger.update_facts(ref.entry_key, {"unrooted_agents": unrooted})
                changes: dict[str, Any] = {"root_project_id": None}
                if workspace_reset:
                    changes["workspace"] = default_workspace
                result = agents.update_with_metadata(
                    agent.id,
                    copy_workspace_identity_files=copy_identity_files and workspace_reset,
                    **changes,
                )
                completed.append((agent, result))
                copied_files[agent.id] = tuple(result.copied_files)
                backed_up_files[agent.id] = tuple(result.backed_up_files)
            with services.projects.archive_files(project_id, tree):
                session_count = ledger.commit_scope(
                    ref.entry_key, ArchiveScope(project_id=project_id)
                )
        except BaseException:
            if _undo_unroots(services, completed):
                _abandon_unless_retained(services, ref, (tree,))
            raise
        _repair_review_currents(services, ref.entry_key, ArchiveScope(project_id=project_id))
    return ProjectArchiveOutcome(
        entry_id=ref.entry_id,
        project_id=project_id,
        session_count=session_count,
        affected_agent_ids=tuple(agent.id for agent, _result in completed),
        copied_files=copied_files,
        backed_up_files=backed_up_files,
    )


def _undo_unroots(
    services: ArchiveServices, completed: Sequence[tuple[Agent, AgentUpdateResult]]
) -> bool:
    """Compensate the unroots of a failed Project archive; ``False`` when one could not."""
    undone = True
    for previous, result in reversed(completed):
        try:
            services.agents.restore_update(previous, result)
        except Exception:
            _LOGGER.exception(
                "Could not root an Agent again after a failed Project archive (agent=%s)",
                previous.id,
            )
            undone = False
    return undone


def reroot_agents(
    services: ArchiveServices, records: Iterable[Any], project_id: str, archived_at: str
) -> builtins.list[str]:
    """Root recorded unrooted Agents in ``project_id`` again; return the Agents changed.

    An Agent that is gone, was replaced under the same id, or is meanwhile
    rooted elsewhere stays as it is. Its
    previous Workspace returns when the unroot reset it to the default, the Agent
    still uses that default and the previous Workspace still exists. Best effort.
    """
    rerooted: builtins.list[str] = []
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("agent_id"), str):
            continue
        agent_id = str(record["agent_id"])
        agent = services.agents.find(agent_id)
        if agent is None or agent.root_project_id is not None:
            continue
        recorded_creation = record.get("agent_created_at")
        if recorded_creation is not None:
            if agent.created_at != recorded_creation:
                continue
        else:
            # Entries without an identity stamp can still exclude an Agent born
            # after the Project was archived. Never guess an unreadable birth time.
            try:
                if canonical_timestamp(agent.created_at) > canonical_timestamp(archived_at):
                    continue
            except ValueError:
                _LOGGER.warning("Cannot identify an Agent for Project restore (agent=%s)", agent_id)
                continue
        changes: dict[str, Any] = {"root_project_id": project_id}
        before = record.get("workspace_before")
        if (
            record.get("workspace_reset") is True
            and isinstance(before, str)
            and agent.workspace == services.agents.default_workspace(agent_id)
            and Path(before).is_dir()
        ):
            changes["workspace"] = before
        try:
            services.agents.update_with_metadata(agent_id, **changes)
        except (AgentError, ProjectError, OSError) as error:
            _LOGGER.warning(
                "Could not root an Agent in its Project again (agent=%s project=%s): %s",
                agent_id,
                project_id,
                error,
            )
            continue
        rerooted.append(agent_id)
    return rerooted


def _abandon_unless_retained(
    services: ArchiveServices, ref: ArchiveEntryRef, trees: Sequence[Path]
) -> None:
    """Delete the entry of a failed archive whose files went back.

    When a payload tree remains or cannot be checked, keep its ``archiving``
    entry so startup recovery can return it. Never mask the archive failure.
    """
    try:
        retained = any(exists_strict(tree, follow_symlinks=False) for tree in trees)
    except OSError as error:
        _LOGGER.warning(
            "Archive left for recovery; its files could not be checked (entry=%s error=%s)",
            ref.entry_id,
            type(error).__name__,
        )
        return
    if retained:
        _LOGGER.warning(
            "Archive left for recovery; its files did not go back (entry=%s)", ref.entry_id
        )
        return
    try:
        services.sessions.archive_ledger.abandon(ref.entry_key)
    except Exception:
        _LOGGER.exception("Could not delete the entry of a failed archive (entry=%s)", ref.entry_id)
        return
    with suppress(OSError):
        (services.data_dir / ARCHIVE_ENTRIES_DIR / ref.entry_id).rmdir()
