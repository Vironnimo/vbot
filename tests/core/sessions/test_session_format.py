"""Current-format Session marker contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.sessions.errors import SessionStorageFormatError
from core.sessions.format import (
    MARKER_FORMAT_VERSION,
    MARKER_STATE_READY,
    read_session_store_marker,
)
from core.sessions.schema import SCHEMA_VERSION
from core.sessions.store import SessionStore


def _write_marker(path: Path, *, schema_version: int) -> None:
    path.write_text(
        json.dumps(
            {
                "format_version": MARKER_FORMAT_VERSION,
                "state": MARKER_STATE_READY,
                "database_id": "a" * 32,
                "schema_version": schema_version,
            }
        ),
        encoding="utf-8",
    )


def test_marker_rejects_a_schema_version_newer_than_the_runtime(tmp_path: Path) -> None:
    _write_marker(tmp_path / "session-store.json", schema_version=SCHEMA_VERSION + 1)

    with pytest.raises(SessionStorageFormatError, match="newer"):
        read_session_store_marker(tmp_path)


def test_existing_root_without_marker_never_authorizes_a_database(tmp_path: Path) -> None:
    uninitialized_root = tmp_path / "uninitialized"
    uninitialized_root.mkdir()
    database = uninitialized_root / "sessions.db"
    database.write_bytes(b"SQLite format 3\x00")

    with pytest.raises(SessionStorageFormatError):
        SessionStore(database)
