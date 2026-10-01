"""Adoption of archives written before archive entries existed."""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_FILES,
    ARCHIVE_KIND_OWNER_GROUP,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ArchiveAdoption,
    ArchiveEntry,
    ArchiveEntryFilter,
    ArchiveTree,
    ChatSessionManager,
    SessionAddress,
)
from core.utils.timestamps import format_canonical_timestamp

_MIGRATION = "sessions.0001_archive_entries"
_BASE = datetime(2026, 8, 13, 21, 0, tzinfo=UTC)


def _open(data_dir: Path, template: Path) -> ChatSessionManager:
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(template / name, data_dir)
    return ChatSessionManager(data_dir)


def _archive_rows(data_dir: Path, archived_at: datetime, *session_ids: str) -> None:
    """Archive rows the way an older vBot did: no archive entry."""
    with sqlite3.connect(data_dir / "sessions.db") as connection:
        connection.executemany(
            "UPDATE sessions SET state = 'archived', archived_at = ? WHERE session_id = ?",
            [(format_canonical_timestamp(archived_at), session_id) for session_id in session_ids],
        )


def _forget_migration(data_dir: Path) -> None:
    with sqlite3.connect(data_dir / "sessions.db") as connection:
        connection.execute("DELETE FROM kernel_migrations WHERE name = ?", (_MIGRATION,))


