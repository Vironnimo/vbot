"""Cross-system fault matrix for SQLite Sessions (Phase 6).

Covers a subset of the required matrix with real SQLite files and real
connections; mocks only inject failures at named seams.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.store import SessionStore


def test_committed_message_survives_restart(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    addr = SessionAddress(project_id=None, agent_id="agent", session_id="s1")
    s = sessions.create("agent", session_id="s1")
    s.append(ChatMessage.user("hello"))
    sessions.close()
    # Reopen
    sessions2 = ChatSessionManager(tmp_path)
    msgs = sessions2.get(addr).load()
    assert any(m.content == "hello" for m in msgs)
    sessions2.close()


def test_busy_retry_does_not_quarantine(tmp_path: Path) -> None:
    from core.sessions.errors import SessionStoreUnavailableError
    from core.sessions.store import SessionStore

    store = SessionStore(tmp_path / "sessions.db")
    addr = SessionAddress(project_id=None, agent_id="agent", session_id="busy")
    store.create(addr)
    # Simulate busy via short patience and mocked OperationalError
    original = store._writer.execute

    def failing_execute(sql, *args, **kwargs):
        if "INSERT INTO messages" in str(sql):
            raise sqlite3.OperationalError("database is locked")
        return original(sql, *args, **kwargs)

    store._writer.execute = failing_execute  # type: ignore[assignment]
    try:
        with pytest.raises(SessionStoreUnavailableError):
            # Use very short patience to fail fast
            store._execute_write(
                lambda c: c.execute(
                    "INSERT INTO messages (session_key, seq, message_id, role, timestamp, message_json) VALUES (1, 0, 'x', 'user', 't', '{}')"
                ),
                patience_s=0.1,
            )
        # After busy, DB still usable
        store._writer.execute = original  # type: ignore[assignment]
        store.append_messages(addr, [ChatMessage.user("y")])
        assert len(store.messages(addr)) == 1
        # No quarantine should have been created
        assert not (tmp_path / "session-quarantine").exists()
    finally:
        store._writer.execute = original  # type: ignore[assignment]
        store.close()


def test_zeroed_db_is_quarantined_and_restored(tmp_path: Path) -> None:
    from core.sessions.snapshots import create_snapshot, list_snapshots

    sessions = ChatSessionManager(tmp_path)
    s = sessions.create("agent", session_id="snap")
    s.append(ChatMessage.user("keep"))
    sessions.close()
    # Create snapshot
    store = SessionStore(tmp_path / "sessions.db")
    from core.sessions.snapshots import snapshot_root

    root = snapshot_root(tmp_path)
    snap = create_snapshot(tmp_path, tmp_path / "sessions.db", store.backup, database_id=None)
    assert snap is not None and snap.exists()
    store.close()
    # Zero the DB
    (tmp_path / "sessions.db").write_bytes(b"\x00" * 1024)
    # Next open should auto-restore
    sessions2 = ChatSessionManager(tmp_path)
    msgs = sessions2.get(SessionAddress(project_id=None, agent_id="agent", session_id="snap")).load()
    assert any("keep" in str(m.content) for m in msgs)
    # Quarantine should exist
    assert (tmp_path / "session-quarantine").exists()
    sessions2.close()


def test_fts_corruption_does_not_block_canonical(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    s = sessions.create("agent", session_id="fts")
    s.append(ChatMessage.user("canonical hello"))
    # Corrupt FTS
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE IF EXISTS messages_fts")
        conn.commit()
    finally:
        conn.close()
    # Append should still succeed via detach
    s2 = sessions.get(SessionAddress(project_id=None, agent_id="agent", session_id="fts"))
    s2.append(ChatMessage.user("after corruption"))
    assert len(s2.load()) == 2
    sessions.close()
