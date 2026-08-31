"""Integrated FTS contracts for canonical Session history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.schema import (
    FTS_COMPLETED_HIGH_WATER_KEY,
    FTS_DEGRADED_REASON_KEY,
    FTS_GENERATION_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    FTS_TARGET_HIGH_WATER_KEY,
)


def test_detached_fts_reopens_complete_when_canonical_projection_already_exists(
    tmp_path: Path,
) -> None:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="fts-reopen")
    message = ChatMessage.user("detached canonical phrase")
    sessions = ChatSessionManager(tmp_path)
    sessions.create(address.agent_id, session_id=address.session_id).append(message)
    sessions.close()

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute("DROP TABLE messages_fts")
        connection.commit()

    reopened = ChatSessionManager(tmp_path)
    try:
        hits = reopened.fts_search(
            "canonical",
            project_id=None,
            agent_id=address.agent_id,
            session_id=address.session_id,
        )
        assert reopened.is_fts_available()
        assert [hit[1] for hit in hits] == [message.id]
    finally:
        reopened.close()


def test_fts_projection_uses_canonical_message_key_and_recall_text_only(tmp_path: Path) -> None:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="projection")
    visible = ChatMessage.user("visible searchable content")
    system = ChatMessage.system("internal system payload", model="model")
    note = ChatMessage.note("internal note payload")
    sessions = ChatSessionManager(tmp_path)
    sessions.create(address.agent_id, session_id=address.session_id).append_many(
        [visible, system, note]
    )
    try:
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            rows = connection.execute(
                """
                SELECT m.message_key, m.message_id, ms.message_key, ms.search_text
                FROM messages AS m
                JOIN message_search AS ms ON ms.message_key = m.message_key
                ORDER BY m.message_key
                """
            ).fetchall()
            assert [row[0] for row in rows] == [row[2] for row in rows]
            assert rows[0][3] == "visible searchable content"
            assert rows[1][3] == ""
            assert rows[2][3] == ""
            assert visible.id not in rows[0][3]
            metadata = dict(connection.execute("SELECT key, value FROM store_meta").fetchall())
        assert metadata[FTS_STORAGE_VERSION_KEY] == str(FTS_STORAGE_VERSION)
        assert metadata[FTS_GENERATION_KEY]
        assert metadata[FTS_TARGET_HIGH_WATER_KEY] == metadata[FTS_COMPLETED_HIGH_WATER_KEY]
        assert metadata[FTS_DEGRADED_REASON_KEY] == ""
        assert sessions.is_fts_available()
        assert (
            sessions.fts_search(
                "internal",
                project_id=None,
                agent_id=address.agent_id,
                session_id=address.session_id,
            )
            == []
        )
    finally:
        sessions.close()


def test_fts_search_preserves_same_message_id_in_distinct_sessions(tmp_path: Path) -> None:
    shared_id = "same-message-id"
    sessions = ChatSessionManager(tmp_path)
    for session_id in ("one", "two"):
        message = ChatMessage(
            id=shared_id,
            timestamp="2026-05-01T12:00:00Z",
            role="user",
            content=f"shared searchable {session_id}",
        )
        sessions.create("agent", session_id=session_id).append(message)
    try:
        hits = sessions.fts_search(
            "shared", project_id=None, agent_id="agent", match_mode="all_terms"
        )
        assert {(hit[0].session_id, hit[1]) for hit in hits} == {
            ("one", shared_id),
            ("two", shared_id),
        }
    finally:
        sessions.close()


def test_malformed_fts_progress_uses_canonical_search_without_hiding_matches(
    tmp_path: Path,
) -> None:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="malformed")
    sessions = ChatSessionManager(tmp_path)
    message = ChatMessage.user("canonical fallback phrase")
    sessions.create(address.agent_id, session_id=address.session_id).append(message)
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "UPDATE store_meta SET value = ? WHERE key = ?",
            ("not-an-integer", FTS_COMPLETED_HIGH_WATER_KEY),
        )
        connection.commit()
    try:
        health = sessions._store.fts_health()
        assert health.available is False
        assert "high-water" in (health.reason or "")
        hits = sessions.fts_search(
            "canonical", project_id=None, agent_id=address.agent_id, session_id=address.session_id
        )
        assert [hit[1] for hit in hits] == [message.id]
    finally:
        sessions.close()


def test_fts_rebuild_resumes_after_an_interrupted_batch(tmp_path: Path, monkeypatch) -> None:
    from core.sessions import store as store_module

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="resumable")
    session.append_many([ChatMessage.user(f"resume needle {index}") for index in range(105)])
    sessions.close()

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute("DELETE FROM message_search")
        connection.execute("DROP TABLE messages_fts")
        connection.commit()

    def interrupt(stage: str, _high_water: int) -> None:
        if stage == "after_batch_commit":
            raise RuntimeError("simulated FTS interruption")

    monkeypatch.setattr(store_module, "_FTS_REBUILD_HOOK", interrupt)
    with pytest.raises(RuntimeError, match="simulated FTS interruption"):
        ChatSessionManager(tmp_path)

    monkeypatch.setattr(store_module, "_FTS_REBUILD_HOOK", None)
    reopened = ChatSessionManager(tmp_path)
    try:
        assert reopened.is_fts_available()
        assert len(reopened.fts_search("resume", project_id=None, agent_id="agent")) == 105
    finally:
        reopened.close()