def _write(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def _touch(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def _refuse_access(monkeypatch: pytest.MonkeyPatch, locked: Path) -> None:
    """Make ``locked`` unreadable the way a folder without read permission is.

    Its own entry stays visible, but listing it fails, and so does every check
    of a path inside it, as ``Path.is_dir`` and ``Path.is_file`` raise before
    Python 3.14.
    """

    def refuse(method: str, inside_only: bool) -> None:
        real = getattr(Path, method)

        def guarded(self: Path, *args: Any, **kwargs: Any) -> Any:
            if locked in self.parents or (not inside_only and self == locked):
                raise PermissionError(errno.EACCES, "Access is denied", str(self))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, guarded)

    refuse("iterdir", inside_only=False)
    for method in ("stat", "is_dir", "is_file", "exists"):
        refuse(method, inside_only=True)


def _entries(manager: ChatSessionManager) -> dict[tuple[str, str], ArchiveEntry]:
    page = manager.archive_ledger.page(ArchiveEntryFilter(), limit=100)
    assert page.next_cursor is None
    return {(entry.kind, entry.subject_id): entry for entry in page.entries}


def test_migration_adopts_legacy_trees_and_the_sessions_their_archives_left(
    tmp_path: Path, current_session_store_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _open(tmp_path, current_session_store_template)
    for agent_id, session_id in (
        ("deepseek", "d-1"),
        ("deepseek", "d-2"),
        ("main", "m-1"),
        ("ev-01", "e-1"),
        ("ev-01", "e-2"),
    ):
        manager.create(agent_id, session_id=session_id)
    for agent_id, session_id in (("lead", "t-0"), ("lead", "t-1"), ("coder", "t-2")):
        manager.create(agent_id, session_id=session_id, project_id="team")
    for participant in ("p-1", "p-2"):
        manager.create_bound_temporary_session(
            SessionAddress(None, "tmp-swarm", participant),
            owner_name="swarm",
            group_id="docs",
            participant_id=participant,
            config={},
        )
    manager.close()
    # An earlier archive of the Project "team", whose tree the latest one replaced.
    _archive_rows(tmp_path, _BASE - timedelta(days=5), "t-0")
    _archive_rows(tmp_path, _BASE + timedelta(seconds=30), "d-1", "d-2")
    _archive_rows(tmp_path, _BASE + timedelta(hours=1), "m-1")
    _archive_rows(tmp_path, _BASE + timedelta(hours=2, seconds=5), "t-1", "t-2")
    _archive_rows(tmp_path, _BASE + timedelta(hours=3), "e-1", "e-2")
    _archive_rows(tmp_path, _BASE + timedelta(hours=4), "p-1", "p-2")
    _forget_migration(tmp_path)

    archive = tmp_path / "archive"
    _write(archive / "agents/deepseek/agent/agent.json", {"id": "deepseek", "name": "DeepSeek"})
    _write(archive / "agents/main/agent/agent.json", {"format_version": 1, "id": "main"})
    _write(archive / "projects/team/project.json", {"format_version": 1, "cwd": "C:/repo/team"})
    _write(
        archive / "coder/agent/agent.json",
        {"format_version": 2, "workspace": "C:/repo/coder", "root_project_id": "team"},
    )
    _write(archive / "coder/workspace/notes.md", "kept")
    _write(archive / "agents/.coder-archive-x1/previous/agent/agent.json", {})
    _write(archive / "sessions/old.jsonl", "{}")
    _write(archive / "readme.txt", "a user's note")
    (archive / "empty").mkdir()
    (archive / "entries/arc_orphan/agent").mkdir(parents=True)
    # Folders the migration cannot read stay as they are, outside any entry.
    for locked in ("agents/locked", "locked-files"):
        _write(archive / locked / "agent/agent.json", {"id": "locked"})
        _refuse_access(monkeypatch, archive / locked)
    _touch(archive / "agents/deepseek", _BASE)
    # main's tree is newer than the pairing window after its only archived Session.
    _touch(archive / "agents/main", _BASE + timedelta(hours=1, minutes=16))
    # Archiving a Project renamed its directory, which kept the time of its last edit.
    _touch(archive / "projects/team", _BASE - timedelta(days=2))
    started = format_canonical_timestamp(datetime.now(UTC) - timedelta(seconds=1))

    manager = ChatSessionManager(tmp_path)
    entries = _entries(manager)

    assert set(entries) == {
        (ARCHIVE_KIND_AGENT, "deepseek"),
        (ARCHIVE_KIND_AGENT, "main"),
        (ARCHIVE_KIND_AGENT, "coder"),
        (ARCHIVE_KIND_PROJECT, "team"),
        (ARCHIVE_KIND_SESSION, "m-1"),
        (ARCHIVE_KIND_SESSION, "t-0"),
        (ARCHIVE_KIND_SESSION, "e-1"),
        (ARCHIVE_KIND_OWNER_GROUP, "docs"),
        (ARCHIVE_KIND_FILES, "archive/agents/.coder-archive-x1"),
        (ARCHIVE_KIND_FILES, "archive/sessions"),
    }
    assert {entry.origin for entry in entries.values()} == {"backfill"}
    assert all(entry.retention_start >= started for entry in entries.values())
    summary = {
        key: (entry.session_count, entry.project_id, entry.agent_id)
        for key, entry in entries.items()
    }
    assert summary[(ARCHIVE_KIND_AGENT, "deepseek")] == (2, "", "deepseek")
    assert summary[(ARCHIVE_KIND_AGENT, "main")] == (0, "", "main")
    assert summary[(ARCHIVE_KIND_SESSION, "m-1")] == (1, "", "main")
    assert summary[(ARCHIVE_KIND_PROJECT, "team")] == (2, "team", "")
    assert summary[(ARCHIVE_KIND_SESSION, "t-0")] == (1, "team", "lead")
    assert summary[(ARCHIVE_KIND_SESSION, "e-1")] == (2, "", "ev-01")
    assert summary[(ARCHIVE_KIND_OWNER_GROUP, "docs")] == (2, "", "")
    assert entries[(ARCHIVE_KIND_OWNER_GROUP, "docs")].owner_name == "swarm"

    deepseek = entries[(ARCHIVE_KIND_AGENT, "deepseek")]
    assert deepseek.archived_at == format_canonical_timestamp(_BASE + timedelta(seconds=30))
    assert deepseek.facts == {
        "name": "DeepSeek",
        "payload_format": "older",
        "root_project_id": None,
        "backfill": {"match": "timestamp", "tree_mtime": format_canonical_timestamp(_BASE)},
    }
    assert deepseek.trees == (
        ArchiveTree("archive/agents/deepseek/agent", "agent", "agents/deepseek"),
    )
    main = entries[(ARCHIVE_KIND_AGENT, "main")]
    assert main.facts["backfill"]["match"] == "none"
    assert main.archived_at == main.facts["backfill"]["tree_mtime"]
    team = entries[(ARCHIVE_KIND_PROJECT, "team")]
    assert team.archived_at == format_canonical_timestamp(_BASE + timedelta(hours=2, seconds=5))
    assert team.facts == {
        "payload_format": 1,
        "cwd": "C:/repo/team",
        "backfill": {
            "match": "timestamp",
            "tree_mtime": format_canonical_timestamp(_BASE - timedelta(days=2)),
        },
    }
    assert entries[(ARCHIVE_KIND_PROJECT, "team")].trees == (
        ArchiveTree("archive/projects/team", "project", "projects/team"),
    )
    coder = entries[(ARCHIVE_KIND_AGENT, "coder")]
    assert coder.facts["payload_format"] == 2
    assert coder.facts["workspace"] == {"path": "C:/repo/coder", "external": True, "moved": True}
    assert coder.trees == (
        ArchiveTree("archive/coder/agent", "agent", "agents/coder"),
        ArchiveTree("archive/coder/workspace", "workspace", "C:/repo/coder"),
    )

    # Adopting again finds everything recorded.
    manager.close()
    _forget_migration(tmp_path)
    manager = ChatSessionManager(tmp_path)
    assert _entries(manager).keys() == entries.keys()
    manager.close()
    monkeypatch.undo()
    assert (archive / "agents/locked/agent/agent.json").is_file()
    assert (archive / "locked-files/agent/agent.json").is_file()


def test_startup_repair_adopts_archived_sessions_written_without_an_entry(
    tmp_path: Path, current_session_store_template: Path
) -> None:
    manager = _open(tmp_path, current_session_store_template)
    manager.create("coder", session_id="one")
    manager.create("coder", session_id="two")
    _archive_rows(tmp_path, _BASE, "one", "two")
    ledger = manager.archive_ledger

    assert ledger.adopt_unrecorded_archived_rows() == ArchiveAdoption(2, {"session": 1})
    assert ledger.adopt_unrecorded_archived_rows() == ArchiveAdoption(0, {})
    (entry,) = _entries(manager).values()
    assert (entry.kind, entry.session_count, entry.archived_at) == (
        ARCHIVE_KIND_SESSION,
        2,
        format_canonical_timestamp(_BASE),
    )
    manager.close()
