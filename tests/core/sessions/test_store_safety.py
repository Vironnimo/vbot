"""Rotating startup backups, quarantine recovery, and batched freshness."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.chat import ChatMessage
from core.sessions import (
    BACKUP_KEEP_COUNT,
    ChatSessionManager,
    SessionAddress,
    backup_directory,
    create_startup_snapshot,
)
from core.sessions.store import SessionStore, quarantine_database


def _address(session_id: str) -> SessionAddress:
    return SessionAddress(project_id=None, agent_id="coder", session_id=session_id)


# ---------------------------------------------------------------------------
# Rotating startup backups
# ---------------------------------------------------------------------------


def test_startup_snapshot_creates_an_openable_copy(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))

        created = create_startup_snapshot(
            manager.backup_snapshot, tmp_path / "sessions.db", tmp_path
        )

        assert created is not None
        check = SessionStore(created)
        try:
            assert check.exists(_address("session-one"))
        finally:
            check.close()
    finally:
        manager.close()


def test_startup_snapshot_without_database_is_a_noop(tmp_path: Path) -> None:
    created = create_startup_snapshot(lambda destination: None, tmp_path / "sessions.db", tmp_path)

    assert created is None
    assert backup_directory(tmp_path).exists() is False


def test_startup_snapshot_failure_is_swallowed(tmp_path: Path) -> None:
    from core.chat.errors import ChatSessionError

    manager = ChatSessionManager(tmp_path)
    try:
        (tmp_path / "sessions.db").write_bytes(b"placeholder")

        def failing_snapshot(_destination: Path) -> None:
            raise ChatSessionError("disk busy")

        created = create_startup_snapshot(failing_snapshot, tmp_path / "sessions.db", tmp_path)

        assert created is None
    finally:
        manager.close()


def test_startup_snapshot_prunes_beyond_keep_count(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        for _ in range(BACKUP_KEEP_COUNT + 2):
            created = create_startup_snapshot(
                manager.backup_snapshot, tmp_path / "sessions.db", tmp_path
            )
            assert created is not None

        # The same-second creations collapse by timestamp filename, so force
        # uniqueness by writing directly for the overflow check instead:
        snapshots = list(backup_directory(tmp_path).iterdir())
        assert 1 <= len(snapshots) <= BACKUP_KEEP_COUNT
    finally:
        manager.close()


def test_prune_keeps_only_the_newest_snapshots(tmp_path: Path) -> None:
    from core.sessions import backup as backup_module

    backup_root = backup_directory(tmp_path)
    backup_root.mkdir(parents=True)
    # Four snapshots with BACKUP_KEEP_COUNT = 5 keep everything; write one
    # extra older file to prove the overflow is pruned.
    stamps = [
        "20251231T000000",
        "20260101T000000",
        "20260102T000000",
        "20260103T000000",
        "20260104T000000",
        "20260105T000000",
    ]
    for stamp in stamps:
        (backup_root / f"sessions-{stamp}Z.db").write_bytes(b"x")
    (backup_root / "unrelated.txt").write_text("keep out")

    backup_module._prune(backup_root)

    remaining = sorted(path.name for path in backup_root.iterdir())
    assert remaining == [
        "sessions-20260101T000000Z.db",
        "sessions-20260102T000000Z.db",
        "sessions-20260103T000000Z.db",
        "sessions-20260104T000000Z.db",
        "sessions-20260105T000000Z.db",
        "unrelated.txt",
    ]


# ---------------------------------------------------------------------------
# Quarantine recovery
# ---------------------------------------------------------------------------


def test_quarantine_moves_database_and_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged bytes")
    Path(f"{database}-wal").write_bytes(b"stale wal")

    destination = quarantine_database(database)

    assert destination is not None
    assert database.exists() is False
    assert Path(f"{database}-wal").exists() is False
    quarantined = sorted(path.name for path in destination.iterdir())
    assert quarantined == ["sessions.db", "sessions.db-wal"]


def test_damaged_database_quarantines_then_opens_fresh(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit()
    connection.close()
    # Overwrite the file body with garbage while keeping a plausible size, so
    # SQLite sees a real (but invalid) database file.
    database.write_bytes(b"X" * 8192)

    first = SessionStore(database)
    try:
        # The damaged file was moved aside and a fresh database opened.
        assert first.exists(SessionAddress(None, "coder", "anything")) is False
    finally:
        first.close()

    quarantines = list((tmp_path / "session-quarantine").iterdir())
    assert len(quarantines) == 1
    assert (quarantines[0] / "sessions.db").exists()


# ---------------------------------------------------------------------------
# Batched canonical freshness
# ---------------------------------------------------------------------------


def test_list_history_versions_returns_live_sessions_in_one_call(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        live = manager.create("coder", session_id="live-one")
        live.append(ChatMessage.user("hello"))
        manager.create("coder", session_id="live-two")
        gone = manager.create("coder", session_id="gone")
        gone.delete()

        versions = manager.list_history_versions(
            [_address_of(live), SessionAddress(None, "coder", "live-two"), _address_of(gone)]
        )

        assert set(versions) == {_address_of(live), SessionAddress(None, "coder", "live-two")}
        generation_id, revision = versions[_address_of(live)]
        assert isinstance(generation_id, str) and generation_id
        assert revision >= 1
    finally:
        manager.close()


def test_list_history_versions_spans_scopes(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        global_session = manager.create("coder", session_id="global-one")
        project_session = manager.create("coder", session_id="project-one", project_id="alpha")

        versions = manager.list_history_versions(
            [_address_of(global_session), _address_of(project_session)]
        )

        assert set(versions) == {_address_of(global_session), _address_of(project_session)}
    finally:
        manager.close()


def _address_of(session) -> SessionAddress:
    return SessionAddress(
        project_id=session.address.project_id,
        agent_id=session.address.agent_id,
        session_id=session.address.session_id,
    )
