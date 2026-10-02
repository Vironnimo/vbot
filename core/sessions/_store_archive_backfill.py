"""Adoption of archives that have no archive entry yet.

Two sources exist. Older vBot versions moved deleted Agents and Projects into
legacy trees under ``archive/`` (``archive/agents/<id>/``,
``archive/projects/<id>/``, flat ``archive/<name>/``, and leftovers such as
staging directories) and archived Sessions without any entry. The Session
database migration ``sessions.0001_archive_entries`` adopts both; startup
repair adopts only archived Sessions an older vBot wrote after a downgrade. Only this module knows the legacy layouts: an
adopted tree is recorded by its path, so no other code branches on them.

Every step skips what an entry already records, so adoption is idempotent.
An adopted tree that may be a folder the user owns rather than a copy vBot made
(every ``files`` tree, and a Workspace moved in from outside the data directory)
is named in the entry's ``user_folders`` fact.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.sessions import _store_archive, _store_values
from core.sessions._archive_types import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_FILES,
    ARCHIVE_KIND_OWNER_GROUP,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ARCHIVE_ORIGIN_BACKFILL,
    ARCHIVE_ROOT,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_FILES,
    ARCHIVE_TREE_PROJECT,
    ARCHIVE_TREE_WORKSPACE,
    ArchiveAdoption,
    ArchiveTree,
)
from core.utils.file_status import is_dir_strict, is_file_strict
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp, utc_now_timestamp

_LOGGER = get_logger("sessions")

# The pairing window of a legacy Agent tree: an older vBot moved the Agent's
# directories into a fresh container right before it archived the Sessions, so
# their archive time follows the container's modification time closely. A
# legacy Project archive renamed the anchor directory itself, which keeps its
# own modification time, the Project's last edit: only a lower bound, so a
# Project tree has no upper bound.
_PAIRING_BEFORE = timedelta(seconds=2)
_PAIRING_AFTER = timedelta(minutes=10)
# Legacy roots under archive/ that never hold one flat Agent archive.
_RESERVED_ROOTS = frozenset({"agents", "projects", "sessions", "entries"})
_KIND_COUNTS = (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ARCHIVE_KIND_OWNER_GROUP,
    ARCHIVE_KIND_FILES,
)


@dataclass
class _Batch:
    """Archived Sessions without an entry that one archive operation left together."""

    project_id: str
    agent_id: str
    owner_name: str | None
    group_id: str | None
    archived_at: str
    keys: list[int] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)


@dataclass
class _LegacyTree:
    """One legacy archive container and the trees an entry for it records.

    ``extras`` are other trees in an Agent's container; a restore of the Agent
    leaves them, so a ``files`` entry of their own records them.
    """

    kind: str
    subject_id: str
    container: str
    trees: list[ArchiveTree]
    mtime: datetime
    facts: dict[str, Any]
    extras: list[ArchiveTree] = field(default_factory=list)


def adopt_legacy_archives(connection: sqlite3.Connection, data_dir: Path) -> ArchiveAdoption:
    """Give every legacy archive tree and every archived Session without one an entry.

    An Agent tree takes the Sessions its archive left when their archive time
    lies within the pairing window after the tree's modification time; a Project
    tree takes the latest of its Project's archives at or after that time.
    Other trees beside an Agent's ``agent/`` and moved ``workspace/`` get a
    ``files`` entry of their own. A directory that cannot be read is skipped with
    a warning and stays where it is, outside any entry.
    """
    now = utc_now_timestamp()
    batches = _unrecorded_batches(connection)
    counts: Counter[str] = Counter()
    sessions = 0
    for tree in sorted(_legacy_trees(connection, data_dir), key=lambda item: item.mtime):
        claimed = _claim_batches(tree, batches)
        facts = dict(tree.facts)
        facts["backfill"] = {
            "match": "timestamp" if claimed else "none",
            "tree_mtime": format_canonical_timestamp(tree.mtime),
        }
        project_id, agent_id = {
            ARCHIVE_KIND_AGENT: ("", tree.subject_id),
            ARCHIVE_KIND_PROJECT: (tree.subject_id, ""),
        }.get(tree.kind, ("", ""))
        archived_at = claimed[0].archived_at if claimed else format_canonical_timestamp(tree.mtime)
        ref = _store_archive.insert_entry(
            connection,
            kind=tree.kind,
            state=ARCHIVE_STATE_ARCHIVED,
            subject_id=tree.subject_id,
            project_id=project_id,
            agent_id=agent_id,
            archived_at=archived_at,
            retention_start=now,
            origin=ARCHIVE_ORIGIN_BACKFILL,
            facts=facts,
            trees=_fixed(tree.trees),
        )
        keys = [key for batch in claimed for key in batch.keys]
        _add_members(connection, ref.entry_key, keys)
        sessions += len(keys)
        counts[tree.kind] += 1
        if tree.extras:
            _store_archive.insert_entry(
                connection,
                kind=ARCHIVE_KIND_FILES,
                state=ARCHIVE_STATE_ARCHIVED,
                subject_id=tree.container,
                archived_at=archived_at,
                retention_start=now,
                origin=ARCHIVE_ORIGIN_BACKFILL,
                facts={
                    "backfill": {**facts["backfill"], "match": "none"},
                    "user_folders": [extra.path for extra in tree.extras],
                },
                trees=_fixed(tree.extras),
            )
            counts[ARCHIVE_KIND_FILES] += 1
    adopted = _adopt_batches(connection, batches, retention_start=now)
    counts.update(adopted.counts)
    result = ArchiveAdoption(sessions + adopted.sessions, dict(counts))
    if result.entries:
        _LOGGER.info(
            "Archive entries adopted (agents=%d projects=%d sessions=%d owner_groups=%d files=%d)",
            *(counts[kind] for kind in _KIND_COUNTS),
        )
    return result


def _fixed(trees: Sequence[ArchiveTree]) -> Callable[[str], Sequence[ArchiveTree]]:
    """Trees whose paths do not depend on the new entry's id."""
    recorded = tuple(trees)
    return lambda _entry_id: recorded


