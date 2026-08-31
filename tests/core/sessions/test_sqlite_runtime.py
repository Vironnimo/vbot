"""Connection ownership, retry, and bounded reader contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.sessions import sqlite_runtime
from core.sessions.sqlite_runtime import (
    READ_CONNECTION_LIMIT,
    SQLiteRuntime,
    tracked_connection_count,
)


def _runtime(tmp_path: Path) -> SQLiteRuntime:
    runtime = SQLiteRuntime(tmp_path / "sessions.db")
    runtime.open_writer(create=True, database_id="a" * 32)
    return runtime


def test_open_failure_unregisters_the_connection(tmp_path: Path, monkeypatch) -> None:
    runtime = SQLiteRuntime(tmp_path / "sessions.db")
    monkeypatch.setattr(
        sqlite_runtime,
        "apply_wal_with_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected setup failure")),
    )

    with pytest.raises(RuntimeError, match="injected setup failure"):
        runtime.open_writer(create=True, database_id="a" * 32)

    assert runtime.live_connection_count() == 0
    assert tracked_connection_count(tmp_path / "sessions.db") == 0


def test_busy_transaction_retries_as_one_idempotent_unit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    attempts = 0

    def write(connection: sqlite3.Connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        connection.execute("CREATE TABLE retry_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO retry_probe(value) VALUES ('once')")

    try:
        runtime.execute_write(write, patience_s=1.0)
        assert attempts == 2
        assert runtime.writer.execute("SELECT COUNT(*) FROM retry_probe").fetchone()[0] == 1
    finally:
        runtime.close()


def test_reader_permits_are_bounded_and_released_on_failure(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    readers = []
    try:
        readers = [runtime._checkout_reader() for _ in range(READ_CONNECTION_LIMIT + 2)]
        active = [reader for reader in readers if reader is not None]
        assert len(active) == READ_CONNECTION_LIMIT
        assert runtime.reader_stats()[1] >= 2

        for reader in active:
            runtime._close_reader(reader)

        with pytest.raises(KeyboardInterrupt), runtime.read_ctx():
            raise KeyboardInterrupt
        assert runtime.reader_stats()[0] == 0
    finally:
        runtime.close()
    assert runtime.live_connection_count() == 0


def test_reader_open_failure_releases_permit_and_can_retry(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    real_connect = sqlite_runtime.connect_tracked
    calls = 0

    def connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite_runtime, "connect_tracked", connect)
    try:
        assert runtime._checkout_reader() is None
        assert runtime.reader_stats()[0] == 0
        assert runtime.live_connection_count() == 1

        runtime._reader_open_failed_at = 0
        reader = runtime._checkout_reader()
        assert reader is not None
        runtime._close_reader(reader)
    finally:
        runtime.close()
    assert runtime.live_connection_count() == 0


def test_checkpoint_with_a_reader_keeps_the_runtime_usable(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    reader = runtime._checkout_reader()
    try:
        assert reader is not None
        reader.execute("BEGIN")
        reader.execute("SELECT 1").fetchone()
        runtime.checkpoint()
        reader.execute("ROLLBACK")
        assert reader.execute("SELECT 1").fetchone()[0] == 1
    finally:
        if reader is not None:
            runtime._close_reader(reader)
        runtime.close()
    assert runtime.live_connection_count() == 0
