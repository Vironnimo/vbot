"""The updater's pre-update data snapshot and its guarded automatic rollback."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from core.database import (
    DatabaseFormatError,
    DatabaseUnavailableError,
    UpdateRollbackRefusedError,
    UpdateSnapshot,
    begin_maintenance,
    create_data_snapshot,
    create_update_snapshot,
    data_changed_since,
    find_update_snapshot,
    open_database,
    read_incident,
    read_maintenance,
    read_marker,
    restore_data_snapshot,
    restore_update_snapshot,
)
from core.database import recovery as recovery_module
from core.database.marker import MarkerEntry, _write_marker
from core.database.recovery import quarantine_root
from core.database.snapshots import SNAPSHOT_MANIFEST_NAME, snapshot_root
from core.database.update_rollback import UPDATE_ROLLBACK_CAUSE, data_stamp
from tests.core.database.database_test_support import (
    add_note,
    notes_spec,
    stored_bodies,
)

_SETTINGS = '{"format_version": 1, "theme": "dark"}\n'
_AGENT = '{"format_version": 1, "id": "main"}\n'


def _write(data_dir: Path, relative: str, text: str) -> Path:
    path = data_dir.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _prepared(data_dir: Path) -> None:
    """Two registered databases and two documents, as a stopped server leaves them."""
    for name in ("notes", "tasks"):
        database = open_database(notes_spec(data_dir, name=name))
        add_note(database, f"saved {name}")
        database.close()
    _write(data_dir, "settings.json", _SETTINGS)
    _write(data_dir, "agents/main/agent.json", _AGENT)


def _snapshot(data_dir: Path, operation_id: str = "upd_1") -> UpdateSnapshot:
    _prepared(data_dir)
    snapshot = create_update_snapshot(data_dir, operation_id=operation_id)
    assert snapshot is not None
    return snapshot


def _candidate_writes(data_dir: Path) -> None:
    """What a failed candidate may leave behind: rows, documents, a new database."""
    database = open_database(notes_spec(data_dir))
    add_note(database, "written by the candidate")
    database.close()
    _write(data_dir, "settings.json", '{"format_version": 2}\n')
    (data_dir / "agents" / "main" / "agent.json").unlink()
    _write(data_dir, "mcp/connections.json", '{"format_version": 1, "connections": []}\n')
    open_database(notes_spec(data_dir, name="journal")).close()


def test_the_update_snapshot_is_bound_to_its_operation_and_captures_everything(
    data_dir: Path,
) -> None:
    snapshot = _snapshot(data_dir)

    manifest = json.loads(
        (snapshot_root(data_dir) / snapshot.snapshot_id / SNAPSHOT_MANIFEST_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["reason"] == "update upd_1"
    assert sorted(manifest["members"]) == ["notes", "tasks"]
    assert sorted(manifest["documents"]) == ["agents/main/agent.json", "settings.json"]
    assert set(snapshot.member_hashes) == {"notes", "tasks"}
    assert data_changed_since(data_dir, snapshot) is None
    assert find_update_snapshot(data_dir, "upd_1") == snapshot.snapshot_id
    assert find_update_snapshot(data_dir, "upd_other") is None


def test_no_registered_database_needs_no_update_snapshot(data_dir: Path) -> None:
    _write(data_dir, "settings.json", _SETTINGS)

    assert create_update_snapshot(data_dir, operation_id="upd_1") is None


def test_databases_without_a_marker_refuse_the_update_snapshot(tmp_path: Path) -> None:
    (tmp_path / "sessions.db").write_bytes(b"")

    with pytest.raises(DatabaseFormatError, match="without a current-format"):
        create_update_snapshot(tmp_path, operation_id="upd_1")


def test_a_failed_capture_raises_with_the_recorded_reason(data_dir: Path) -> None:
    _prepared(data_dir)
    begin_maintenance(data_dir, "convert")

    with pytest.raises(DatabaseFormatError, match="maintenance is incomplete"):
        create_update_snapshot(data_dir, operation_id="upd_1")


def test_a_missing_extension_database_refuses_the_update_with_its_release_command(
    data_dir: Path,
) -> None:
    _prepared(data_dir)
    open_database(notes_spec(data_dir, name="ext.gone.state")).close()
    notes_spec(data_dir, name="ext.gone.state").path.unlink()

    with pytest.raises(
        DatabaseUnavailableError, match="`vbot data-store unregister ext.gone.state --yes`"
    ):
        create_update_snapshot(data_dir, operation_id="upd_1")
    assert find_update_snapshot(data_dir, "upd_1") is None


@pytest.mark.parametrize("change", ["database", "document", "marker"])
def test_every_write_after_the_snapshot_is_detected(data_dir: Path, change: str) -> None:
    snapshot = _snapshot(data_dir)

    if change == "database":
        database = open_database(notes_spec(data_dir, name="tasks"))
        add_note(database, "later")
        database.close()
    elif change == "document":
        _write(data_dir, "agents/main/agent.json", '{"format_version": 1, "id": "other"}\n')
    else:
        open_database(notes_spec(data_dir, name="journal")).close()

    assert data_changed_since(data_dir, snapshot) is not None


def test_the_stamp_ignores_an_empty_journal_left_by_a_read_only_open(data_dir: Path) -> None:
    _prepared(data_dir)
    before = data_stamp(data_dir)
    wal = Path(f"{notes_spec(data_dir).path}-wal")
    wal.write_bytes(b"")

    assert data_stamp(data_dir) == before
    wal.write_bytes(b"frames")
    assert data_stamp(data_dir) != before


def test_the_rollback_restores_every_member_and_the_document_set(data_dir: Path) -> None:
    snapshot = _snapshot(data_dir)
    _candidate_writes(data_dir)

    result = restore_update_snapshot(data_dir, snapshot)

    assert result.snapshot_id == snapshot.snapshot_id
    assert result.databases == ("notes", "tasks")
    assert stored_bodies(notes_spec(data_dir)) == ["saved notes"]
    assert stored_bodies(notes_spec(data_dir, name="tasks")) == ["saved tasks"]
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == _SETTINGS
    assert (data_dir / "agents" / "main" / "agent.json").read_text(encoding="utf-8") == _AGENT
    assert not (data_dir / "mcp" / "connections.json").exists()
    assert result.documents is not None
    assert result.documents.removed == ("mcp/connections.json",)
    # The database the candidate registered is retired, not deleted.
    assert result.retired == ("journal",)
    marker = read_marker(data_dir)
    assert marker is not None
    assert sorted(marker.databases) == ["notes", "tasks"]
    assert not notes_spec(data_dir, name="journal").path.exists()
    assert list((quarantine_root(data_dir) / "journal").iterdir())
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["cause"] == UPDATE_ROLLBACK_CAUSE
    assert incident["restored_snapshot_id"] == snapshot.snapshot_id
    assert read_maintenance(data_dir) is None


def test_a_missing_snapshot_is_refused_without_changes(data_dir: Path) -> None:
    snapshot = _snapshot(data_dir)
    _candidate_writes(data_dir)
    shutil.rmtree(snapshot_root(data_dir) / snapshot.snapshot_id)

    with pytest.raises(UpdateRollbackRefusedError, match="missing or malformed"):
        restore_update_snapshot(data_dir, snapshot)

    assert stored_bodies(notes_spec(data_dir)) == ["saved notes", "written by the candidate"]
    assert read_maintenance(data_dir) is None
    assert not quarantine_root(data_dir).exists()


def test_a_snapshot_of_another_operation_is_refused(data_dir: Path) -> None:
    snapshot = _snapshot(data_dir)
    manual = create_data_snapshot(data_dir, reason="manual")
    assert manual is not None

    with pytest.raises(UpdateRollbackRefusedError, match="another operation"):
        restore_update_snapshot(data_dir, replace(snapshot, snapshot_id=manual.name))
    with pytest.raises(UpdateRollbackRefusedError, match="another operation"):
        restore_update_snapshot(data_dir, replace(snapshot, operation_id="upd_2"))
    assert not quarantine_root(data_dir).exists()


def test_a_snapshot_whose_content_differs_from_the_capture_is_refused(data_dir: Path) -> None:
    snapshot = _snapshot(data_dir)

    with pytest.raises(UpdateRollbackRefusedError, match="databases differ"):
        restore_update_snapshot(
            data_dir, replace(snapshot, member_hashes={**snapshot.member_hashes, "notes": "0" * 64})
        )
    stamp = replace(snapshot.stamp, documents={"settings.json": "0" * 64})
    with pytest.raises(UpdateRollbackRefusedError, match="JSON documents differ"):
        restore_update_snapshot(data_dir, replace(snapshot, stamp=stamp))


@pytest.mark.parametrize("member", ["notes.db", "documents/settings.json"])
def test_a_tampered_member_is_refused_without_changes(data_dir: Path, member: str) -> None:
    snapshot = _snapshot(data_dir)
    _candidate_writes(data_dir)
    copy = snapshot_root(data_dir) / snapshot.snapshot_id / member
    data = bytearray(copy.read_bytes())
    data[-2] ^= 0xFF
    copy.write_bytes(bytes(data))

    with pytest.raises(UpdateRollbackRefusedError, match="cannot be restored"):
        restore_update_snapshot(data_dir, snapshot)

    assert stored_bodies(notes_spec(data_dir)) == ["saved notes", "written by the candidate"]
    assert '"format_version": 2' in (data_dir / "settings.json").read_text(encoding="utf-8")
    assert read_maintenance(data_dir) is None
    assert not quarantine_root(data_dir).exists()


def test_a_changed_database_identity_is_refused(data_dir: Path) -> None:
    snapshot = _snapshot(data_dir)
    marker = read_marker(data_dir)
    assert marker is not None
    _write_marker(data_dir, {**marker.databases, "notes": MarkerEntry("f" * 32, 1)})

    with pytest.raises(UpdateRollbackRefusedError, match="notes database registration changed"):
        restore_update_snapshot(data_dir, snapshot)


def test_incomplete_maintenance_is_refused(data_dir: Path) -> None:
    snapshot = _snapshot(data_dir)
    begin_maintenance(data_dir, "restore")

    with pytest.raises(UpdateRollbackRefusedError, match="maintenance"):
        restore_update_snapshot(data_dir, snapshot)
    assert read_maintenance(data_dir) is not None


def test_an_interrupted_rollback_keeps_the_guard_and_completes_when_repeated(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(data_dir)
    _candidate_writes(data_dir)
    real_retire = recovery_module._retire_databases_locked

    def fail_retire(*_args: object) -> None:
        raise DatabaseUnavailableError("injected interruption")

    monkeypatch.setattr(recovery_module, "_retire_databases_locked", fail_retire)
    with pytest.raises(DatabaseUnavailableError, match="injected interruption"):
        restore_update_snapshot(data_dir, snapshot)
    guard = read_maintenance(data_dir)
    assert guard is not None and guard.operation == "restore"
    # The automatic path never resumes; an operator repeats the complete restore.
    with pytest.raises(UpdateRollbackRefusedError):
        restore_update_snapshot(data_dir, snapshot)

    monkeypatch.setattr(recovery_module, "_retire_databases_locked", real_retire)
    result = restore_data_snapshot(
        data_dir,
        snapshot_root(data_dir) / snapshot.snapshot_id,
        documents=True,
        retire_unlisted=True,
    )
    assert result.retired == ("journal",)
    assert read_maintenance(data_dir) is None
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == _SETTINGS


def test_retiring_requires_every_member(data_dir: Path) -> None:
    snapshot = _snapshot(data_dir)

    with pytest.raises(ValueError, match="every member"):
        restore_data_snapshot(
            data_dir,
            snapshot_root(data_dir) / snapshot.snapshot_id,
            names=["notes"],
            retire_unlisted=True,
        )