def adopt_unrecorded_archived_rows(connection: sqlite3.Connection) -> ArchiveAdoption:
    """Give archived Sessions without an entry their own entries, one per batch."""
    return _adopt_batches(
        connection, _unrecorded_batches(connection), retention_start=utc_now_timestamp()
    )


# -- Archived Sessions without an entry ---------------------------------------------


def _unrecorded_batches(connection: sqlite3.Connection) -> list[_Batch]:
    """Archived Sessions without membership, batched by scope or owner group and archive time."""
    batches: dict[tuple[Any, ...], _Batch] = {}
    # Unordered in SQL, so the read walks only the archived rows' partial index.
    rows = connection.execute(
        "SELECT s.session_key, s.project_id, s.agent_id, s.session_id, "
        "COALESCE(s.archived_at, s.last_activity_at) AS archived_at, b.owner_name, b.group_id "
        "FROM sessions AS s LEFT JOIN temporary_session_bindings AS b ON b.session_key = s.session_key "
        "WHERE s.state = 'archived' AND NOT EXISTS (SELECT 1 FROM archive_entry_sessions AS m "
        "WHERE m.session_key = s.session_key)"
    ).fetchall()
    for row in sorted(rows, key=lambda item: int(item["session_key"])):
        owned = row["owner_name"] is not None
        key = (
            ("owner", row["owner_name"], row["group_id"], row["archived_at"])
            if owned
            else ("scope", row["project_id"], row["agent_id"], row["archived_at"])
        )
        batch = batches.get(key)
        if batch is None:
            batch = batches[key] = _Batch(
                project_id=str(row["project_id"]),
                agent_id=str(row["agent_id"]),
                owner_name=str(row["owner_name"]) if owned else None,
                group_id=str(row["group_id"]) if owned else None,
                archived_at=str(row["archived_at"]),
            )
        batch.keys.append(int(row["session_key"]))
        batch.session_ids.append(str(row["session_id"]))
    return list(batches.values())


def _adopt_batches(
    connection: sqlite3.Connection, batches: Sequence[_Batch], *, retention_start: str
) -> ArchiveAdoption:
    counts: Counter[str] = Counter()
    sessions = 0
    for batch in batches:
        if not batch.keys:
            continue
        if batch.owner_name is not None:
            ref = _store_archive.insert_entry(
                connection,
                kind=ARCHIVE_KIND_OWNER_GROUP,
                state=ARCHIVE_STATE_ARCHIVED,
                subject_id=str(batch.group_id),
                owner_name=batch.owner_name,
                archived_at=batch.archived_at,
                retention_start=retention_start,
                origin=ARCHIVE_ORIGIN_BACKFILL,
            )
            counts[ARCHIVE_KIND_OWNER_GROUP] += 1
        else:
            ref = _store_archive.insert_entry(
                connection,
                kind=ARCHIVE_KIND_SESSION,
                state=ARCHIVE_STATE_ARCHIVED,
                subject_id=batch.session_ids[0],
                project_id=batch.project_id,
                agent_id=batch.agent_id,
                archived_at=batch.archived_at,
                retention_start=retention_start,
                origin=ARCHIVE_ORIGIN_BACKFILL,
            )
            counts[ARCHIVE_KIND_SESSION] += 1
        _add_members(connection, ref.entry_key, batch.keys)
        sessions += len(batch.keys)
        batch.keys.clear()
    return ArchiveAdoption(sessions, dict(counts))


