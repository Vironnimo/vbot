"""The sessions view measures real Tool calls from a converted copy of a sessions database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.tool_lab import sessions
from tests.scripts.converters.persistence_generation_1.legacy_sessions_support import (
    LegacySessionStore,
)

_FAILED_READ = json.dumps(
    {
        "ok": False,
        "error": {"code": "invalid_arguments", "message": "read needs path"},
        "data": None,
        "artifacts": [],
    }
)
_OK = json.dumps({"ok": True, "error": None, "data": {"content": "1| one"}, "artifacts": []})


def test_sessions_converts_an_old_database_copy_and_measures_calls(tmp_path: Path) -> None:
    source = tmp_path / "live" / "sessions.db"
    with LegacySessionStore(source) as legacy:
        key = legacy.session("s1")
        legacy.start_run(key, "run-1", minute=1)
        legacy.user(key, "read the notes", minute=1, run_id="run-1")
        legacy.assistant(
            key,
            None,
            minute=2,
            run_id="run-1",
            tool_calls=[{"id": "c1", "name": "read", "arguments": {"file_path": "notes.md"}}],
        )
        legacy.tool_result(key, "c1", _FAILED_READ, minute=2)
        legacy.assistant(
            key,
            None,
            minute=3,
            run_id="run-1",
            tool_calls=[{"id": "c2", "name": "read", "arguments": {"path": "notes.md"}}],
        )
        legacy.tool_result(key, "c2", _OK, minute=3)
        legacy.assistant(key, "done", minute=4, run_id="run-1")
        legacy.finish_run(key, "run-1", minute=4)
    before = source.read_bytes()

    with sessions.prepared_database(source, work=None) as database:
        records = sessions.load_calls(database)

    assert source.read_bytes() == before
    assert [(record.name, record.failed, record.error_code) for record in records] == [
        ("read", True, "invalid_arguments"),
        ("read", False, None),
    ]
    assert records[0].error_message == "read needs path"
    view = sessions.tool_view(records, "read", schema_keys=frozenset({"path", "offset", "limit"}))
    assert "!file_path" in view
    assert "invalid_arguments: read needs path" in view
    assert "same Tool succeeded next" in view
    assert "read" in sessions.overview(records)


def test_sessions_keeps_a_prepared_copy_for_later_runs(tmp_path: Path) -> None:
    source = tmp_path / "live" / "sessions.db"
    with LegacySessionStore(source) as legacy:
        legacy.session("s1")
    work = tmp_path / "work"

    with sessions.prepared_database(source, work=work) as database:
        assert database == work / "sessions.db"
    source.unlink()

    with sessions.prepared_database(work, work=work) as database:
        assert sessions.load_calls(database) == []


def test_sessions_refuses_a_foreign_database(tmp_path: Path) -> None:
    foreign = tmp_path / "other.db"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE things (id INTEGER)")

    with (
        pytest.raises(sessions.SessionsSourceError, match="not a vBot sessions database"),
        sessions.prepared_database(foreign, work=None),
    ):
        pass
