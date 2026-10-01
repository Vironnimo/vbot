"""Restoring an archive entry: the read-only check, the restore and its follow-up.

Blocking steps for the Session database's pool. A restore claims the entry
(``restoring``), moves the payload back and makes the Sessions live in one
transaction (``restored``); a failure before that commit moves the files back
and returns the entry to ``archived``. The follow-up (roster, grants, roots,
Sub-Agent links, empty payload directories) is idempotent, runs again from
startup recovery after a crash, and deletes the entry last.
"""

from __future__ import annotations

import builtins
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import Agent, default_workspace_dir
from core.archive._operations import prune_empty_parents, reroot_agents, stored_path
from core.archive._types import (
    RESTORE_CONFLICT_CODES,
    RestoreCheck,
    RestoreOutcome,
    RestoreProblem,
)
from core.archive.errors import ArchiveNotRestorableError, ArchiveRestoreConflictError
from core.projects import Project
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_PROJECT,
    ARCHIVE_TREE_WORKSPACE,
    SESSION_ID_PATTERN,
    ArchiveAddressTakenError,
    ArchiveEntry,
    ArchiveEntryBusyError,
    ArchiveEntryNotFoundError,
    ArchiveMembersManagedError,
    ArchiveTree,
    SessionAddress,
)
from core.utils.logging import get_logger
from core.utils.tree_move import move_tree

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_LOGGER = get_logger("archive")

RESTORABLE_KINDS = frozenset({ARCHIVE_KIND_AGENT, ARCHIVE_KIND_PROJECT, ARCHIVE_KIND_SESSION})

_PAYLOAD_PROBLEMS = {
    "payload_missing": "its files are missing",
    "payload_invalid": "its configuration file cannot be read",
    "older_format": "it was archived in an older format this vBot cannot read",
    "newer_format": "it was archived by a newer vBot",
}


@dataclass(frozen=True)
class _Plan:
    """A checked restore and how it runs."""

    check: RestoreCheck
    entry: ArchiveEntry
    # The payload tree of the Agent or Anchor (``None`` for Sessions).
    source: Path | None = None
    # The Agent's Workspace after the restore (``None`` keeps the archived one) and root.
    workspace: str | None = None
    root_project_id: str | None = None
    # A Workspace an older vBot moved into the archive: its payload tree and its destination.
    workspace_move: tuple[Path, Path] | None = None


@dataclass
class _Findings:
    blockers: builtins.list[RestoreProblem] = field(default_factory=list)
    warnings: builtins.list[RestoreProblem] = field(default_factory=list)

    def block(self, code: str, message: str, **details: Any) -> None:
        self.blockers.append(RestoreProblem(code, message, details))

    def warn(self, code: str, message: str, **details: Any) -> None:
        self.warnings.append(RestoreProblem(code, message, details))


def check(services: ArchiveServices, entry_id: str, target_id: str | None) -> RestoreCheck:
    """Everything a restore of ``entry_id`` under ``target_id`` would meet; changes nothing."""
    return _plan(services, _require_entry(services, entry_id), target_id).check


def restore(services: ArchiveServices, entry_id: str, target_id: str | None) -> RestoreOutcome:
    """Restore one entry, under ``target_id`` when given, and run its follow-up.

    Raises :class:`ArchiveRestoreConflictError` when the target's id or Session
    addresses are taken, :class:`ArchiveEntryBusyError` when another operation
    holds the entry and :class:`ArchiveNotRestorableError` for every other blocker,
    such as member Sessions an Extension manages (``owner_managed``).
    """
    ledger = services.sessions.archive_ledger
    plan = _plan(services, _require_entry(services, entry_id), target_id)
    _raise_blockers(plan)
    entry = plan.entry
    target = plan.check.target_id
    renamed = target != entry.subject_id
    restore_plan: dict[str, Any] = {"target_id": target, "strip_channel_keys": renamed}
    if plan.workspace_move is not None:
        restore_plan["workspace_destination"] = str(plan.workspace_move[1])
    with services.snapshot_barrier.compound_mutation():
        ledger.begin_restore(entry.entry_id, restore_plan)
        try:
            addresses = _move_and_commit(services, plan, target, renamed)
        except ArchiveAddressTakenError as error:
            _abort(services, plan)
            raise ArchiveRestoreConflictError(
                entry.entry_id, (_addresses_taken(error.addresses),)
            ) from error
        except ArchiveMembersManagedError as error:
            _abort(services, plan)
            raise ArchiveNotRestorableError(
                entry.entry_id,
                (
                    RestoreProblem(
                        "owner_managed",
                        "an Extension manages its Sessions; use that Extension to resume them",
                    ),
                ),
            ) from error
        except BaseException:
            _abort(services, plan)
            raise
    restored = ledger.entry_by_key(entry.entry_key)
    if restored is not None:
        follow_up(services, restored)
    return RestoreOutcome(
        entry_id=entry.entry_id,
        kind=entry.kind,
        subject_id=entry.subject_id,
        target_id=target,
        addresses=addresses,
        warnings=plan.check.warnings,
    )


