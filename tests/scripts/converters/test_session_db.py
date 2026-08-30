"""Offline Session database verification and maintenance coverage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.chat import ChatMessage
from core.sessions import ChatSessionManager
from scripts.converters import session_db


def _database(data_dir: Path) -> Path:
    manager = ChatSessionManager(data_dir)
    session = manager.create("coder", session_id="session-one")
    session.append(ChatMessage.user("hello"))
    manager.close()
    return data_dir / "sessions.db"


def test_verify_accepts_a_healthy_database(tmp_path: Path, capsys) -> None:
    database = _database(tmp_path)

    assert session_db._verify(database) == 0
    assert "semantic_errors: 0" in capsys.readouterr().out


def test_verify_accepts_a_supported_additive_generation(tmp_path: Path, monkeypatch) -> None:
    database = _database(tmp_path)
    monkeypatch.setattr(session_db, "SCHEMA_VERSION", session_db.SCHEMA_VERSION + 1)

    assert session_db._verify(database) == 0


def test_verify_rejects_a_newer_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {session_db.SCHEMA_VERSION + 1}")

    assert session_db._verify(database) == 1


def test_verify_reports_semantic_projection_drift(tmp_path: Path, capsys) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET message_count = message_count + 1")

    assert session_db._verify(database) == 1
    assert "message_count does not match rows" in capsys.readouterr().out


def test_backup_command_creates_a_standalone_verified_database(tmp_path: Path) -> None:
    _database(tmp_path)
    destination = tmp_path / "backups" / "sessions.db"

    assert session_db.main(["backup", str(tmp_path), str(destination)]) == 0
    assert session_db._verify(destination) == 0
