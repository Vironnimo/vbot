"""Retained snapshot reads protect external resources without mutating copies."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.database import DatabaseCorruptError, DatabaseUnavailableError
from core.database.snapshots import read_retained_snapshot_members
from tests.core.database.database_test_support import snapshot_with_notes


def test_member_reads_use_every_retained_snapshot_and_are_read_only(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "first")
    snapshot_with_notes(data_dir, "second")
    results = read_retained_snapshot_members(
        data_dir,
        "notes",
        lambda connection: tuple(
            row[0] for row in connection.execute("SELECT body FROM notes ORDER BY note_id")
        ),
    )
    assert sorted(results, key=len) == [("first",), ("first", "second")]
    with pytest.raises(DatabaseUnavailableError):
        read_retained_snapshot_members(
            data_dir, "notes", lambda connection: connection.execute("DELETE FROM notes")
        )


@pytest.mark.parametrize("damage", ["missing_member", "invalid_manifest", "foreign_identity"])
def test_unreadable_retained_members_do_not_look_like_empty_reference_sets(
    data_dir: Path, damage: str
) -> None:
    snapshot = snapshot_with_notes(data_dir, "keep")
    if damage == "missing_member":
        (snapshot / "notes.db").unlink()
    elif damage == "invalid_manifest":
        (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
    else:
        with sqlite3.connect(snapshot / "notes.db") as connection:
            connection.execute("UPDATE kernel_meta SET value = 'other' WHERE key = 'database_name'")
    with pytest.raises(DatabaseCorruptError):
        read_retained_snapshot_members(data_dir, "notes", lambda connection: ())