def follow_up(services: ArchiveServices, entry: ArchiveEntry) -> None:
    """Finish a ``restored`` entry, then delete it.

    Roster position, delegation grants, Project roots, Sub-Agent links and
    empty payload directories.
    """
    ledger = services.sessions.archive_ledger
    target = _restore_target(entry)
    renamed = target != entry.subject_id
    facts = entry.facts
    if entry.kind == ARCHIVE_KIND_AGENT:
        roster_index = facts.get("roster_index")
        services.agents.place_in_roster(
            target, roster_index if isinstance(roster_index, int) else None
        )
        grants = [grant for grant in facts.get("grants") or () if isinstance(grant, Mapping)]
        services.agents.restore_delegation_grants(target, grants)
        if renamed:
            _retarget_links(services, None, entry.subject_id, None, target)
    elif entry.kind == ARCHIVE_KIND_PROJECT:
        reroot_agents(services, facts.get("unrooted_agents") or (), target)
        if renamed:
            for agent_id in services.sessions.list_agent_ids(target):
                _retarget_links(services, entry.subject_id, agent_id, target, agent_id)
    for tree in entry.trees:
        path = stored_path(services, tree.path)
        if not path.exists():
            prune_empty_parents(services, path)
    ledger.finish_restore(entry.entry_key)


def roll_back(services: ArchiveServices, entry: ArchiveEntry) -> bool:
    """Return the files of an interrupted restore to the payload and the entry to ``archived``.

    ``False`` when a payload tree could not be returned; the entry then stays
    ``restoring``.
    """
    target = _restore_target(entry)
    live = {
        ARCHIVE_KIND_AGENT: services.data_dir / "agents" / target,
        ARCHIVE_KIND_PROJECT: services.data_dir / "projects" / target,
    }.get(entry.kind)
    tree = _tree(
        entry, ARCHIVE_TREE_AGENT if entry.kind == ARCHIVE_KIND_AGENT else ARCHIVE_TREE_PROJECT
    )
    payload = None if tree is None else stored_path(services, tree.path)
    if payload is not None and live is not None and not payload.exists() and live.is_dir():
        move_tree(live, payload)
    workspace_tree = _tree(entry, ARCHIVE_TREE_WORKSPACE)
    destination = entry.facts.get("restore_plan", {}).get("workspace_destination")
    if workspace_tree is not None and isinstance(destination, str):
        moved = stored_path(services, workspace_tree.path)
        if not moved.exists() and Path(destination).is_dir():
            move_tree(Path(destination), moved)
    if payload is not None and not payload.exists():
        return False
    services.sessions.archive_ledger.abort_restore(entry.entry_key)
    return True


# -- Check -------------------------------------------------------------------------


def _require_entry(services: ArchiveServices, entry_id: str) -> ArchiveEntry:
    entry = services.sessions.archive_ledger.entry(entry_id)
    if entry is None:
        raise ArchiveEntryNotFoundError(entry_id)
    return entry


