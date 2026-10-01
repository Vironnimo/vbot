"""Startup recovery: finish or undo what an interrupted archive operation left behind.

It runs before Channels, Cron and Runs start, so nothing else changes Agents,
Projects or archive entries meanwhile. Every step is idempotent; an entry whose
recovery fails is logged and tried again at the next start.
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.agents import AGENT_FORMAT_VERSION
from core.archive import _restore
from core.archive._operations import cleanup_agent, prune_empty_parents, reroot_agents, stored_path
from core.projects import PROJECT_FORMAT_VERSION
from core.sessions import (
    ARCHIVE_ENTRIES_DIR,
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_FILES,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_STATE_ARCHIVING,
    ARCHIVE_STATE_RESTORED,
    ARCHIVE_STATE_RESTORING,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_FILES,
    ARCHIVE_TREE_PROJECT,
    ArchiveEntry,
    ArchiveTree,
)
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp
from core.utils.tree_move import move_tree

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_LOGGER = get_logger("archive")


def recover(services: ArchiveServices) -> bool:
    """Bring the archive ledger and its payloads back in step; ``True`` when anything changed."""
    ledger = services.sessions.archive_ledger
    changed = False
    adoption = ledger.adopt_unrecorded_archived_rows()
    if adoption.sessions:
        _LOGGER.warning(
            "Archived Sessions without an archive entry adopted (sessions=%d entries=%d)",
            adoption.sessions,
            adoption.entries,
        )
        changed = True
    changed = _adopt_orphan_payloads(services) or changed
    for entry in ledger.unsettled():
        try:
            changed = _settle(services, entry) or changed
        except Exception:
            _LOGGER.exception(
                "Archive entry recovery failed; it is retried at the next start (entry=%s)",
                entry.entry_id,
            )
    return changed


def _settle(services: ArchiveServices, entry: ArchiveEntry) -> bool:
    if entry.state == ARCHIVE_STATE_ARCHIVING:
        return _roll_back_archive(services, entry)
    if entry.state == ARCHIVE_STATE_ARCHIVED and entry.cleanup_pending:
        if entry.kind == ARCHIVE_KIND_AGENT:
            cleanup_agent(services, entry)
        else:
            services.sessions.archive_ledger.finish_cleanup(entry.entry_key)
        _LOGGER.info("Interrupted archive completed (entry=%s kind=%s)", entry.entry_id, entry.kind)
        return True
    if entry.state == ARCHIVE_STATE_RESTORING:
        if _restore.roll_back(services, entry):
            _LOGGER.warning(
                "Interrupted restore rolled back (entry=%s kind=%s subject=%s)",
                entry.entry_id,
                entry.kind,
                entry.subject_id,
            )
            return True
        _LOGGER.warning(
            "Interrupted restore could not be rolled back; its files are not in the archive "
            "(entry=%s kind=%s subject=%s)",
            entry.entry_id,
            entry.kind,
            entry.subject_id,
        )
        return False
    if entry.state == ARCHIVE_STATE_RESTORED:
        _restore.follow_up(services, entry)
        _LOGGER.info("Interrupted restore completed (entry=%s kind=%s)", entry.entry_id, entry.kind)
        return True
    # ``purging`` entries keep what is not deleted yet; a later purge continues them.
    return False


def _roll_back_archive(services: ArchiveServices, entry: ArchiveEntry) -> bool:
    """Move an interrupted archive's files back and delete its entry; its Sessions never left."""
    for tree in entry.trees:
        if tree.source_path is None:
            continue
        payload = stored_path(services, tree.path)
        source = stored_path(services, tree.source_path)
        if payload.exists() and not os.path.lexists(source):
            source.parent.mkdir(parents=True, exist_ok=True)
            move_tree(payload, source)
    if entry.kind == ARCHIVE_KIND_PROJECT:
        reroot_agents(services, entry.facts.get("unrooted_agents") or (), entry.subject_id)
    retained = [tree.path for tree in entry.trees if stored_path(services, tree.path).exists()]
    if retained:
        _LOGGER.warning(
            "Interrupted archive could not be rolled back; its files stay in the archive "
            "(entry=%s kind=%s subject=%s)",
            entry.entry_id,
            entry.kind,
            entry.subject_id,
        )
        return False
    services.sessions.archive_ledger.abandon(entry.entry_key)
    for tree in entry.trees:
        prune_empty_parents(services, stored_path(services, tree.path))
    _LOGGER.warning(
        "Interrupted archive rolled back (entry=%s kind=%s subject=%s)",
        entry.entry_id,
        entry.kind,
        entry.subject_id,
    )
    return True