def _add_members(connection: sqlite3.Connection, entry_key: int, keys: Sequence[int]) -> None:
    if keys:
        connection.execute(
            "INSERT INTO archive_entry_sessions (session_key, entry_key) "
            "SELECT value, ? FROM json_each(?)",
            (entry_key, _store_values._key_list(keys)),
        )


def _claim_batches(tree: _LegacyTree, batches: list[_Batch]) -> list[_Batch]:
    """Take the batches one Agent or Project archive left.

    An Agent tree takes the Identity-scope batch of its id closest in time within
    the pairing window. A Project tree takes every Agent's batch of its id
    archived at one instant, which is how one Project archive left them: the
    latest such instant at or after the tree's modification time, because a
    repeated archive of the same id replaced the earlier tree.
    """
    if tree.kind not in (ARCHIVE_KIND_AGENT, ARCHIVE_KIND_PROJECT):
        return []
    project = tree.kind == ARCHIVE_KIND_PROJECT
    lower = tree.mtime - _PAIRING_BEFORE
    upper = None if project else tree.mtime + _PAIRING_AFTER
    candidates: dict[str, list[_Batch]] = {}
    for batch in batches:
        if not batch.keys or batch.owner_name is not None:
            continue
        if tree.kind == ARCHIVE_KIND_AGENT and (batch.project_id, batch.agent_id) != (
            "",
            tree.subject_id,
        ):
            continue
        if tree.kind == ARCHIVE_KIND_PROJECT and batch.project_id != tree.subject_id:
            continue
        archived_at = _parsed(batch.archived_at)
        if (
            archived_at is None
            or archived_at < lower
            or (upper is not None and archived_at > upper)
        ):
            continue
        candidates.setdefault(batch.archived_at, []).append(batch)
    if not candidates:
        return []

    def instant(archived_at: str) -> datetime:
        return _parsed(archived_at) or tree.mtime

    if project:
        chosen = max(candidates, key=instant)
    else:
        chosen = min(candidates, key=lambda archived_at: abs(instant(archived_at) - tree.mtime))
    claimed = candidates[chosen]
    batches[:] = [batch for batch in batches if batch not in claimed]
    return claimed


def _parsed(value: str) -> datetime | None:
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


# -- Legacy trees ---------------------------------------------------------------


def _legacy_trees(connection: sqlite3.Connection, data_dir: Path) -> Iterator[_LegacyTree]:
    """Discover the legacy archive containers no entry records yet."""
    for child in _directories(Path(data_dir) / ARCHIVE_ROOT):
        if child.name == "entries":
            continue
        containers: list[tuple[Path, Callable[[Path, Path], _LegacyTree | None] | None]]
        if child.name in ("agents", "projects"):
            match = _agent_tree if child.name == "agents" else _project_tree
            containers = [(container, match) for container in _directories(child)]
        else:
            containers = [(child, None if child.name in _RESERVED_ROOTS else _agent_tree)]
        for container, owner in containers:
            try:
                tree = (owner and owner(container, data_dir)) or _files_tree(container, data_dir)
            except OSError as error:
                _unreadable(container, error)
                continue
            if tree is not None and not _store_archive.is_recorded(connection, tree.container):
                yield tree


def _directories(path: Path) -> list[Path]:
    """The readable subdirectories of ``path`` by name; links are never followed."""
    try:
        children = sorted(path.iterdir(), key=lambda entry: entry.name)
    except FileNotFoundError:
        return []
    except OSError as error:
        _unreadable(path, error)
        return []
    directories = []
    for child in children:
        try:
            if is_dir_strict(child) and not child.is_symlink():
                directories.append(child)
        except OSError as error:
            _unreadable(child, error)
    return directories


def _unreadable(path: Path, error: OSError) -> None:
    _LOGGER.warning(
        "Archive folder not adopted; it cannot be read and stays as it is (path=%s): %s",
        path,
        error,
    )