def _plan(services: ArchiveServices, entry: ArchiveEntry, target_id: str | None) -> _Plan:
    target = entry.subject_id if target_id is None else target_id
    findings = _Findings()
    if entry.kind not in RESTORABLE_KINDS:
        findings.block(
            "kind_not_restorable", f"an entry of kind {entry.kind} can only be deleted permanently"
        )
        return _Plan(_checked(entry, target, findings), entry)
    if entry.state != ARCHIVE_STATE_ARCHIVED:
        findings.block("entry_busy", f"the entry is {entry.state}", state=entry.state)
    if entry.kind == ARCHIVE_KIND_AGENT:
        return _plan_agent(services, entry, target, findings)
    if entry.kind == ARCHIVE_KIND_PROJECT:
        return _plan_project(services, entry, target, findings)
    _check_session(services, entry, target, findings)
    return _Plan(_checked(entry, target, findings), entry)


def _checked(entry: ArchiveEntry, target: str, findings: _Findings) -> RestoreCheck:
    return RestoreCheck(
        entry_id=entry.entry_id,
        kind=entry.kind,
        target_id=target,
        blockers=tuple(findings.blockers),
        warnings=tuple(findings.warnings),
    )


def _payload_source(
    services: ArchiveServices, entry: ArchiveEntry, role: str, findings: _Findings
) -> Path | None:
    tree = _tree(entry, role)
    source = None if tree is None else stored_path(services, tree.path)
    if source is None or not source.is_dir():
        findings.block(
            "payload_missing",
            _PAYLOAD_PROBLEMS["payload_missing"],
            path=None if tree is None else tree.path,
            source_path=None if tree is None else tree.source_path,
        )
        return None
    return source


def _payload_problem(code: str, findings: _Findings) -> None:
    findings.block(code, _PAYLOAD_PROBLEMS.get(code, code))


def _check_addresses(
    services: ArchiveServices, entry: ArchiveEntry, findings: _Findings, **replacement: str
) -> None:
    taken = services.sessions.archive_ledger.taken_addresses(entry.entry_key, **replacement)
    if taken:
        findings.blockers.append(_addresses_taken(taken))


def _addresses_taken(addresses: Sequence[SessionAddress]) -> RestoreProblem:
    return RestoreProblem(
        "session_address_taken",
        "live Sessions already use "
        + ", ".join(sorted({address.session_id for address in addresses})),
        {
            "addresses": [
                {
                    "project_id": address.project_id,
                    "agent_id": address.agent_id,
                    "session_id": address.session_id,
                }
                for address in addresses
            ]
        },
    )


def _plan_agent(
    services: ArchiveServices, entry: ArchiveEntry, target: str, findings: _Findings
) -> _Plan:
    agent: Agent | None = None
    source = _payload_source(services, entry, ARCHIVE_TREE_AGENT, findings)
    if source is not None:
        inspected = services.agents.inspect_archived(source)
        if inspected.problem is not None:
            _payload_problem(inspected.problem, findings)
        agent = inspected.agent
    target_problem = services.agents.restore_target_problem(target)
    if target_problem == "invalid_target_id":
        findings.block("invalid_target_id", f"{target} is not a valid Agent id")
    elif target_problem is not None:
        findings.block("agent_id_taken", f"an Agent with id {target} exists", agent_id=target)
    else:
        renamed = target != entry.subject_id
        _check_addresses(services, entry, findings, **({"agent_id": target} if renamed else {}))
    for grant in entry.facts.get("grants") or ():
        holder = grant.get("agent_id") if isinstance(grant, Mapping) else None
        if isinstance(holder, str) and holder != target and not services.agents.exists(holder):
            findings.warn(
                "grant_target_missing",
                f"Agent {holder} no longer exists; its delegation grant is not restored",
                agent_id=holder,
            )
    if agent is None or source is None:
        return _Plan(_checked(entry, target, findings), entry, source)
    root = agent.root_project_id
    if root is not None and not services.projects.exists(root):
        findings.warn(
            "root_project_missing",
            f"Project {root} does not exist; the Agent is restored without it",
            project_id=root,
        )
        root = None
    workspace, workspace_move = _plan_workspace(services, entry, source, agent, target, findings)
    return _Plan(
        _checked(entry, target, findings),
        entry,
        source,
        workspace=workspace,
        root_project_id=root,
        workspace_move=workspace_move,
    )