def _adopt_orphan_payloads(services: ArchiveServices) -> bool:
    """Record ``archive/entries/arc_*`` payloads that have no entry, for example after a
    data snapshot restore. Nothing else under ``archive/`` is ever claimed."""
    ledger = services.sessions.archive_ledger
    entries_dir = services.data_dir / ARCHIVE_ENTRIES_DIR
    try:
        candidates = sorted(
            child
            for child in entries_dir.iterdir()
            if child.name.startswith("arc_") and child.is_dir() and not child.is_symlink()
        )
    except OSError:
        return False
    relative = {f"{ARCHIVE_ENTRIES_DIR}/{child.name}": child for child in candidates}
    adopted = False
    for directory in ledger.unrecorded(tuple(relative)):
        child = relative[directory]
        kind, trees, subject_id, facts = _payload_contents(services, directory, child)
        if not trees:
            # An empty directory a failed archive left behind.
            with suppress(OSError):
                child.rmdir()
            continue
        archived_at = format_canonical_timestamp(datetime.fromtimestamp(child.stat().st_mtime, UTC))
        ref = ledger.adopt_payload(
            child.name,
            kind,
            subject_id=subject_id,
            archived_at=archived_at,
            trees=trees,
            facts=facts,
        )
        _LOGGER.warning(
            "Archive payload without an entry adopted (entry=%s kind=%s)", ref.entry_id, kind
        )
        adopted = True
    return adopted


def _payload_contents(
    services: ArchiveServices, directory: str, child: Any
) -> tuple[str, tuple[ArchiveTree, ...], str, dict[str, Any]]:
    """The kind, trees, subject and facts an orphan payload directory shows."""
    agent_dir = child / "agent"
    project_dir = child / "project"
    if agent_dir.is_dir():
        inspected = services.agents.inspect_archived(agent_dir)
        facts = _format_facts(inspected.problem, AGENT_FORMAT_VERSION)
        subject = child.name
        if inspected.agent is not None:
            subject = inspected.agent.id
            facts["name"] = inspected.agent.name
        return (
            ARCHIVE_KIND_AGENT,
            (ArchiveTree(f"{directory}/agent", ARCHIVE_TREE_AGENT, f"agents/{subject}"),),
            subject,
            facts,
        )
    if project_dir.is_dir():
        inspected_project = services.projects.inspect_archived(project_dir)
        facts = _format_facts(inspected_project.problem, PROJECT_FORMAT_VERSION)
        subject = child.name
        if inspected_project.project is not None:
            subject = inspected_project.project.project_id
            facts["name"] = inspected_project.project.display_name
            facts["cwd"] = inspected_project.project.cwd
        return (
            ARCHIVE_KIND_PROJECT,
            (ArchiveTree(f"{directory}/project", ARCHIVE_TREE_PROJECT, f"projects/{subject}"),),
            subject,
            facts,
        )
    if any(child.iterdir()):
        return ARCHIVE_KIND_FILES, (ArchiveTree(directory, ARCHIVE_TREE_FILES),), directory, {}
    return ARCHIVE_KIND_FILES, (), directory, {}


def _format_facts(problem: str | None, version: int) -> dict[str, Any]:
    if problem is None:
        return {"payload_format": version}
    if problem in {"older_format", "newer_format"}:
        return {"payload_format": problem.removesuffix("_format")}
    return {}


__all__ = ["recover"]