def _relative(path: Path, data_dir: Path) -> str:
    """The data-dir relative POSIX path of a tree below ``archive/``."""
    return path.relative_to(data_dir).as_posix()


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _document(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _payload_format(document: dict[str, Any] | None) -> dict[str, Any]:
    """The format of a payload document: its version, or ``older`` without a valid one."""
    if document is None:
        return {}
    version = document.get("format_version")
    if isinstance(version, int) and not isinstance(version, bool) and version >= 1:
        return {"payload_format": version}
    return {"payload_format": "older"}


def _name(document: dict[str, Any] | None) -> dict[str, Any]:
    name = None if document is None else document.get("name")
    return {"name": name} if isinstance(name, str) and name.strip() else {}


def _agent_tree(container: Path, data_dir: Path) -> _LegacyTree | None:
    """An Agent archive: ``<container>/agent/`` with an optional moved Workspace beside it.

    Raises ``OSError`` when the container cannot be read.
    """
    agent_dir = container / "agent"
    if not is_dir_strict(agent_dir):
        return None
    mtime = _mtime(container)
    subject = container.name
    document = _document(agent_dir / "agent.json")
    workspace = None if document is None else document.get("workspace")
    trees = [ArchiveTree(_relative(agent_dir, data_dir), ARCHIVE_TREE_AGENT, f"agents/{subject}")]
    facts: dict[str, Any] = {**_name(document), **_payload_format(document)}
    root_project_id = None if document is None else document.get("root_project_id")
    facts["root_project_id"] = root_project_id if isinstance(root_project_id, str) else None
    moved = False
    extras: list[ArchiveTree] = []
    for child in sorted(container.iterdir(), key=lambda entry: entry.name):
        if child.name == "agent":
            continue
        if child.name == "workspace" and is_dir_strict(child):
            moved = True
            if isinstance(workspace, str) and _outside(workspace, data_dir):
                facts["user_folders"] = [_relative(child, data_dir)]
            trees.append(
                ArchiveTree(
                    _relative(child, data_dir),
                    ARCHIVE_TREE_WORKSPACE,
                    workspace if isinstance(workspace, str) and workspace else None,
                )
            )
        else:
            extras.append(ArchiveTree(_relative(child, data_dir), ARCHIVE_TREE_FILES, None))
    if isinstance(workspace, str) and workspace:
        facts["workspace"] = {
            "path": workspace,
            "external": moved or not _inside_agent_home(workspace, subject, data_dir),
            "moved": moved,
        }
    return _LegacyTree(
        ARCHIVE_KIND_AGENT, subject, _relative(container, data_dir), trees, mtime, facts, extras
    )


def _outside(workspace: str, data_dir: Path) -> bool:
    """Whether a stored Workspace path names a folder outside the data directory."""
    path = Path(workspace)
    if not path.is_absolute():
        return False
    try:
        return not path.resolve().is_relative_to(Path(data_dir).resolve())
    except (OSError, RuntimeError):
        return not path.is_relative_to(data_dir)


def _inside_agent_home(workspace: str, agent_id: str, data_dir: Path) -> bool:
    """Whether a stored Workspace path lies inside the Agent's own home ``agents/<id>``.

    A Workspace inside the data directory is stored relative to it.
    """
    home = Path(data_dir) / "agents" / agent_id
    path = Path(workspace)
    return (path if path.is_absolute() else Path(data_dir) / path).is_relative_to(home)


def _project_tree(container: Path, data_dir: Path) -> _LegacyTree | None:
    """A Project archive: the moved anchor directory holding ``project.json``.

    Raises ``OSError`` when the container cannot be read.
    """
    if not is_file_strict(container / "project.json"):
        return None
    mtime = _mtime(container)
    document = _document(container / "project.json")
    cwd = None if document is None else document.get("cwd")
    facts: dict[str, Any] = {**_name(document), **_payload_format(document)}
    if isinstance(cwd, str) and cwd:
        facts["cwd"] = cwd
    path = _relative(container, data_dir)
    return _LegacyTree(
        ARCHIVE_KIND_PROJECT,
        container.name,
        path,
        [ArchiveTree(path, ARCHIVE_TREE_PROJECT, f"projects/{container.name}")],
        mtime,
        facts,
    )


def _files_tree(container: Path, data_dir: Path) -> _LegacyTree | None:
    """Any other non-empty directory: archived files without a restorable owner.

    Raises ``OSError`` when the container cannot be read.
    """
    if not any(container.iterdir()):
        return None
    mtime = _mtime(container)
    path = _relative(container, data_dir)
    return _LegacyTree(
        ARCHIVE_KIND_FILES,
        path,
        path,
        [ArchiveTree(path, ARCHIVE_TREE_FILES, None)],
        mtime,
        {"user_folders": [path]},
    )


__all__ = ["adopt_legacy_archives", "adopt_unrecorded_archived_rows"]
