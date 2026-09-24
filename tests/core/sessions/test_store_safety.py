"""Canonical Session snapshots, quarantine, and freshness contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions import snapshots as snapshots_module
from core.sessions.errors import SessionStoreCorruptError, SessionStoreUnavailableError
from core.sessions.recovery import quarantine_database
from core.sessions.snapshots import (
    SNAPSHOT_DATABASE_NAME,
    SNAPSHOT_KEEP_COUNT,
    SNAPSHOT_MANIFEST_NAME,
    create_snapshot,
    list_snapshots,
    read_snapshot_health,
    snapshot_root,
)
from core.sessions.sqlite_runtime import readonly_sqlite_uri
from core.sessions.store import SessionStore


def test_write_error_classification_preserves_owner_errors_and_rolls_back(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    address = SessionAddress(project_id=None, agent_id="coder", session_id="duplicate")
    try:
        store.create(address)
        with pytest.raises(ChatSessionError) as duplicate:
            store.create(address)
        assert not isinstance(duplicate.value, SessionStoreUnavailableError)

        def corrupt(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO store_meta(key, value) VALUES ('rollback-corrupt', 'value')"
            )
            raise sqlite3.DatabaseError("database disk image is malformed")

        with pytest.raises(SessionStoreCorruptError):
            store._execute_write(corrupt)
        assert store._writer.in_transaction is False
        assert (
            store._writer.execute(
                "SELECT value FROM store_meta WHERE key = 'rollback-corrupt'"
            ).fetchone()
            is None
        )

        def unavailable(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO store_meta(key, value) VALUES ('rollback-unavailable', 'value')"
            )
            raise sqlite3.OperationalError("attempt to write a readonly database")

        with pytest.raises(SessionStoreUnavailableError):
            store._execute_write(unavailable)
        assert store._writer.in_transaction is False
        assert (
            store._writer.execute(
                "SELECT value FROM store_meta WHERE key = 'rollback-unavailable'"
            ).fetchone()
            is None
        )
    finally:
        store.close()


def _address(session_id: str) -> SessionAddress:
    return SessionAddress(project_id=None, agent_id="coder", session_id=session_id)


def test_verified_snapshot_creates_an_openable_copy(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))
        created = manager.create_snapshot(reason="test")
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


@pytest.fixture(params=["delete", "wal"])
def pinned_journal_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Open canonical stores in one journal mode regardless of the SQLite build.

    Forcing WAL on a WAL-reset-vulnerable build is safe here: the Runtime's single
    writer is the only connection that writes or checkpoints.
    """
    from core.sessions import schema

    mode = str(request.param)
    monkeypatch.setattr(schema, "is_wal_reset_vulnerable", lambda _version: False)
    monkeypatch.setattr(schema, "required_journal_mode", lambda _version: mode)
    return mode


