"""Startup recovery: finish or undo what an interrupted archive operation left behind.

It runs before Channels, Cron and Runs start, so nothing else changes Agents,
Projects or archive entries meanwhile. Every step is idempotent; a step or an
entry whose recovery fails, such as an unreadable folder under ``archive/``, is
logged and tried again at the next start, and never stops the start itself.
"""

from __future__ import annotations

import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import AGENT_FORMAT_VERSION
from core.archive import _restore
from core.archive._operations import (
    cleanup_agent,
    cleanup_currents,
    prune_empty_parents,
    reroot_agents,
    stored_path,
)
from core.projects import PROJECT_FORMAT_VERSION
from core.sessions import (
    ARCHIVE_ENTRIES_DIR,
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_FILES,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_ROOT,
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
from core.utils.file_status import exists_strict, is_dir_strict, is_link_status
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp
from core.utils.tree_move import move_tree, remove_tree

if TYPE_CHECKING:
    from core.archive.archive import ArchiveServices

_LOGGER = get_logger("archive")


def recover(services: ArchiveServices) -> bool:
    """Bring the archive ledger and its payloads back in step; ``True`` when anything changed."""
    ledger = services.sessions.archive_ledger
    changed = False
    try:
        adoption = ledger.adopt_unrecorded_archived_rows()
    except Exception:
        _LOGGER.exception(
            "Archived Sessions without an archive entry could not be adopted; "
            "retried at the next start"
        )
    else:
        if adoption.sessions:
            _LOGGER.warning(
                "Archived Sessions without an archive entry adopted (sessions=%d entries=%d)",
                adoption.sessions,
                adoption.entries,
            )
            changed = True
    for entry in ledger.unsettled():
        try:
            changed = _settle(services, entry) or changed
        except Exception:
            _LOGGER.exception(
                "Archive entry recovery failed; it is retried at the next start (entry=%s)",
                entry.entry_id,
            )
    # After settling: an undone archive may leave a payload to adopt.
    try:
        changed = _adopt_orphan_payloads(services) or changed
    except Exception:
        _LOGGER.exception(
            "Archive payloads without an entry could not be adopted; retried at the next start"
        )
    return changed


def _settle(services: ArchiveServices, entry: ArchiveEntry) -> bool:
    if entry.state == ARCHIVE_STATE_ARCHIVING:
        return _roll_back_archive(services, entry)
    if entry.state == ARCHIVE_STATE_ARCHIVED and entry.cleanup_pending:
        if entry.kind == ARCHIVE_KIND_AGENT:
            cleanup_agent(services, entry)
        else:
            cleanup_currents(services, entry)
        _LOGGER.info("Interrupted archive completed (entry=%s kind=%s)", entry.entry_id, entry.kind)
        return True
    if entry.state == ARCHIVE_STATE_RESTORING:
        outcome = _restore.settle_interrupted(services, entry)
        if outcome == "stuck":
            settled = services.sessions.archive_ledger.entry_by_key(entry.entry_key)
            plan = (settled or entry).facts.get("restore_plan") or {}
            _LOGGER.warning(
                "Interrupted restore cannot finish; its files stay live "
                "(entry=%s kind=%s subject=%s path=%s): %s",
                entry.entry_id,
                entry.kind,
                entry.subject_id,
                plan.get("live_path"),
                plan.get("problem"),
            )
        else:
            _LOGGER.warning(
                "Interrupted restore %s (entry=%s kind=%s subject=%s)",
                "completed" if outcome == "completed" else "rolled back",
                entry.entry_id,
                entry.kind,
                entry.subject_id,
            )
        return True
    if entry.state == ARCHIVE_STATE_RESTORED:
        _restore.follow_up(services, entry)
        _LOGGER.info("Interrupted restore completed (entry=%s kind=%s)", entry.entry_id, entry.kind)
        return True
    # ``purging`` entries keep what is not deleted yet; the retention sweep continues them.
    return False


def _roll_back_archive(services: ArchiveServices, entry: ArchiveEntry) -> bool:
    """Undo an interrupted archive and delete its entry; its Sessions never left.

    A payload tree whose source path is free moves back. A payload tree beside
    a source is removed only when every payload file is also there, byte for
    byte. A restored configuration alone cannot establish that the source is
    complete: snapshots omit Workspace files. Unproven copies stay and are
    adopted as recovered entries.
    """
    archive_root = services.data_dir / ARCHIVE_ROOT
    kept: list[str] = []
    for tree in entry.trees:
        if tree.source_path is None:
            continue
        payload = stored_path(services, tree.path)
        source = stored_path(services, tree.source_path)
        # Raises when either cannot be checked: the entry is retried at the next start.
        if not exists_strict(payload):
            continue
        if not exists_strict(source, follow_symlinks=False):
            source.parent.mkdir(parents=True, exist_ok=True)
            move_tree(payload, source)
        elif _partial_copy(payload, source):
            remove_tree(payload, within=archive_root)
        else:
            kept.append(tree.path)
    if entry.kind == ARCHIVE_KIND_PROJECT:
        reroot_agents(
            services, entry.facts.get("unrooted_agents") or (), entry.subject_id, entry.archived_at
        )
    services.sessions.archive_ledger.abandon(entry.entry_key)
    for tree in entry.trees:
        prune_empty_parents(services, stored_path(services, tree.path))
    if kept:
        _LOGGER.warning(
            "Interrupted archive undone; the live %s may lack archived files, which stay "
            "for a recovered entry (entry=%s subject=%s paths=%s)",
            entry.kind,
            entry.entry_id,
            entry.subject_id,
            ", ".join(kept),
        )
    else:
        _LOGGER.warning(
            "Interrupted archive rolled back (entry=%s kind=%s subject=%s)",
            entry.entry_id,
            entry.kind,
            entry.subject_id,
        )
    return True


def _partial_copy(payload: Path, source: Path) -> bool:
    """Prove every payload entry exists unchanged in the source, without following links."""
    try:
        copied, original = payload.lstat(), source.lstat()
        if is_link_status(copied) or is_link_status(original):
            return False
        if stat.S_ISDIR(copied.st_mode) and stat.S_ISDIR(original.st_mode):
            return all(_partial_copy(child, source / child.name) for child in payload.iterdir())
        if not (stat.S_ISREG(copied.st_mode) and stat.S_ISREG(original.st_mode)):
            return False
        if copied.st_size != original.st_size:
            return False
        with payload.open("rb") as left, source.open("rb") as right:
            while chunk := left.read(1024 * 1024):
                if chunk != right.read(len(chunk)):
                    return False
            return right.read(1) == b""
    except OSError:
        # What cannot be compared is kept.
        return False


def _adopt_orphan_payloads(services: ArchiveServices) -> bool:
    """Record ``archive/entries/arc_*`` payloads that have no entry, for example after a
    data snapshot restore. Nothing else under ``archive/`` is ever claimed."""
    ledger = services.sessions.archive_ledger
    entries_dir = services.data_dir / ARCHIVE_ENTRIES_DIR
    try:
        children = sorted(entries_dir.iterdir())
    except FileNotFoundError:
        return False
    except OSError as error:
        _unreadable(entries_dir, error)
        return False
    candidates = []
    for child in children:
        try:
            if not child.name.startswith("arc_"):
                continue
            status = child.lstat()
            if stat.S_ISDIR(status.st_mode) and not is_link_status(status):
                candidates.append(child)
        except OSError as error:
            _unreadable(child, error)
    relative = {f"{ARCHIVE_ENTRIES_DIR}/{child.name}": child for child in candidates}
    adopted = False
    for directory in ledger.unrecorded(tuple(relative)):
        child = relative[directory]
        try:
            kind, trees, subject_id, facts = _payload_contents(services, directory, child)
            if not trees:
                # An empty directory a failed archive left behind.
                with suppress(OSError):
                    child.rmdir()
                continue
            mtime = child.stat().st_mtime
        except OSError as error:
            _unreadable(child, error)
            continue
        archived_at = format_canonical_timestamp(datetime.fromtimestamp(mtime, UTC))
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


def _unreadable(path: Path, error: OSError) -> None:
    _LOGGER.warning(
        "Archive payload not adopted; it cannot be read and is retried at the next start "
        "(path=%s): %s",
        path,
        error,
    )


def _payload_contents(
    services: ArchiveServices, directory: str, child: Path
) -> tuple[str, tuple[ArchiveTree, ...], str, dict[str, Any]]:
    """The kind, trees, subject and facts an orphan payload directory shows.

    Raises ``OSError`` when the directory cannot be read.
    """
    agent_dir = child / "agent"
    project_dir = child / "project"
    if is_dir_strict(agent_dir) and not is_link_status(agent_dir.lstat()):
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
    if is_dir_strict(project_dir) and not is_link_status(project_dir.lstat()):
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
