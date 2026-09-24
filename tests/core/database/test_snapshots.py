"""Data snapshots: capture, strict manifests, per-member verification and retention."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    DatabaseUnavailableError,
    create_data_snapshot,
    data_store_status,
    list_data_snapshots,
    open_database,
    read_marker,
    read_snapshot_health,
    required_journal_mode,
    snapshot_root,
    snapshot_summaries,
)
from core.database import snapshots as snapshots_module
from core.database.marker import MarkerEntry, _write_marker
from core.database.snapshots import (
    SNAPSHOT_MANIFEST_NAME,
    member_restore_candidates,
    snapshot_inventory,
)
from tests.core.database.database_test_support import (
    add_note,
    notes_spec,
    snapshot_with_notes,
)


def _specs(data_dir: Path) -> dict:
    return {"notes": notes_spec(data_dir), "tasks": notes_spec(data_dir, name="tasks")}


def _manifest(snapshot: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (snapshot / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    return manifest


def _rewrite_manifest(snapshot: Path, change: Callable[[dict[str, Any]], None]) -> None:
    payload = _manifest(snapshot)
    change(payload)
    (snapshot / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")


def test_snapshot_captures_every_registered_database_as_one_verified_member_each(
    data_dir: Path,
) -> None:
    tasks = open_database(notes_spec(data_dir, name="tasks"))
    add_note(tasks, "closed while the snapshot runs")
    tasks.close()
    notes = open_database(notes_spec(data_dir))
    try:
        add_note(notes, "open during the snapshot")
        snapshot = create_data_snapshot(
            data_dir,
            reason="test",
            databases=(notes,),
            specs=(notes_spec(data_dir, name="tasks"),),
        )
    finally:
        notes.close()

    assert snapshot is not None
    assert sorted(path.name for path in snapshot.iterdir()) == [
        "manifest.json",
        "notes.db",
        "tasks.db",
    ]
    payload = _manifest(snapshot)
    assert set(payload) == {
        "manifest_version",
        "snapshot_id",
        "reason",
        "created_at",
        "vbot_version",
        "sqlite_version",
        "sqlite_source_id",
        "members",
        "complete",
    }
    assert set(payload["members"]) == {"notes", "tasks"}
    member = payload["members"]["notes"]
    assert set(member) == {
        "file",
        "database_id",
        "application_id",
        "format_generation",
        "file_size",
        "sha256",
        "integrity",
        "foreign_key_check",
        "migrations",
        "facts",
    }
    marker = read_marker(data_dir)
    assert marker is not None
    assert member["database_id"] == marker.databases["notes"].database_id
    assert member["facts"] == {"note_count": 1}
    assert payload["members"]["tasks"]["facts"] == {"note_count": 1}
    assert list_data_snapshots(data_dir, specs=_specs(data_dir)) == [snapshot]
    assert read_snapshot_health(data_dir)["state"] == "healthy"
    summary = snapshot_summaries(data_dir)[0]
    assert summary["snapshot_id"] == snapshot.name
    assert "open during the snapshot" not in json.dumps(summary)


def test_a_snapshot_copy_opens_as_a_standalone_database(data_dir: Path, tmp_path: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "hello")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    (copy_dir / "notes.db").write_bytes((snapshot / "notes.db").read_bytes())

    from core.database import open_offline_database

    database = open_offline_database(notes_spec(copy_dir))
    try:
        with database.read() as connection:
            assert connection.execute("SELECT body FROM notes").fetchone()[0] == "hello"
    finally:
        database.close()


def test_snapshot_without_registered_databases_is_a_noop(data_dir: Path) -> None:
    assert create_data_snapshot(data_dir, reason="test") is None
    assert list_data_snapshots(data_dir) == []


def test_snapshot_reason_must_be_named(data_dir: Path) -> None:
    with pytest.raises(ValueError):
        create_data_snapshot(data_dir, reason=" ")


@pytest.mark.parametrize("invalid_identity", [False, True])
def test_file_snapshot_copies_committed_wal_content_and_checks_the_marker(
    data_dir: Path, invalid_identity: bool
) -> None:
    if required_journal_mode(sqlite3.sqlite_version_info) != "wal":
        pytest.skip("This SQLite build cannot safely write WAL")
    database = open_database(notes_spec(data_dir))
    add_note(database, "retained")
    database.close()
    if invalid_identity:
        _write_marker(data_dir, {"notes": MarkerEntry("0" * 32, 1)})
    with closing(sqlite3.connect(database.path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        # A committed relation unknown to the declaration remains in the WAL.
        writer.execute("CREATE TABLE snapshot_probe(value TEXT NOT NULL)")
        writer.execute("INSERT INTO snapshot_probe VALUES ('committed in WAL')")
        writer.commit()
        snapshot = create_data_snapshot(data_dir, reason="update")
        if invalid_identity:
            assert snapshot is None
            assert list_data_snapshots(data_dir) == []
            assert read_snapshot_health(data_dir)["state"] == "degraded"
        else:
            assert snapshot is not None
            with closing(sqlite3.connect(snapshot / "notes.db")) as saved:
                assert saved.execute("SELECT value FROM snapshot_probe").fetchone() == (
                    "committed in WAL",
                )
        assert writer.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_a_registered_database_that_is_missing_fails_the_snapshot(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "first")
    open_database(notes_spec(data_dir, name="tasks")).close()
    notes_spec(data_dir, name="tasks").path.unlink()

    assert create_data_snapshot(data_dir, reason="test") is None
    health = read_snapshot_health(data_dir)
    assert health["state"] == "degraded"
    assert "tasks" in str(health["reason"])
    assert not [path for path in snapshot_root(data_dir).iterdir() if path.name.startswith(".")]


@pytest.mark.parametrize("suffix", [b"\x1a", b"\r\n\x1a", b"\x00\xff"])
def test_snapshot_fsync_preserves_binary_file(tmp_path: Path, suffix: bytes) -> None:
    path = tmp_path / "notes.db"
    original = bytes(range(256)) + suffix
    path.write_bytes(original)

    snapshots_module.fsync_file(path)

    assert path.read_bytes() == original


def test_an_unexpected_manifest_key_is_not_a_snapshot(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")
    _rewrite_manifest(snapshot, lambda payload: payload.update(unexpected=True))

    assert list_data_snapshots(data_dir) == []
    assert snapshot_inventory(data_dir) == []


def _member(field: str, value: object) -> Callable[[dict[str, Any]], None]:
    def change(payload: dict[str, Any]) -> None:
        if field == "fact":
            payload["members"]["notes"]["facts"]["note_count"] = value
        else:
            payload["members"]["notes"][field] = value

    return change


@pytest.mark.parametrize(
    "change",
    [
        _member("database_id", "0" * 32),
        _member("fact", 999),
        _member("sha256", "0" * 64),
        _member("file_size", 1),
        _member("migrations", ["notes.unknown"]),
        _member("format_generation", 2),
    ],
    ids=["database-id", "fact", "sha256", "file-size", "migrations", "generation"],
)
def test_verification_rejects_manifest_database_disagreement(
    data_dir: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")
    _rewrite_manifest(snapshot, change)

    assert list_data_snapshots(data_dir, specs=_specs(data_dir)) == []
    assert snapshot_summaries(data_dir, specs=_specs(data_dir)) == []


def test_a_snapshot_of_another_data_directory_is_excluded(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")
    marker = read_marker(data_dir)
    assert marker is not None
    expected = {"notes": marker.databases["notes"].database_id}

    assert list_data_snapshots(data_dir, expected=expected) == [snapshot]
    assert list_data_snapshots(data_dir, expected={"notes": "f" * 32}) == []
    assert (
        member_restore_candidates(
            data_dir, "notes", database_id="f" * 32, spec=notes_spec(data_dir)
        )
        == []
    )


@pytest.mark.parametrize(
    ("older_time", "newer_time"),
    [
        ("2026-09-01T10:00:00Z", "2026-09-01T10:00:00.100000Z"),
        ("2026-09-01T12:00:00+03:00", "2026-09-01T10:00:00Z"),
    ],
)
def test_snapshot_ordering_and_retention_use_timestamp_instants(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, older_time: str, newer_time: str
) -> None:
    older = snapshot_with_notes(data_dir, "one")
    newer = snapshot_with_notes(data_dir, "two")
    for snapshot, created_at in ((older, older_time), (newer, newer_time)):

        def stamp(payload: dict[str, Any], value: str = created_at) -> None:
            payload["created_at"] = value

        _rewrite_manifest(snapshot, stamp)

    assert list_data_snapshots(data_dir) == [newer, older]
    assert [item["snapshot_id"] for item in snapshot_inventory(data_dir)] == [
        newer.name,
        older.name,
    ]
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 2)
    published = snapshot_with_notes(data_dir, "three")
    assert published.exists()
    assert older.exists() is False
    assert newer.exists() is True


@pytest.mark.parametrize(
    "error_message", ["database is locked", "disk I/O error", "unable to open database file"]
)
def test_snapshot_verification_preserves_operational_failures(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, error_message: str
) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError(error_message)

    monkeypatch.setattr(snapshots_module.sqlite3, "connect", unavailable)
    with pytest.raises(DatabaseUnavailableError):
        list_data_snapshots(data_dir)
    assert snapshot.is_dir()


@pytest.mark.parametrize("failed_operation", ["stat", "read_text", "open"])
def test_snapshot_verification_preserves_file_access_failures(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, failed_operation: str
) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")
    target = snapshot / ("notes.db" if failed_operation == "open" else SNAPSHOT_MANIFEST_NAME)
    original = getattr(Path, failed_operation)

    def unavailable(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == target:
            raise PermissionError("injected inaccessible snapshot")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, failed_operation, unavailable)
    with pytest.raises(DatabaseUnavailableError):
        list_data_snapshots(data_dir)


def test_snapshot_failure_keeps_the_previous_verified_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = open_database(notes_spec(data_dir))
    try:
        add_note(database, "hello")
        first = create_data_snapshot(data_dir, reason="test", databases=(database,))
        assert first is not None

        def failing_backup(*_args: Any, **_kwargs: Any) -> bool:
            raise DatabaseUnavailableError("disk busy")

        monkeypatch.setattr(database, "backup", failing_backup)
        assert create_data_snapshot(data_dir, reason="test", databases=(database,)) is None
        assert list_data_snapshots(data_dir) == [first]
        assert read_snapshot_health(data_dir)["state"] == "degraded"
        assert data_store_status(data_dir, databases=(database,))["state"] == "snapshot_degraded"
    finally:
        database.close()


def test_a_cancelled_snapshot_publishes_nothing(data_dir: Path) -> None:
    database = open_database(notes_spec(data_dir))
    try:
        add_note(database, "hello")
        assert (
            create_data_snapshot(
                data_dir, reason="test", databases=(database,), cancelled=lambda: True
            )
            is None
        )
    finally:
        database.close()
    assert list_data_snapshots(data_dir) == []


def test_retention_prunes_only_after_a_verified_publish(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 2)
    for index in range(4):
        snapshot_with_notes(data_dir, f"note {index}")

    assert len(list_data_snapshots(data_dir)) == 2


def test_retention_does_not_reverify_retained_snapshots(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_with_notes(data_dir, "hello")
    sha256_calls = 0
    verification_calls = 0
    real_sha256 = snapshots_module._sha256
    real_verify = snapshots_module.verify_database_file

    def counted_sha256(path: Path, **kwargs: Any) -> str:
        nonlocal sha256_calls
        sha256_calls += 1
        return real_sha256(path, **kwargs)

    def counted_verify(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal verification_calls
        verification_calls += 1
        return real_verify(path, *args, **kwargs)

    monkeypatch.setattr(snapshots_module, "_sha256", counted_sha256)
    monkeypatch.setattr(snapshots_module, "verify_database_file", counted_verify)

    snapshot_with_notes(data_dir, "again")

    assert sha256_calls == 1
    assert verification_calls == 1


def test_retention_always_keeps_the_just_published_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = snapshot_with_notes(data_dir, "hello")
    _rewrite_manifest(first, lambda payload: payload.update(created_at="2099-01-01T00:00:00Z"))
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 1)

    published = snapshot_with_notes(data_dir, "again")

    assert published.is_dir()
    assert first.exists() is False
    assert list_data_snapshots(data_dir) == [published]


def test_retention_enforces_the_byte_limit_around_the_published_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = snapshot_with_notes(data_dir, "hello")
    snapshot_size = (first / "notes.db").stat().st_size
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_BYTES", snapshot_size * 2)

    snapshot_with_notes(data_dir)
    published = snapshot_with_notes(data_dir)

    retained = list_data_snapshots(data_dir)
    assert published in retained
    assert len(retained) == 2
    assert sum((path / "notes.db").stat().st_size for path in retained) <= snapshot_size * 2


def test_retention_leaves_malformed_directories_untouched(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = snapshot_root(data_dir) / "20260101T000000Z-deadbeef"
    malformed.mkdir(parents=True)
    (malformed / SNAPSHOT_MANIFEST_NAME).write_text("{", encoding="utf-8")
    (malformed / "notes.db").write_bytes(b"evidence")
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 1)

    published = snapshot_with_notes(data_dir, "hello")

    assert published.is_dir()
    assert (malformed / "notes.db").read_bytes() == b"evidence"


def test_restore_candidates_are_per_member_and_newest_first(data_dir: Path) -> None:
    older = snapshot_with_notes(data_dir, "one")
    open_database(notes_spec(data_dir, name="tasks")).close()
    newer = snapshot_with_notes(data_dir, "two")
    marker = read_marker(data_dir)
    assert marker is not None

    notes_candidates = member_restore_candidates(
        data_dir, "notes", database_id=marker.databases["notes"].database_id
    )
    tasks_candidates = member_restore_candidates(
        data_dir, "tasks", database_id=marker.databases["tasks"].database_id
    )

    assert [path for path, _manifest, _member in notes_candidates] == [newer, older]
    assert [path for path, _manifest, _member in tasks_candidates] == [newer]
    # Damage to one member leaves the other member of the same snapshot usable.
    (newer / "tasks.db").write_bytes(b"damaged")
    assert [
        path
        for path, _manifest, _member in member_restore_candidates(
            data_dir, "notes", database_id=marker.databases["notes"].database_id
        )
    ] == [newer, older]
    assert list_data_snapshots(data_dir) == [older]