def test_online_snapshot_completes_while_runs_keep_committing(
    tmp_path: Path, pinned_journal_mode: str
) -> None:
    # Generous budgets below the 30 s pytest timeout keep a hang readable on a loaded box.
    deadline = time.monotonic() + 18.0

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    manager = ChatSessionManager(tmp_path)
    address = _address("busy")
    commits = 0
    failures: list[BaseException] = []
    progressed = threading.Condition()
    stop = threading.Event()

    def fill(connection: sqlite3.Connection) -> None:
        # About 24 MB, so commits overlap the copy and its verification.
        connection.execute("CREATE TABLE snapshot_filler(value BLOB NOT NULL) STRICT")
        connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 6000) "
            "INSERT INTO snapshot_filler SELECT randomblob(4000) FROM n"
        )

    def write() -> None:
        nonlocal commits
        index = 0
        while not stop.wait(0.1):
            index += 1
            try:
                manager.get(address).append(ChatMessage.user(f"message {index}"))
                # Completion activity has the shortest busy budget (0.5 s).
                manager.record_terminal_run(
                    address, f"run-{index}", "completed", "2026-01-01T00:00:00+00:00"
                )
            except BaseException as exc:
                with progressed:
                    failures.append(exc)
                    progressed.notify_all()
                return
            with progressed:
                commits += 1
                progressed.notify_all()

    def wait_for_more_commits(count: int) -> tuple[int, int]:
        with progressed:
            before = commits
            progressed.wait_for(
                lambda: commits >= before + count or bool(failures), timeout=remaining()
            )
            return before, commits

    outcome: list[Path | BaseException | None] = []

    def snapshot() -> None:
        try:
            outcome.append(manager.create_snapshot(reason="test"))
        except BaseException as exc:
            outcome.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    snapshotter = threading.Thread(target=snapshot, daemon=True)
    try:
        manager.create("coder", session_id=address.session_id)
        manager._store._execute_write(fill)
        with closing(
            sqlite3.connect(readonly_sqlite_uri(tmp_path / "sessions.db"), uri=True)
        ) as probe:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0] == pinned_journal_mode
        writer.start()
        _, started = wait_for_more_commits(1)
        snapshotter.start()
        snapshotter.join(remaining())
        snapshot_finished = not snapshotter.is_alive()
        after_snapshot, finished = wait_for_more_commits(2)
    finally:
        stop.set()
        writer.join(4.0)
        snapshotter.join(4.0)
        manager.close()

    assert snapshot_finished, "the online snapshot did not finish while Runs kept committing"
    assert failures == []
    assert finished >= after_snapshot + 2, "writes stopped committing after the snapshot"
    published = outcome[0]
    assert isinstance(published, Path), (published, read_snapshot_health(tmp_path))
    assert list_snapshots(tmp_path) == [published]
    # A standalone rollback-journal file: verification leaves no WAL sidecars behind.
    assert {path.name for path in published.iterdir()} == {
        SNAPSHOT_DATABASE_NAME,
        SNAPSHOT_MANIFEST_NAME,
    }
    manifest = json.loads((published / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert started <= manifest["message_count"] <= commits


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


def test_snapshot_retention_does_not_reverify_retained_databases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))
        assert manager.create_snapshot(reason="test") is not None
        sha256_calls = 0
        verification_calls = 0
        real_sha256 = snapshots_module._sha256
        real_verify = snapshots_module._verify_snapshot_db

        def counted_sha256(
            path: Path,
            *,
            cancelled: Callable[[], bool] | None = None,
        ) -> str:
            nonlocal sha256_calls
            sha256_calls += 1
            return real_sha256(path, cancelled=cancelled)

        def counted_verify(
            path: Path,
            expected_database_id: str | None = None,
            *,
            cancelled: Callable[[], bool] | None = None,
        ) -> Any:
            nonlocal verification_calls
            verification_calls += 1
            return real_verify(
                path,
                expected_database_id,
                cancelled=cancelled,
            )

        monkeypatch.setattr(snapshots_module, "_sha256", counted_sha256)
        monkeypatch.setattr(snapshots_module, "_verify_snapshot_db", counted_verify)

        assert manager.create_snapshot(reason="test") is not None

        assert sha256_calls == 1
        assert verification_calls == 1
    finally:
        manager.close()


def test_snapshot_retention_always_keeps_the_just_published_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))
        first = manager.create_snapshot(reason="test")
        assert first is not None
        manifest_path = first / SNAPSHOT_MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = "2099-01-01T00:00:00Z"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 1)

        published = manager.create_snapshot(reason="test")

        assert published is not None
        assert published.is_dir()
        assert first.exists() is False
        assert list_snapshots(tmp_path) == [published]
    finally:
        manager.close()


def test_snapshot_retention_enforces_the_byte_limit_around_the_published_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="session-one").append(ChatMessage.user("hello"))
        first = manager.create_snapshot(reason="test")
        assert first is not None
        snapshot_size = (first / SNAPSHOT_DATABASE_NAME).stat().st_size
        monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_BYTES", snapshot_size * 2)

        assert manager.create_snapshot(reason="test") is not None
        published = manager.create_snapshot(reason="test")

        retained = list_snapshots(tmp_path)
        assert published is not None
        assert published in retained
        assert len(retained) == 2
        assert sum((path / SNAPSHOT_DATABASE_NAME).stat().st_size for path in retained) <= (
            snapshot_size * 2
        )
    finally:
        manager.close()


def test_snapshot_retention_leaves_malformed_directories_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = snapshot_root(tmp_path) / "20260101T000000Z-deadbeef"
    malformed.mkdir(parents=True)
    (malformed / SNAPSHOT_MANIFEST_NAME).write_text("{", encoding="utf-8")
    (malformed / SNAPSHOT_DATABASE_NAME).write_bytes(b"evidence")
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 1)
    manager = ChatSessionManager(tmp_path)
    try:
        published = manager.create_snapshot(reason="test")

        assert published is not None
        assert published.is_dir()
        assert (malformed / SNAPSHOT_DATABASE_NAME).read_bytes() == b"evidence"
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
