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


def test_empty_internal_fts_index_never_reports_healthy_or_hides_matches(tmp_path: Path) -> None:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="empty-index")
    message = ChatMessage.user("unique needle 314159")
    sessions = ChatSessionManager(tmp_path)
    sessions.create(address.agent_id, session_id=address.session_id).append(message)
    try:
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            connection.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
            connection.commit()

        health = sessions.fts_health()
        assert health.state == "degraded"
        assert sessions.status_projection()["state"] == "search_degraded"
        hits = sessions.fts_search(
            "needle", project_id=None, agent_id=address.agent_id, session_id=address.session_id
        )
        assert [hit[1] for hit in hits] == [message.id]
    finally:
        sessions.close()

    reopened = ChatSessionManager(tmp_path)
    try:
        assert reopened.fts_health().state == "healthy"
        assert [
            hit[1]
            for hit in reopened.fts_search(
                "needle",
                project_id=None,
                agent_id=address.agent_id,
                session_id=address.session_id,
            )
        ] == [message.id]
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
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            rows = connection.execute(
                "SELECT message_id, content, searchable FROM messages ORDER BY message_key"
            ).fetchall()
            indexed = connection.execute(
                "SELECT rowid, content FROM messages_fts ORDER BY rowid"
            ).fetchall()
            assert "message_json" not in columns
            assert "reasoning" not in columns
            assert "tool_calls_json" not in columns
            assert "run_id" not in columns
            assert "message_search" not in tables
            assert {
                "assistant_messages",
                "tool_calls",
                "tool_messages",
                "run_summaries",
                "compaction_checkpoints",
                "continuations",
            }.issubset(tables)
            assert rows == [
                (visible.id, "visible searchable content", 1),
                (system.id, "internal system payload", 0),
                (note.id, "internal note payload", 0),
            ]
            assert indexed == [(1, "visible searchable content")]
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


def test_fts_and_canonical_search_apply_explicit_result_bounds(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="bounded")
    session.append_many([ChatMessage.user(f"bounded needle {index}") for index in range(20)])
    try:
        assert len(sessions.fts_search("needle", project_id=None, agent_id="agent", limit=7)) == 7
        assert len(sessions.fts_search("ne", project_id=None, agent_id="agent", limit=7)) == 7
    finally:
        sessions.close()


def test_fts_candidate_filters_apply_before_the_result_limit(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    excluded = sessions.create("agent", session_id="excluded")
    included = sessions.create("agent", session_id="included")
    excluded.append(ChatMessage.assistant(model="model", content="needle needle needle"))
    included_message = ChatMessage.user("needle")
    included.append(included_message)
    try:
        rows = sessions.fts_search(
            "needle",
            project_id=None,
            agent_id="agent",
            roles=("user",),
            excluded_session_ids=("excluded",),
            limit=1,
        )
        actual = [(row[0].session_id, row[1]) for row in rows]
        assert actual == [("included", included_message.id)]
    finally:
        sessions.close()


def test_history_edit_materializes_active_lineage_and_removes_stale_fts_rows(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="edited")
    original = ChatMessage.user("obsolete needle")
    session.append_many(
        [original, ChatMessage.assistant(model="model", content="obsolete tail needle")]
    )

    session.append_many(
        [ChatMessage.history_edit(original.id), ChatMessage.user("replacement text")]
    )

    assert [message.content for message in session.load_active()] == ["replacement text"]
    assert (
        sessions.fts_search(
            "needle",
            project_id=None,
            agent_id="agent",
            roles=("user", "assistant"),
        )
        == []
    )
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute("SELECT active FROM messages ORDER BY seq").fetchall() == [
            (0,),
            (0,),
            (0,),
            (1,),
        ]
        assert connection.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == 1
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
        connection.execute("DROP TABLE messages_fts")
        connection.execute("DROP TABLE messages_fts_trigram")
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
