"""Integrated FTS contracts for canonical Session history."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.database import data_store_status
from core.runs import RunKind
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.schema import (
    FTS_COMPLETED_HIGH_WATER_KEY,
    FTS_DEGRADED_REASON_KEY,
    FTS_GENERATION_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    FTS_TARGET_HIGH_WATER_KEY,
)
from tests.core.sessions.history_fixtures import admit_run, append_tool_fixture, seed_history


def test_empty_store_bootstrap_does_not_enter_resumable_fts_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.sessions import _store_fts as store_module

    def fail_backfill(_connection: sqlite3.Connection) -> None:
        raise AssertionError("an empty FTS projection must become healthy in its bootstrap commit")

    monkeypatch.setattr(store_module, "_backfill_fts", fail_backfill)
    sessions = ChatSessionManager(tmp_path)
    try:
        health = sessions.fts_health()
        assert health.state == "healthy"
        assert health.target_high_water == 0
        assert health.completed_high_water == 0
    finally:
        sessions.close()


def test_search_availability_uses_lifecycle_markers_without_coverage_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.sessions import _store_fts as store_module

    sessions = ChatSessionManager(tmp_path)
    message = ChatMessage.user("marker backed search")
    sessions.create("agent", session_id="search").append(message)

    def fail_coverage(*_args, **_kwargs):
        raise AssertionError("ordinary search availability must not scan canonical coverage")

    monkeypatch.setattr(store_module, "_fts_coverage_ok", fail_coverage)
    try:
        assert sessions.is_fts_available()
        hits = sessions.search_messages(
            "marker", project_id=None, agent_id="agent", session_id="search"
        ).hits
        assert [hit.message_id for hit in hits] == [message.id]
    finally:
        sessions.close()


def test_search_without_trigram_support_uses_the_standard_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.sessions import _store_fts as store_module

    # SQLite builds without the trigram tokenizer create the standard index only.
    monkeypatch.setattr(store_module, "FTS_SQL", store_module.FTS_SQL_FALLBACK)
    sessions = ChatSessionManager(tmp_path)
    try:
        message = ChatMessage.user("standard index needle")
        sessions.create("agent", session_id="no-trigram").append(message)

        result = sessions.search_messages(
            "needle", project_id=None, agent_id="agent", roles=("user", "assistant")
        )

        assert [hit.message_id for hit in result.hits] == [message.id]
        assert result.method == "fts"
        assert sessions.fts_health().state == "healthy"
    finally:
        sessions.close()


def test_detached_fts_reopens_complete_when_canonical_projection_already_exists(
    tmp_path: Path,
) -> None:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="fts-reopen")
    message = ChatMessage.user("detached canonical phrase")
    sessions = ChatSessionManager(tmp_path)
    sessions.create(address.agent_id, session_id=address.session_id).append(message)
    sessions.close()

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute("DROP TABLE entries_fts")
        connection.commit()

    reopened = ChatSessionManager(tmp_path)
    try:
        hits = reopened.search_messages(
            "canonical",
            project_id=None,
            agent_id=address.agent_id,
            session_id=address.session_id,
        ).hits
        assert reopened.is_fts_available()
        assert [hit.message_id for hit in hits] == [message.id]
    finally:
        reopened.close()


def test_empty_internal_fts_index_never_reports_healthy_or_hides_matches(tmp_path: Path) -> None:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="empty-index")
    message = ChatMessage.user("unique needle 314159")
    sessions = ChatSessionManager(tmp_path)
    sessions.create(address.agent_id, session_id=address.session_id).append(message)
    try:
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            connection.execute("INSERT INTO entries_fts(entries_fts) VALUES('delete-all')")
            connection.commit()

        health = sessions.fts_health()
        assert health.state == "degraded"
        status = data_store_status(tmp_path, databases=(sessions.database,))
        assert status["state"] == "degraded"
        assert status["databases"]["sessions"]["details"]["fts"]["state"] == "degraded"
        hits = sessions.search_messages(
            "needle", project_id=None, agent_id=address.agent_id, session_id=address.session_id
        ).hits
        assert [hit.message_id for hit in hits] == [message.id]
    finally:
        sessions.close()

    reopened = ChatSessionManager(tmp_path)
    try:
        assert reopened.fts_health().state == "healthy"
        assert [
            hit.message_id
            for hit in reopened.search_messages(
                "needle",
                project_id=None,
                agent_id=address.agent_id,
                session_id=address.session_id,
            ).hits
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
                row[1] for row in connection.execute("PRAGMA table_info(entries)").fetchall()
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            rows = connection.execute(
                "SELECT e.entry_key, e.entry_id, t.content, e.searchable FROM entries AS e "
                "JOIN entry_text AS t ON t.entry_key = e.entry_key ORDER BY e.entry_key"
            ).fetchall()
            indexed = connection.execute(
                "SELECT rowid, content FROM entries_fts ORDER BY rowid"
            ).fetchall()
            # Hot entry rows stay narrow: text, reasoning and calls live in side tables.
            assert {"content", "message_json", "reasoning", "tool_calls_json"}.isdisjoint(columns)
            assert "run_key" in columns
            assert "message_search" not in tables
            assert {
                "entry_text",
                "assistant_entries",
                "tool_calls",
                "runs",
                "checkpoint_entries",
                "continuations",
            }.issubset(tables)
            assert [row[1:] for row in rows] == [
                (visible.id, "visible searchable content", 1),
                (system.id, "internal system payload", 0),
                (note.id, "internal note payload", 0),
            ]
            assert indexed == [(rows[0][0], "visible searchable content")]
            metadata = dict(connection.execute("SELECT key, value FROM store_meta").fetchall())
        assert metadata[FTS_STORAGE_VERSION_KEY] == str(FTS_STORAGE_VERSION)
        assert metadata[FTS_GENERATION_KEY]
        assert metadata[FTS_TARGET_HIGH_WATER_KEY] == metadata[FTS_COMPLETED_HIGH_WATER_KEY]
        assert metadata[FTS_DEGRADED_REASON_KEY] == ""
        assert sessions.is_fts_available()
        assert (
            sessions.search_messages(
                "internal",
                project_id=None,
                agent_id=address.agent_id,
                session_id=address.session_id,
            ).hits
            == ()
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
        hits = sessions.search_messages(
            "shared", project_id=None, agent_id="agent", match_mode="all_terms"
        ).hits
        assert {(hit.address.session_id, hit.message_id) for hit in hits} == {
            ("one", shared_id),
            ("two", shared_id),
        }
    finally:
        sessions.close()


def test_fts_and_canonical_search_apply_explicit_result_bounds(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="bounded")
    session.append_many([ChatMessage.user(f"bounded ne needle {index}") for index in range(20)])
    try:
        assert (
            len(sessions.search_messages("needle", project_id=None, agent_id="agent", limit=7).hits)
            == 7
        )
        assert (
            len(sessions.search_messages("ne", project_id=None, agent_id="agent", limit=7).hits)
            == 7
        )
    finally:
        sessions.close()


def test_short_fts_terms_match_tokens_without_substring_scanning(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="short-terms")
    exact = ChatMessage.user("AI project")
    substring_only = ChatMessage.user("main project")
    session.append_many([exact, substring_only])
    try:
        hits = sessions.search_messages("ai", project_id=None, agent_id="agent").hits
        assert [hit.message_id for hit in hits] == [exact.id]
    finally:
        sessions.close()


def test_healthy_fts_null_result_does_not_fall_back_to_canonical_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from core.sessions import _store_search

    sessions = ChatSessionManager(tmp_path)
    sessions.create("agent", session_id="no-match").append(ChatMessage.user("present text"))

    def fail_scan(*_args: object) -> None:
        raise AssertionError("healthy FTS null results must not scan canonical rows")

    monkeypatch.setattr(_store_search, "_scan_candidates", fail_scan)
    try:
        result = sessions.search_messages(
            "absent",
            project_id=None,
            agent_id="agent",
            roles=("user", "assistant", "error", "compaction_checkpoint"),
        )
        assert (result.hits, result.complete, result.method) == ((), True, "fts")
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
        rows = sessions.search_messages(
            "needle",
            project_id=None,
            agent_id="agent",
            roles=("user",),
            excluded_session_ids=("excluded",),
            limit=1,
        ).hits
        actual = [(row.address.session_id, row.message_id) for row in rows]
        assert actual == [("included", included_message.id)]
    finally:
        sessions.close()


def test_history_edit_supersedes_the_tail_and_removes_stale_fts_rows(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="edited")
    original = ChatMessage.user("obsolete needle")
    session.append_many(
        [original, ChatMessage.assistant(model="model", content="obsolete tail needle")]
    )

    session.apply_edit(original.id, [ChatMessage.user("replacement text")])

    assert [message.content for message in session.load_active()] == ["replacement text"]
    assert (
        sessions.search_messages(
            "needle",
            project_id=None,
            agent_id="agent",
            roles=("user", "assistant"),
        ).hits
        == ()
    )
    forked = asyncio.run(sessions.fork(session.address, target_agent_id="reviewer"))
    assert [message.content for message in forked.load_active()] == ["replacement text"]
    # The fork shares the entry: a search its origin is not eligible for reports the fork.
    assert [
        (hit.address, hit.message_id)
        for hit in sessions.search_messages(
            "replacement",
            project_id=None,
            agent_id="reviewer",
            session_id=forked.address.session_id,
        ).hits
    ] == [(forked.address, session.load_active()[0].id)]
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute(
            "SELECT e.role, e.superseded_at_seq FROM entries AS e "
            "JOIN sessions AS s ON s.session_key = e.session_key "
            "WHERE s.agent_id = 'agent' ORDER BY e.seq"
        ).fetchall() == [
            ("user", 2),
            ("assistant", 2),
            ("history_edit", 2),
            ("user", None),
        ]
        assert connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM entries_fts_docsize").fetchone()[0] == 1
    sessions.close()


def test_session_delete_and_history_edit_remove_exactly_their_indexed_rows(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    seeded: dict[str, list[ChatMessage]] = {}
    for session_id in ("kept", "removed"):
        messages = [
            ChatMessage.user(f"alpha needle question {session_id}"),
            ChatMessage.assistant(model="model", content=f"alpha needle answer {session_id}"),
            ChatMessage.tool(
                tool_call_id=f"call-{session_id}",
                name="read",
                content='{"ok": true, "data": "tool needle payload"}',
            ),
            ChatMessage.user(f"beta needle follow-up {session_id}"),
            ChatMessage.assistant(model="model", content=f"beta needle reply {session_id}"),
        ]
        seed_history(sessions.create("agent", session_id=session_id), messages)
        seeded[session_id] = messages
    kept = sessions.get(SessionAddress(project_id=None, agent_id="agent", session_id="kept"))
    kept.apply_edit(seeded["kept"][3].id, [ChatMessage.user("gamma replacement")])

    sessions.delete(SessionAddress(project_id=None, agent_id="agent", session_id="removed"))

    try:
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            # rank=1 compares every indexed row with its external content.
            connection.execute(
                "INSERT INTO entries_fts(entries_fts, rank) VALUES('integrity-check', 1)"
            )
            connection.execute(
                "INSERT INTO entries_fts_trigram(entries_fts_trigram, rank) "
                "VALUES('integrity-check', 1)"
            )
            base_rows = connection.execute("SELECT COUNT(*) FROM entries_fts_docsize").fetchone()
            trigram_rows = connection.execute(
                "SELECT COUNT(*) FROM entries_fts_trigram_docsize"
            ).fetchone()
        # The kept prefix (question, answer, Tool result) and the replacement question.
        assert base_rows == (4,)
        assert trigram_rows == (3,)
        assert sessions.fts_health().state == "healthy"
        hits = sessions.search_messages(
            "needle", project_id=None, agent_id="agent", roles=("user", "assistant", "tool")
        ).hits
        assert {(hit.address.session_id, hit.message_id) for hit in hits} == {
            ("kept", message.id) for message in seeded["kept"][:3]
        }
    finally:
        sessions.close()


def test_failed_fts_delete_detaches_the_index_and_still_deletes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.sessions import _store_fts as store_module

    address = SessionAddress(project_id=None, agent_id="agent", session_id="doomed")
    sessions = ChatSessionManager(tmp_path)
    sessions.create(address.agent_id, session_id=address.session_id).append(
        ChatMessage.user("indexed words")
    )
    real_forget = store_module.fts_forget
    failures: list[str] = []

    def fail_once(connection: sqlite3.Connection, keys_sql: str, params: Any) -> None:
        if not failures:
            failures.append(keys_sql)
            raise sqlite3.OperationalError("fts5: simulated index write failure")
        real_forget(connection, keys_sql, params)

    monkeypatch.setattr(store_module, "fts_forget", fail_once)
    try:
        sessions.delete(address)

        assert failures
        assert not sessions.exists(address)
        health = sessions.fts_health()
        assert health.state == "unavailable"
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            stale = connection.execute(
                "SELECT value FROM store_meta WHERE key = 'fts_stale'"
            ).fetchone()
        assert stale == ("1",)
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
        hits = sessions.search_messages(
            "canonical", project_id=None, agent_id=address.agent_id, session_id=address.session_id
        ).hits
        assert [hit.message_id for hit in hits] == [message.id]
    finally:
        sessions.close()


def test_fts_rebuild_reads_content_only_inside_current_batch(tmp_path: Path, monkeypatch) -> None:
    from core.sessions import _store_fts as store_module

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="bounded-rebuild")
    session.append_many([ChatMessage.user(f"bounded content {index}") for index in range(100)])
    sessions.close()
    observed: set[int] = set()

    def observe(key: int, content: str) -> str:
        observed.add(key)
        return content

    def stop_after_read(stage: str, _high_water: int) -> None:
        if stage == "before_batch_commit":
            raise RuntimeError("test batch read complete")

    monkeypatch.setattr(store_module, "_FTS_BATCH_WINDOW", 5)
    monkeypatch.setattr(store_module, "_FTS_REBUILD_HOOK", stop_after_read)
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("observe_content", 2, observe)
        original = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='entries_fts_source'"
        ).fetchone()[0]
        connection.execute(original.replace("entries_fts_source", "unobserved_fts_source", 1))
        connection.execute("DROP VIEW entries_fts_source")
        connection.execute(
            "CREATE VIEW entries_fts_source AS SELECT entry_key, "
            "observe_content(entry_key, content) AS content, search_text, "
            "reasoning, name, error_kind, tool_calls FROM unobserved_fts_source"
        )
        connection.execute(
            "UPDATE store_meta SET value='0' WHERE key=?",
            (store_module.FTS_COMPLETED_HIGH_WATER_KEY,),
        )
        connection.commit()
        first_batch = {
            row[0]
            for row in connection.execute(
                "SELECT entry_key FROM entries ORDER BY entry_key LIMIT 5"
            )
        }
        with pytest.raises(RuntimeError, match="test batch read complete"):
            store_module._backfill_fts(connection)
    assert observed == first_batch


def test_fts_rebuild_resumes_after_an_interrupted_batch(tmp_path: Path, monkeypatch) -> None:
    from core.sessions import _store_fts as store_module

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="resumable")
    session.append_many([ChatMessage.user(f"resume needle {index}") for index in range(105)])
    sessions.close()

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute("DROP TABLE entries_fts")
        connection.execute("DROP TABLE entries_fts_trigram")
        connection.commit()

    def interrupt(stage: str, _high_water: int) -> None:
        if stage == "after_batch_commit":
            raise RuntimeError("simulated FTS interruption")

    monkeypatch.setattr(store_module, "_FTS_BATCH_WINDOW", 50)
    monkeypatch.setattr(store_module, "_FTS_REBUILD_HOOK", interrupt)
    with pytest.raises(RuntimeError, match="simulated FTS interruption"):
        ChatSessionManager(tmp_path)

    monkeypatch.setattr(store_module, "_FTS_REBUILD_HOOK", None)
    reopened = ChatSessionManager(tmp_path)
    try:
        assert reopened.is_fts_available()
        assert (
            len(reopened.search_messages("resume", project_id=None, agent_id="agent").hits) == 105
        )
    finally:
        reopened.close()


@pytest.mark.parametrize("use_fts", [True, False])
def test_time_filters_compare_exact_instants_across_timestamp_encodings(
    tmp_path: Path, use_fts: bool
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="instant-boundary")

    def at(label: str, timestamp: str) -> ChatMessage:
        return ChatMessage(
            id=f"boundary-{label}", timestamp=timestamp, role="user", content=f"boundary {label}"
        )

    before = at("before", "2026-05-01T12:00:00.249999Z")
    equivalent = [
        at("z", "2026-05-01T12:00:00.250Z"),
        at("utc", "2026-05-01T12:00:00.250000+00:00"),
        at("short", "2026-05-01T12:00:00.25+00:00"),
    ]
    after = at("after", "2026-05-01T12:00:00.250001Z")
    session.append_many([before, *equivalent, after])
    try:
        with sqlite3.connect(tmp_path / "sessions.db") as connection:
            stored = dict(connection.execute("SELECT entry_id, created_at FROM entries"))
        assert {stored[message.id] for message in equivalent} == {"2026-05-01T12:00:00.250000Z"}
        for since, until in (
            ("2026-05-01T12:00:00.250Z", "2026-05-01T12:00:00.250Z"),
            ("2026-05-01T14:00:00.250+02:00", "2026-05-01T12:00:00.250000+00:00"),
        ):
            hits = sessions.search_messages(
                "boundary",
                project_id=None,
                agent_id="agent",
                since=since,
                until=until,
                use_fts=use_fts,
            ).hits
            assert {hit.message_id for hit in hits} == {message.id for message in equivalent}
    finally:
        sessions.close()


def test_fts_query_keeps_unicode_tokenizer_spelling(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    message = ChatMessage.user("Die Stra\u00dfe ist lang")
    sessions.create("agent", session_id="unicode").append(message)
    try:
        hits = sessions.search_messages(
            "Stra\u00dfe", project_id=None, agent_id="agent", session_id="unicode", roles=("user",)
        ).hits
        assert [hit.message_id for hit in hits] == [message.id]
    finally:
        sessions.close()


@pytest.mark.parametrize("use_index", [True, False])
def test_search_admits_only_recall_visible_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_index: bool
) -> None:
    from core.sessions import _store_fts as store_module

    if not use_index:
        monkeypatch.setattr(
            store_module,
            "_fts_health_from_connection",
            lambda *_args, **_kwargs: store_module.FtsHealth(state="unavailable", reason="test"),
        )
    sessions = ChatSessionManager(tmp_path)
    visibility = {
        "ordinary": (RunKind.USER,),
        "calendar": (RunKind.CALENDAR,),
        "delegated": (RunKind.SUBAGENT,),
        "reflection": (RunKind.USER, RunKind.REFLECTION),
        "system": (RunKind.SYSTEM,),
    }
    for session_id, run_kinds in visibility.items():
        session = sessions.create("agent", session_id=session_id)
        session.append(ChatMessage.user(f"visible needle {session_id}"))
        for run_kind in run_kinds:
            asyncio.run(admit_run(sessions, session.address, run_kind))
    try:

        def searched(**options: Any) -> set[str]:
            return {
                hit.address.session_id
                for hit in sessions.search_messages(
                    "needle", project_id=None, agent_id="agent", roles=("user",), **options
                ).hits
            }

        assert searched() == {"ordinary", "calendar"}
        assert searched(include_subagents=True) == {"ordinary", "calendar", "delegated"}
        assert searched(include_subagents=True, excluded_session_ids=("calendar",)) == {
            "ordinary",
            "delegated",
        }
        assert searched(session_id="reflection") == set()
    finally:
        sessions.close()


@pytest.mark.parametrize("use_fts", [True, False])
def test_tool_inclusive_search_skips_persisted_recall_results(
    tmp_path: Path, use_fts: bool
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="tools")
    artifact = ChatMessage.tool(
        tool_call_id="call-search", name="session_search", content='{"items": ["needle"]}'
    )
    ordinary = ChatMessage.tool(tool_call_id="call-read", name="read", content="needle in a file")
    append_tool_fixture(session, artifact)
    append_tool_fixture(session, ordinary)
    try:
        result = sessions.search_messages(
            "needle", project_id=None, agent_id="agent", roles=("tool",), use_fts=use_fts
        )
        assert [hit.message_id for hit in result.hits] == [ordinary.id]
        assert result.hits[0].text == "needle in a file"
    finally:
        sessions.close()