def _plan_workspace(
    services: ArchiveServices,
    entry: ArchiveEntry,
    source: Path,
    agent: Agent,
    target: str,
    findings: _Findings,
) -> tuple[str | None, tuple[Path, Path] | None]:
    """Where the restored Agent's Workspace lives.

    A Workspace inside the Agent's directory travels with it. An external one
    stays where it was and is re-attached, or the default Workspace replaces a
    vanished one. A Workspace an older vBot moved into the archive returns to
    its folder, or becomes the default Workspace when that folder is in use.
    """
    default = str(default_workspace_dir(services.data_dir, target).resolve())
    moved_tree = _tree(entry, ARCHIVE_TREE_WORKSPACE)
    if moved_tree is not None and stored_path(services, moved_tree.path).is_dir():
        moved = stored_path(services, moved_tree.path)
        folder = (
            None
            if moved_tree.source_path is None
            else stored_path(services, moved_tree.source_path)
        )
        if folder is not None and not os.path.lexists(folder):
            return None, (moved, folder)
        if not os.path.lexists(source / "workspace"):
            findings.warn(
                "workspace_path_taken",
                f"the Workspace folder {folder} is in use; the archived Workspace becomes "
                "the default Workspace",
                path=None if folder is None else str(folder),
            )
            return default, (moved, source / "workspace")
        findings.block(
            "workspace_path_taken",
            f"the archived Workspace returns to {folder}, which is in use, and so is the "
            f"Agent's default Workspace; move or rename {folder}, then restore again",
            path=None if folder is None else str(folder),
            archived_workspace=str(moved),
        )
        return None, None
    workspace = Path(agent.workspace)
    home = services.data_dir / "agents" / agent.id
    if not workspace.resolve().is_relative_to(home.resolve()) and not workspace.is_dir():
        findings.warn(
            "external_workspace_missing",
            f"the Workspace folder {workspace} is gone; the Agent uses its default Workspace",
            path=str(workspace),
        )
        return default, None
    return None, None


def _plan_project(
    services: ArchiveServices, entry: ArchiveEntry, target: str, findings: _Findings
) -> _Plan:
    project: Project | None = None
    source = _payload_source(services, entry, ARCHIVE_TREE_PROJECT, findings)
    if source is not None:
        inspected = services.projects.inspect_archived(source)
        if inspected.problem is not None:
            _payload_problem(inspected.problem, findings)
        project = inspected.project
    target_problem = services.projects.restore_target_problem(target)
    if target_problem == "invalid_target_id":
        findings.block("invalid_target_id", f"{target} is not a valid Project id")
    elif target_problem is not None:
        findings.block("project_id_taken", f"a Project with id {target} exists", project_id=target)
    else:
        renamed = target != entry.subject_id
        _check_addresses(services, entry, findings, **({"project_id": target} if renamed else {}))
    if project is not None:
        owner = services.projects.find_by_cwd(project.cwd)
        if owner is not None and owner.project_id != target:
            findings.block(
                "project_cwd_claimed",
                f"Project {owner.project_id} already uses the folder {project.cwd}; "
                "remove that Project first",
                project_id=owner.project_id,
                cwd=project.cwd,
            )
    return _Plan(_checked(entry, target, findings), entry, source)


def _check_session(
    services: ArchiveServices, entry: ArchiveEntry, target: str, findings: _Findings
) -> None:
    ledger = services.sessions.archive_ledger
    renamed = target != entry.subject_id
    if renamed and entry.session_count != 1:
        findings.block(
            "invalid_target_id",
            "the entry holds several Sessions; restore it without a new Session id",
        )
    elif renamed and SESSION_ID_PATTERN.fullmatch(target) is None:
        findings.block("invalid_target_id", f"{target} is not a valid Session id")
    else:
        _check_addresses(services, entry, findings, **({"session_id": target} if renamed else {}))
    if entry.project_id:
        if not services.projects.exists(entry.project_id):
            findings.block(
                "scope_missing",
                f"Project {entry.project_id} does not exist; restore it first",
                project_id=entry.project_id,
                entry_id=ledger.newest_entry_id(ARCHIVE_KIND_PROJECT, entry.project_id),
            )
            return
        try:
            services.agent_resolver.resolve_agent(entry.project_id, entry.agent_id)
        except Exception:
            findings.block(
                "scope_missing",
                f"Agent {entry.agent_id} is not in the Team of Project {entry.project_id}",
                project_id=entry.project_id,
                agent_id=entry.agent_id,
            )
    elif not services.agents.exists(entry.agent_id):
        findings.block(
            "scope_missing",
            f"Agent {entry.agent_id} does not exist; restore it first",
            agent_id=entry.agent_id,
            entry_id=ledger.newest_entry_id(ARCHIVE_KIND_AGENT, entry.agent_id),
        )


