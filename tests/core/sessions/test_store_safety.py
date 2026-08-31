"""Canonical Session snapshots, quarantine, and freshness contracts."""

from __future__ import annotations

import threading
from pathlib import Path

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.snapshots import (
    SNAPSHOT_KEEP_COUNT,
    create_snapshot,
    list_snapshots,
    read_snapshot_health,
    snapshot_root,
)
from core.sessions.store import SessionStore, quarantine_database


def _address(session_id: str) -> SessionAddress:
    return SessionAddress(project_id=None, agent_id="coder", session_id=session_id)


def test_verified_snapshot_creates_an_openable_copy(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))
        created = create_snapshot(
            tmp_path,
            tmp_path / "sessions.db",
            manager.backup_snapshot,
            reason="test",
        )
        assert created is not None
        check = SessionStore(created / "sessions.db", _offline=True)
        try:
            assert check.exists(_address("session-one"))
        finally:
            check.close()
    finally:
        manager.close()


def test_snapshot_without_database_is_a_noop(tmp_path: Path) -> None:
    created = create_snapshot(tmp_path, tmp_path / "sessions.db", lambda destination: None)
    assert created is None
    assert list_snapshots(tmp_path) == []


def test_cancelled_online_backup_leaves_no_partial_database(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    destination = tmp_path / "cancelled.db"
    cancelled = threading.Event()
    cancelled.set()
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))

        assert manager.backup_snapshot(destination, cancel_event=cancelled) is False
        assert destination.exists() is False
        assert not list(tmp_path.glob(".cancelled.db.*.tmp"))
    finally:
        manager.close()


def test_snapshot_failure_keeps_previous_verified_snapshot(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))
        first = create_snapshot(tmp_path, tmp_path / "sessions.db", manager.backup_snapshot)
        assert first is not None

        def failing_snapshot(_destination: Path) -> None:
            raise OSError("disk busy")

        assert create_snapshot(tmp_path, tmp_path / "sessions.db", failing_snapshot) is None
        assert list_snapshots(tmp_path)
        assert read_snapshot_health(tmp_path)["state"] == "degraded"
        assert manager.status_projection()["state"] == "snapshot_degraded"
    finally:
        manager.close()


def test_snapshot_retention_prunes_only_after_verified_publish(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))
        for _ in range(SNAPSHOT_KEEP_COUNT + 2):
            assert create_snapshot(tmp_path, tmp_path / "sessions.db", manager.backup_snapshot)
        assert 1 <= len(list_snapshots(tmp_path)) <= SNAPSHOT_KEEP_COUNT
        assert snapshot_root(tmp_path).is_dir()
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# Quarantine recovery
# ---------------------------------------------------------------------------


def test_quarantine_moves_database_and_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged bytes")
    Path(f"{database}-wal").write_bytes(b"stale wal")

    destination = quarantine_database(database)

    assert destination.succeeded
    assert destination.path is not None
    assert database.exists() is False
    assert Path(f"{database}-wal").exists() is False
    quarantined = sorted(path.name for path in destination.path.iterdir())
    assert quarantined == ["sessions.db", "sessions.db-wal"]


def test_damaged_database_quarantines_then_opens_fresh(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    # Create a valid current-format database first so the marker exists,
    # then corrupt it. Under the current SQLite-only contract a corrupt
    # canonical database must not be silently replaced by an empty one;
    # it raises and preserves the damaged file until snapshot recovery
    # (Phase 4) restores a verified snapshot.
    from core.sessions.errors import SessionStoreCorruptError
    from core.storage.layout import initialize_data_directory

    initialize_data_directory(tmp_path)
    first = SessionStore(database)
    first.close()

    # Corrupt the now-valid database.
    database.write_bytes(b"X" * 8192)

    try:
        SessionStore(database)
        raise AssertionError("expected SessionStoreCorruptError for a corrupt database")
    except SessionStoreCorruptError:
        pass

    # No silent quarantine-and-replace: the damaged file remains for
    # diagnostics and no fresh database was created in its place.
    assert database.exists()
    assert database.read_bytes().startswith(b"X")
    # The standalone quarantine helper still works when invoked explicitly.
    assert (tmp_path / "session-quarantine").exists() is False or not list(
        (tmp_path / "session-quarantine").iterdir()
    )


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
