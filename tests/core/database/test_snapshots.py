"""Data snapshots: capture, strict manifests, per-member verification and retention."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import closing
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from core.database import (
    ANCHOR_CAPTURE,
    TRAILING_CAPTURE,
    Database,
    DatabaseSchemaMismatchError,
    DatabaseUnavailableError,
    MemberFrozenError,
    SnapshotBarrier,
    SnapshotFacts,
    create_data_snapshot,
    data_store_status,
    list_data_snapshots,
    open_database,
    open_offline_database,
    read_incident,
    read_marker,
    read_snapshot_health,
    required_journal_mode,
    restore_data_snapshot,
    snapshot_root,
    snapshot_summaries,
)
from core.database import snapshots as snapshots_module
from core.database.snapshot_barrier import CAPTURE_ATTEMPTS, capture_members
from core.database.snapshots import SNAPSHOT_HEALTH_FILE_NAME, SNAPSHOT_MANIFEST_NAME
from core.json_documents import document_change
from tests.core.database.database_test_support import (
    NOTES_FACTS,
    NOTES_SCHEMA_SQL,
    add_note,
    add_notes,
    manifest_payload,
    note_bodies,
    note_count,
    notes_spec,
    pin_journal_mode,
    rewrite_manifest,
    rewrite_marker,
    snapshot_with_notes,
    stored_bodies,
    write_document,
)


def _specs(data_dir: Path) -> dict[str, Any]:
    return {"notes": notes_spec(data_dir), "tasks": notes_spec(data_dir, name="tasks")}


def _status_snapshot_ids(data_dir: Path) -> list[str]:
    """The snapshots the status lists from their manifests, without rehashing."""
    return [item["snapshot_id"] for item in data_store_status(data_dir)["snapshots"]]


def test_a_snapshot_captures_every_registered_database_as_one_standalone_member_each(
    data_dir: Path, tmp_path: Path
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
    payload = manifest_payload(snapshot)
    assert set(payload) == {
        "manifest_version",
        "snapshot_id",
        "reason",
        "created_at",
        "vbot_version",
        "sqlite_version",
        "sqlite_source_id",
        "members",
        "documents",
        "complete",
    }
    assert payload["documents"] == {}
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
    # Each member copy opens on its own, outside any data directory.
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    (copy_dir / "notes.db").write_bytes((snapshot / "notes.db").read_bytes())
    copy = open_offline_database(notes_spec(copy_dir))
    try:
        assert note_bodies(copy) == ["open during the snapshot"]
    finally:
        copy.close()


def _write_without_patience(database: Database) -> None:
    # A write that meets a lock held by the copy fails at once as busy.
    database.write(
        lambda connection: connection.execute("INSERT INTO notes (body) VALUES ('overlap')"),
        patience_s=0.0,
    )


@pytest.mark.parametrize("journal_mode", ["wal", "delete"])
def test_an_online_snapshot_is_one_consistent_copy_while_another_thread_keeps_writing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, journal_mode: str
) -> None:
    pin_journal_mode(monkeypatch, journal_mode)
    # SQLite lock waits fail at once, so only a write that never meets one can succeed.
    monkeypatch.setattr("core.database._runtime.BUSY_TIMEOUT_MS", 0)
    # The anchor, like the Session database, keeps taking writes while it is copied.
    database = open_database(notes_spec(data_dir, snapshot_capture=ANCHOR_CAPTURE))
    writer = ThreadPoolExecutor(max_workers=1)
    overlapping: list[Future[None]] = []
    committed_during_copy: list[bool] = []
    try:
        add_notes(database, 5000)  # Enough rows for several progress polls of the copy.
        assert database.wal_active() is (journal_mode == "wal")
        idle_connections = database.live_connection_count()

        def write_during_the_copy() -> bool:
            # The copy reads through its own connection beside this database's.
            if database.live_connection_count() > idle_connections and not overlapping:
                overlapping.append(writer.submit(_write_without_patience, database))
                try:
                    # WAL commits while the copy keeps reading its snapshot; the
                    # rollback journal queues the write until the copy finishes.
                    overlapping[0].result(timeout=10.0 if journal_mode == "wal" else 0.05)
                    committed_during_copy.append(True)
                except FutureTimeoutError:
                    committed_during_copy.append(False)
            return False

        published = create_data_snapshot(
            data_dir, reason="test", databases=(database,), cancelled=write_during_the_copy
        )
        assert overlapping, "the copy never polled while it was reading"
        overlapping[0].result(timeout=10.0)
        assert note_count(database) == 5001
    finally:
        writer.shutdown(wait=True)
        database.close()

    assert committed_during_copy == [journal_mode == "wal"]
    assert isinstance(published, Path), read_snapshot_health(data_dir)
    assert list_data_snapshots(data_dir) == [published]
    # A standalone rollback-journal copy of one instant, without sidecars.
    assert {path.name for path in published.iterdir()} == {"notes.db", SNAPSHOT_MANIFEST_NAME}
    assert manifest_payload(published)["members"]["notes"]["facts"] == {"note_count": 5000}


@pytest.mark.asyncio
async def test_a_capture_waits_for_compound_mutations_in_flight_and_holds_off_new_ones() -> None:
    barrier = SnapshotBarrier()
    order: list[str] = []
    mutating, capture_waiting, finish_mutation = Event(), Event(), Event()
    captured, finish_capture = Event(), Event()

    def mutation() -> None:
        with barrier.compound_mutation():
            mutating.set()
            assert finish_mutation.wait(10.0)
            # A nested entry never waits on the capture that waits for this mutation.
            with barrier.compound_mutation():
                order.append("mutation")

    def still_waiting() -> bool:
        capture_waiting.set()  # consulted only while the capture waits
        return False

    def capture() -> None:
        with barrier.capture(cancelled=still_waiting) as held:
            assert held
            order.append("capture")
            captured.set()
            assert finish_capture.wait(10.0)

    async def new_mutation() -> None:
        async with barrier.compound_mutation_async():
            order.append("new mutation")

    with ThreadPoolExecutor(max_workers=2) as threads:
        in_flight = threads.submit(mutation)
        assert await asyncio.to_thread(mutating.wait, 10.0)
        capturing = threads.submit(capture)
        assert await asyncio.to_thread(capture_waiting.wait, 10.0)
        finish_mutation.set()
        assert await asyncio.to_thread(captured.wait, 10.0)
        arriving = asyncio.create_task(new_mutation())
        await asyncio.sleep(0)  # One loop turn: the new mutation runs up to its wait.
        assert not arriving.done()
        finish_capture.set()
        await arriving
        in_flight.result(timeout=10.0)
        capturing.result(timeout=10.0)
    assert order == ["mutation", "capture", "new mutation"]


def test_a_capture_freezes_held_members_until_the_anchor_is_copied(data_dir: Path) -> None:
    notes = open_database(notes_spec(data_dir))
    journal = open_database(notes_spec(data_dir, name="journal", snapshot_capture=ANCHOR_CAPTURE))
    tally = open_database(notes_spec(data_dir, name="tally", snapshot_capture=TRAILING_CAPTURE))
    document = data_dir / "settings.json"
    inside, draining, late_ran = Event(), Event(), Event()
    order: list[str] = []
    late: list[Future[None]] = []

    def in_flight(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO notes (body) VALUES ('in flight')")
        inside.set()
        assert draining.wait(10.0)
        # A document change inside this held write is admitted at once, although
        # the capture already holds off new held changes and waits for this one.
        with document_change(document):
            document.write_text("{}", encoding="utf-8")

    def still_draining() -> bool:
        draining.set()  # consulted only while the capture waits for held changes
        return False

    def late_note(connection: sqlite3.Connection) -> None:
        late_ran.set()
        connection.execute("INSERT INTO notes (body) VALUES ('late')")

    def change_without_waiting() -> None:
        with document_change(document, wait=False):
            pytest.fail("a change that must not wait entered during the freeze")

    def copy_database(name: str) -> None:
        order.append(name)
        if name == "journal":
            late.append(threads.submit(notes.write, late_note))
            # The anchor keeps taking writes; the held member does not.
            threads.submit(add_note, journal, "live").result(timeout=10.0)
            assert not late_ran.is_set()
            # A change that must not wait is refused instead.
            with pytest.raises(MemberFrozenError):
                threads.submit(change_without_waiting).result(timeout=10.0)
            assert note_bodies(notes) == ["in flight"]

    try:
        with ThreadPoolExecutor(max_workers=4) as threads:
            writing = threads.submit(notes.write, in_flight)
            assert inside.wait(10.0)
            capture = threads.submit(
                capture_members,
                data_dir,
                {"tally": tally.spec, "journal": journal.spec, "notes": notes.spec},
                copy_database=copy_database,
                copy_documents=lambda: order.append("documents"),
                discard_copies=lambda: pytest.fail("no change entered during the freeze"),
                cancelled=still_draining,
            ).result(timeout=20.0)
            writing.result(timeout=10.0)
            late[0].result(timeout=10.0)
        assert capture is not None
        assert order == ["notes", "documents", "journal", "tally"]
        assert note_bodies(notes) == ["in flight", "late"]
        assert note_bodies(journal) == ["live"]
    finally:
        for database in (notes, journal, tally):
            database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("disturbed_attempts", [1, CAPTURE_ATTEMPTS])
async def test_a_capture_an_event_loop_write_disturbed_is_retried_a_bounded_number_of_times(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, disturbed_attempts: int
) -> None:
    notes = open_database(notes_spec(data_dir))
    real_backup = notes.backup
    copying: queue.Queue[Event] = queue.Queue()
    disturbed: list[Path] = []

    def backup(destination: Path, *, cancelled: Callable[[], bool] | None = None) -> bool:
        if len(disturbed) < disturbed_attempts:
            disturbed.append(destination)
            written = Event()
            copying.put(written)
            assert written.wait(10.0)
        return real_backup(destination, cancelled=cancelled)

    monkeypatch.setattr(notes, "backup", backup)
    try:
        taking = asyncio.ensure_future(
            asyncio.to_thread(create_data_snapshot, data_dir, reason="test", databases=(notes,))
        )
        for attempt in range(disturbed_attempts):
            written = await asyncio.to_thread(copying.get, timeout=10.0)
            # A held write on the Event Loop never waits, even while the members are frozen.
            add_note(notes, f"from the Event Loop {attempt}")
            written.set()
        published = await taking
        assert note_count(notes) == disturbed_attempts
    finally:
        notes.close()

    if disturbed_attempts < CAPTURE_ATTEMPTS:
        # The disturbed copies were discarded; the next attempt holds every write.
        assert isinstance(published, Path)
        assert manifest_payload(published)["members"]["notes"]["facts"] == {
            "note_count": disturbed_attempts
        }
        assert sorted(path.name for path in snapshot_root(data_dir).iterdir()) == sorted(
            [published.name, SNAPSHOT_HEALTH_FILE_NAME]
        )
    else:
        assert published is None
        assert list_data_snapshots(data_dir) == []
        assert "retry the snapshot" in read_snapshot_health(data_dir)["reason"]


def test_an_online_snapshot_never_copies_inside_a_compound_mutation(data_dir: Path) -> None:
    barrier = SnapshotBarrier(capture_wait_seconds=0.0)
    database = open_database(notes_spec(data_dir))
    try:
        with barrier.compound_mutation():
            add_note(database, "renamed")
            halfway = create_data_snapshot(
                data_dir, reason="test", databases=(database,), barrier=barrier
            )
            write_document(data_dir, "agents/renamed/agent.json", '{"format_version": 1}\n')
        health = read_snapshot_health(data_dir)
        published = create_data_snapshot(
            data_dir, reason="test", databases=(database,), barrier=barrier
        )
    finally:
        database.close()

    # The attempt fails once the mutation outlasts the barrier's wait budget.
    assert halfway is None
    assert health["state"] == "degraded"
    assert health["reason"].startswith(DatabaseUnavailableError.__name__)
    assert isinstance(published, Path)
    manifest = manifest_payload(published)
    assert manifest["members"]["notes"]["facts"] == {"note_count": 1}
    assert list(manifest["documents"]) == ["agents/renamed/agent.json"]


@pytest.mark.parametrize("observed_at", ["2026-09-01T10:00:00Z", 17])
def test_a_health_record_with_a_non_canonical_time_is_malformed(
    data_dir: Path, observed_at: object
) -> None:
    snapshot_with_notes(data_dir, "hello")
    health_path = snapshot_root(data_dir) / SNAPSHOT_HEALTH_FILE_NAME
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    assert read_snapshot_health(data_dir) == payload
    health_path.write_text(json.dumps({**payload, "observed_at": observed_at}), encoding="utf-8")

    health = read_snapshot_health(data_dir)

    assert health["state"] == "degraded"
    assert health["observed_at"] is None


def test_a_snapshot_needs_a_reason_and_captures_nothing_without_a_registered_database(
    data_dir: Path,
) -> None:
    with pytest.raises(ValueError):
        create_data_snapshot(data_dir, reason=" ")
    assert create_data_snapshot(data_dir, reason="test") is None
    assert list_data_snapshots(data_dir) == []


@pytest.mark.parametrize("invalid_identity", [False, True])
def test_a_file_snapshot_copies_committed_wal_content_and_checks_the_marker(
    data_dir: Path, invalid_identity: bool
) -> None:
    if required_journal_mode(sqlite3.sqlite_version_info) != "wal":
        pytest.skip("This SQLite build cannot safely write WAL")
    database = open_database(notes_spec(data_dir))
    add_note(database, "retained")
    database.close()
    if invalid_identity:

        def foreign(payload: dict[str, Any]) -> None:
            payload["databases"]["notes"]["database_id"] = "0" * 32

        rewrite_marker(data_dir, foreign)
    with closing(sqlite3.connect(database.path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        # A committed relation unknown to the declaration remains in the WAL.
        writer.execute("CREATE TABLE snapshot_probe(value TEXT NOT NULL)")
        writer.execute("INSERT INTO snapshot_probe (value) VALUES ('committed in WAL')")
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


@pytest.mark.parametrize(
    ("name", "next_step"),
    [
        ("tasks", "starting vBot restores it from the newest verified data snapshot"),
        ("ext.demo.tasks", "`vbot data-store unregister ext.demo.tasks --yes` releases it"),
    ],
    ids=["core", "extension"],
)
def test_a_registered_database_that_is_missing_fails_the_snapshot_with_a_next_step(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, name: str, next_step: str
) -> None:
    retained = snapshot_with_notes(data_dir, "first")
    open_database(notes_spec(data_dir, name=name)).close()
    notes_spec(data_dir, name=name).path.unlink()
    # Retention would otherwise prune the retained copies of the missing database.
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 1)

    assert create_data_snapshot(data_dir, reason="test") is None
    health = read_snapshot_health(data_dir)
    assert health["state"] == "degraded"
    assert f"the registered {'Extension ' if name.startswith('ext.') else ''}database {name}" in (
        str(health["reason"])
    )
    assert next_step in str(health["reason"])
    assert list_data_snapshots(data_dir) == [retained]
    assert not [path for path in snapshot_root(data_dir).iterdir() if path.name.startswith(".")]
    status = data_store_status(data_dir)
    assert status["state"] == "unavailable"
    assert next_step in status["databases"][name]["reason"]


def test_an_older_vbot_restores_from_a_manifest_with_fields_a_newer_one_added(
    data_dir: Path,
) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")

    def newer(payload: dict[str, Any]) -> None:
        payload["compression"] = {"algorithm": "none", "levels": [0]}
        payload["members"]["notes"]["page_size"] = 4096

    rewrite_manifest(snapshot, newer)

    assert list_data_snapshots(data_dir, specs=_specs(data_dir)) == [snapshot]
    assert _status_snapshot_ids(data_dir) == [snapshot.name]
    notes_spec(data_dir).path.write_bytes(b"damaged")
    assert stored_bodies(notes_spec(data_dir)) == ["retained"]
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["restored_snapshot_id"] == snapshot.name


@pytest.mark.parametrize(
    "change",
    [
        lambda payload: payload.update(manifest_version=2),
        lambda payload: payload.pop("reason"),
        lambda payload: payload["members"]["notes"].pop("facts"),
        # Valid ISO 8601, but a manifest is written canonical: anything else is damage.
        lambda payload: payload.update(created_at="2026-09-01T10:00:00Z"),
        lambda payload: payload.update(created_at="2026-09-01T12:00:00.000000+02:00"),
    ],
    ids=[
        "newer-version",
        "missing-field",
        "missing-member-field",
        "non-canonical-created-at",
        "offset-created-at",
    ],
)
def test_a_newer_damaged_or_incomplete_manifest_is_not_a_snapshot(
    data_dir: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")
    rewrite_manifest(snapshot, change)

    assert list_data_snapshots(data_dir) == []
    assert _status_snapshot_ids(data_dir) == []


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
    ],
    ids=["database-id", "fact", "sha256", "file-size", "migrations"],
)
def test_verification_rejects_manifest_database_disagreement(
    data_dir: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")
    rewrite_manifest(snapshot, change)

    assert list_data_snapshots(data_dir, specs=_specs(data_dir)) == []
    assert snapshot_summaries(data_dir, specs=_specs(data_dir)) == []


def test_a_snapshot_of_another_data_directory_is_excluded(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")
    marker = read_marker(data_dir)
    assert marker is not None
    expected = {"notes": marker.databases["notes"].database_id}

    assert list_data_snapshots(data_dir, expected=expected) == [snapshot]
    assert list_data_snapshots(data_dir, expected={"notes": "f" * 32}) == []


def test_snapshot_ordering_and_retention_use_the_manifest_creation_time(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Created later but stamped earlier: the manifest time decides the order.
    newer = snapshot_with_notes(data_dir, "two")
    older = snapshot_with_notes(data_dir, "one")
    for snapshot, created_at in (
        (older, "2026-09-01T10:00:00.000000Z"),
        (newer, "2026-09-01T10:00:00.100000Z"),
    ):

        def stamp(payload: dict[str, Any], value: str = created_at) -> None:
            payload["created_at"] = value

        rewrite_manifest(snapshot, stamp)

    assert list_data_snapshots(data_dir) == [newer, older]
    assert _status_snapshot_ids(data_dir) == [newer.name, older.name]
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 2)
    published = snapshot_with_notes(data_dir, "three")
    assert published.exists()
    assert older.exists() is False
    assert newer.exists() is True


@pytest.mark.parametrize("error_message", ["database is locked", "disk I/O error"])
def test_snapshot_verification_preserves_operational_failures(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, error_message: str
) -> None:
    snapshot = snapshot_with_notes(data_dir, "retained")

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError(error_message)

    monkeypatch.setattr(sqlite3, "connect", unavailable)
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


def test_a_failed_or_cancelled_snapshot_keeps_the_previous_verified_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = open_database(notes_spec(data_dir))
    try:
        add_note(database, "hello")
        first = create_data_snapshot(data_dir, reason="test", databases=(database,))
        assert first is not None
        cancelled = create_data_snapshot(
            data_dir, reason="test", databases=(database,), cancelled=lambda: True
        )
        assert cancelled is None
        assert read_snapshot_health(data_dir)["state"] == "healthy"

        def failing_backup(*_args: Any, **_kwargs: Any) -> bool:
            raise DatabaseUnavailableError("disk busy")

        with monkeypatch.context() as patched:
            patched.setattr(database, "backup", failing_backup)
            with caplog.at_level(logging.INFO, logger="vbot.database"):
                for _attempt in range(2):
                    assert (
                        create_data_snapshot(data_dir, reason="test", databases=(database,)) is None
                    )
            assert list_data_snapshots(data_dir) == [first]
            assert read_snapshot_health(data_dir)["state"] == "degraded"
            assert (
                data_store_status(data_dir, databases=(database,))["state"] == "snapshot_degraded"
            )
        # The degradation is a transition: a repeated failure does not log it again.
        assert [record.levelno for record in caplog.records] == [logging.WARNING]
        caplog.clear()

        with caplog.at_level(logging.INFO, logger="vbot.database"):
            recovered = create_data_snapshot(data_dir, reason="test", databases=(database,))
        assert recovered is not None
        assert read_snapshot_health(data_dir)["state"] == "healthy"
        assert {record.levelno for record in caplog.records} == {logging.INFO}
        assert any("recovered" in record.getMessage() for record in caplog.records)
    finally:
        database.close()
    assert not [path for path in snapshot_root(data_dir).iterdir() if path.name.startswith(".")]


def test_retention_prunes_only_after_a_verified_publish(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_KEEP_COUNT", 2)
    for index in range(4):
        snapshot_with_notes(data_dir, f"note {index}")

    assert len(list_data_snapshots(data_dir)) == 2


def test_publishing_never_reads_the_retained_snapshots_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = snapshot_with_notes(data_dir, "hello")
    member = retained / "notes.db"
    real_open = Path.open
    real_connect = sqlite3.connect

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == member:
            raise PermissionError("injected unreadable retained member")
        return real_open(path, *args, **kwargs)

    def unconnectable(database: Any, *args: Any, **kwargs: Any) -> Any:
        if retained.name in str(database):
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    # Retention works from manifests and file sizes: multi-GB copies are never rehashed.
    with monkeypatch.context() as patched:
        patched.setattr(Path, "open", unreadable)
        patched.setattr(sqlite3, "connect", unconnectable)
        published = snapshot_with_notes(data_dir, "again")
        assert read_snapshot_health(data_dir)["state"] == "healthy"
        # Full verification would have read it.
        with pytest.raises(DatabaseUnavailableError):
            list_data_snapshots(data_dir)

    assert list_data_snapshots(data_dir) == [published, retained]


def test_retention_always_keeps_the_just_published_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = snapshot_with_notes(data_dir, "hello")
    rewrite_manifest(
        first, lambda payload: payload.update(created_at="2099-01-01T00:00:00.000000Z")
    )
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


def test_a_restore_takes_the_newest_snapshot_whose_own_member_verifies(data_dir: Path) -> None:
    older = snapshot_with_notes(data_dir, "one")
    open_database(notes_spec(data_dir, name="tasks")).close()
    newer = snapshot_with_notes(data_dir, "two")
    # Only the newer snapshot holds a tasks member.
    notes_spec(data_dir, name="tasks").path.write_bytes(b"damaged")
    assert stored_bodies(notes_spec(data_dir, name="tasks")) == []
    tasks_incident = read_incident(data_dir, "tasks")
    assert tasks_incident is not None
    assert tasks_incident["restored_snapshot_id"] == newer.name
    # Damage to one member hides the whole snapshot but leaves its other member usable.
    (newer / "tasks.db").write_bytes(b"damaged")
    assert list_data_snapshots(data_dir) == [older]

    notes_spec(data_dir).path.write_bytes(b"damaged")

    assert stored_bodies(notes_spec(data_dir)) == ["one", "two"]
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["restored_snapshot_id"] == newer.name


def test_an_older_member_is_verified_only_against_the_facts_it_recorded(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    # A later vBot adds a table and a fact over it; the older member lacks both.
    grown = notes_spec(
        data_dir,
        schema_sql=NOTES_SCHEMA_SQL + "\nCREATE TABLE labels (label TEXT PRIMARY KEY) STRICT;",
        snapshot_facts=SnapshotFacts(
            {**NOTES_FACTS.queries, "label_count": "SELECT COUNT(*) FROM labels"}
        ),
    )
    grown.path.write_bytes(b"damaged")

    assert stored_bodies(grown) == ["saved"]
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["restored_snapshot_id"] == snapshot.name


def test_a_recorded_fact_this_vbot_cannot_compute_is_a_schema_mismatch(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    changed = notes_spec(
        data_dir, snapshot_facts=SnapshotFacts({"note_count": "SELECT COUNT(*) FROM missing"})
    )

    with pytest.raises(DatabaseSchemaMismatchError, match="snapshot fact note_count"):
        restore_data_snapshot(data_dir, snapshot, specs=(changed,), check_only=True)
    assert list_data_snapshots(data_dir, specs={"notes": changed}) == []
    # Automatic restore treats the member as unusable and finds no other copy.
    changed.path.unlink()
    with pytest.raises(DatabaseUnavailableError, match="no verified data snapshot could restore"):
        open_database(changed)