def _raise_blockers(plan: _Plan) -> None:
    blockers = plan.check.blockers
    if not blockers:
        return
    busy = next((blocker for blocker in blockers if blocker.code == "entry_busy"), None)
    if busy is not None:
        raise ArchiveEntryBusyError(plan.entry.entry_id, plan.entry.state)
    if all(blocker.code in RESTORE_CONFLICT_CODES for blocker in blockers):
        raise ArchiveRestoreConflictError(plan.entry.entry_id, blockers)
    raise ArchiveNotRestorableError(plan.entry.entry_id, blockers)


# -- Restore -----------------------------------------------------------------------


def _move_and_commit(
    services: ArchiveServices, plan: _Plan, target: str, renamed: bool
) -> tuple[SessionAddress, ...]:
    ledger = services.sessions.archive_ledger
    entry = plan.entry
    if entry.kind == ARCHIVE_KIND_SESSION:
        return ledger.commit_restore(
            entry.entry_key,
            session_id=target if renamed else None,
            strip_channel_keys=renamed,
        )
    assert plan.source is not None
    if entry.kind == ARCHIVE_KIND_PROJECT:
        with services.projects.restore_files(plan.source, target):
            return ledger.commit_restore(
                entry.entry_key,
                project_id=target if renamed else None,
                strip_channel_keys=renamed,
            )
    if plan.workspace_move is not None:
        move_tree(*plan.workspace_move)
    try:
        with services.agents.restore_files(
            plan.source,
            target,
            workspace=plan.workspace,
            root_project_id=plan.root_project_id,
        ):
            return ledger.commit_restore(
                entry.entry_key,
                agent_id=target if renamed else None,
                strip_channel_keys=renamed,
            )
    except BaseException:
        if plan.workspace_move is not None:
            moved, destination = plan.workspace_move
            if destination.is_dir() and not moved.exists():
                move_tree(destination, moved)
        raise


def _abort(services: ArchiveServices, plan: _Plan) -> None:
    """Return a failed restore's entry to ``archived`` once its files are back in the payload."""
    if plan.source is not None and not plan.source.is_dir():
        _LOGGER.warning(
            "Restore left for recovery; its files did not go back (entry=%s)", plan.entry.entry_id
        )
        return
    try:
        services.sessions.archive_ledger.abort_restore(plan.entry.entry_key)
    except Exception:
        _LOGGER.exception(
            "Could not reopen the entry of a failed restore (entry=%s)", plan.entry.entry_id
        )


def _retarget_links(
    services: ArchiveServices,
    old_project_id: str | None,
    old_agent_id: str,
    new_project_id: str | None,
    new_agent_id: str,
) -> None:
    session_ids = [
        address.session_id
        for address in services.sessions.list_addresses(new_project_id, agent_id=new_agent_id)
    ]
    services.sessions.archive_ledger.retarget_subagent_links(
        old_project_id=old_project_id,
        old_agent_id=old_agent_id,
        new_project_id=new_project_id,
        new_agent_id=new_agent_id,
        session_ids=session_ids,
    )


def _restore_target(entry: ArchiveEntry) -> str:
    plan = entry.facts.get("restore_plan")
    target = plan.get("target_id") if isinstance(plan, Mapping) else None
    return target if isinstance(target, str) and target else entry.subject_id


def _tree(entry: ArchiveEntry, role: str) -> ArchiveTree | None:
    return next((tree for tree in entry.trees if tree.role == role), None)
