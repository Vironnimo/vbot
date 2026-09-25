"""Tests for the SQLite FTS recall backend."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage, ToolCall
from core.chat.content_blocks import FileBlock, TextBlock
from core.database import APPLICATION_IDS, DatabaseUnavailableError
from core.recall import (
    RecallBackendContext,
    RecallOrder,
    RecallSearchRequest,
    SqliteFtsRecallBackend,
)
from core.recall._passage_catalog import PassageCatalog
from core.recall.canonical import CANONICAL_FALLBACK_PARTIAL_REASON
from core.sessions import ChatSession, ChatSessionManager
from tests.core.sessions.history_fixtures import append_tool_fixture

pytestmark = pytest.mark.asyncio


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def request(
    *,
    query: str,
    match_mode: str = "all_terms",
    roles: tuple[str, ...] = ("user", "assistant", "tool", "error", "compaction_checkpoint"),
    limit: int = 20,
) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        offset=0,
        session_id=None,
        query=query,
        since=None,
        until=None,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        limit=limit,
        order="newest",
    )


def backend(tmp_path: Path, sessions: ChatSessionManager) -> SqliteFtsRecallBackend:
    return SqliteFtsRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))


def passage_request(
    query: str,
    *,
    limit: int = 20,
    session_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        session_id=session_id,
        query=query,
        since=since,
        until=until,
        roles=("user", "assistant", "error", "compaction_checkpoint"),
        match_mode="all_terms",
        order="relevance",
        offset=0,
        limit=limit,
    )


def message_request(
    query: str,
    *,
    roles: tuple[str, ...] = ("user", "assistant", "error", "compaction_checkpoint"),
    limit: int = 20,
) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        session_id=None,
        query=query,
        since=None,
        until=None,
        roles=roles,
        match_mode="all_terms",
        order="relevance",
        offset=0,
        limit=limit,
    )


async def test_sqlite_fts_passage_arm_returns_multiple_source_faithful_hits(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    original = "needle  spacing\n" * 250
    sessions.create("coder", session_id="passages").append(
        ChatMessage.user(original, timestamp=timestamp(1))
    )

    page = await backend(tmp_path, sessions).search_passages(passage_request("needle"))

    assert page.result_type == "passage"
    assert page.ranking == "bm25_trigram"
    assert len(page.hits) > 1
    assert all(hit.session_id == "passages" for hit in page.hits)
    assert all(hit.sources == ("literal",) for hit in page.hits)
    assert page.hits[0].text in original


async def test_passage_time_filters_compare_equivalent_timestamp_encodings(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="timestamp-encoding").append(
        ChatMessage.user("encoding needle", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("needle"))
    with closing(sqlite3.connect(recall.index_path)) as connection, connection:
        connection.execute(
            "UPDATE passages SET start_timestamp = ?, end_timestamp = ?",
            ("2026-05-01T12:00:00Z", "2026-05-01T12:00:00Z"),
        )

    page = await recall.search_passages(
        passage_request(
            "needle",
            since=datetime(2026, 5, 1, 12, tzinfo=UTC),
            until=datetime(2026, 5, 1, 12, tzinfo=UTC),
        )
    )

    assert [hit.session_id for hit in page.hits] == ["timestamp-encoding"]


async def test_sqlite_fts_filtered_search_keeps_other_scope_sessions_indexed(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    for session_id, day in (("one", 1), ("two", 2)):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(f"banana fruit {session_id}", timestamp=timestamp(day))
        )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("fruit"))

    await recall.search_passages(passage_request("fruit", session_id="one"))

    with closing(sqlite3.connect(recall.index_path)) as connection:
        indexed = {
            str(row[0])
            for row in connection.execute(
                "SELECT session_id FROM indexed_sessions WHERE agent_id = ? AND project_id = ?",
                ("coder", ""),
            )
        }
    assert indexed == {"one", "two"}


async def test_sqlite_fts_message_search_uses_canonical_storage(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="search-session").append(
        ChatMessage.user("Release deploy plan", timestamp=timestamp(3))
    )
    recall = backend(tmp_path, sessions)

    page = await recall.search_page(request(query="release deploy"))

    assert page.hits[0].session_id == "search-session"
    assert not recall.index_path.exists()


async def test_passage_index_does_not_duplicate_canonical_message_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="passage-only").append(
        ChatMessage.user("Passage projection only", timestamp=timestamp(1))
    )
    monkeypatch.setattr(sessions, "is_fts_available", lambda: False)
    recall = backend(tmp_path, sessions)

    await recall.search_passages(passage_request("projection"))

    with closing(sqlite3.connect(recall.index_path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
    assert "passages" in tables
    assert "passages_fts" in tables
    assert "messages" not in tables
    assert "messages_fts" not in tables


async def test_sqlite_fts_reindexes_stale_session_after_append(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="stale-session")
    session.append(ChatMessage.user("Initial release notes", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    first_data = await recall.search_page(request(query="release"))
    session.append(ChatMessage.user("SQLite recall needle", timestamp=timestamp(2)))
    second_data = await recall.search_page(request(query="sqlite recall"))

    assert len(first_data.hits) == 1
    assert second_data.hits[0].text == "SQLite recall needle"


def _index_identity(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        identity: dict[str, object] = dict(
            connection.execute("SELECT key, value FROM kernel_meta").fetchall()
        )
        identity["application_id"] = connection.execute("PRAGMA application_id").fetchone()[0]
    return identity


async def test_passage_index_is_a_kernel_disposable_projection(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("Disposable recall index", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("disposable"))

    identity = _index_identity(recall.index_path)
    assert recall.index_path == tmp_path / "recall" / "session_index.sqlite"
    assert identity["application_id"] == APPLICATION_IDS["recall_index"]
    assert identity["database_name"] == "recall_index"
    assert identity["projection_version"] == "1"


async def test_sqlite_fts_rebuilds_when_index_file_is_deleted(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="rebuild-session").append(
        ChatMessage.user("Disposable recall index", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("disposable"))
    assert recall.index_path.is_file()
    await recall.aclose()
    recall.index_path.unlink()

    restarted = backend(tmp_path, sessions)
    page = await restarted.search_passages(passage_request("disposable"))

    assert page.hits[0].session_id == "rebuild-session"
    assert restarted.index_path.is_file()


async def test_projection_version_mismatch_discards_and_rebuilds_the_index(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="rebuild-session").append(
        ChatMessage.user("Disposable recall index", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("disposable"))
    old_identity = _index_identity(recall.index_path)
    recall.close()
    with closing(sqlite3.connect(recall.index_path)) as connection, connection:
        connection.execute("UPDATE kernel_meta SET value = '0' WHERE key = 'projection_version'")
        connection.execute("UPDATE passages SET text = 'stale text'")

    restarted = backend(tmp_path, sessions)
    page = await restarted.search_passages(passage_request("disposable"))

    assert [hit.text for hit in page.hits] == ["Disposable recall index"]
    identity = _index_identity(restarted.index_path)
    assert identity["projection_version"] == "1"
    assert identity["database_id"] != old_identity["database_id"]


async def test_passage_index_damaged_during_a_search_is_rebuilt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="damaged").append(
        ChatMessage.user("needle", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("needle"))
    old_identity = _index_identity(recall.index_path)
    refresh = PassageCatalog.refresh
    failures = 0

    async def damaged_once(self: PassageCatalog, *args: object) -> None:
        nonlocal failures
        if failures == 0:
            failures += 1
            raise sqlite3.DatabaseError("database disk image is malformed")
        await refresh(self, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(PassageCatalog, "refresh", damaged_once)

    page = await recall.search_passages(passage_request("needle"))

    assert [hit.session_id for hit in page.hits] == ["damaged"]
    assert _index_identity(recall.index_path)["database_id"] != old_identity["database_id"]


async def test_busy_passage_index_fails_the_search_without_discarding_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.recall._passage_catalog.WRITE_PATIENCE_S", 0.2)
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="busy")
    session.append(ChatMessage.user("needle one", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("needle"))
    identity = _index_identity(recall.index_path)
    await asyncio.to_thread(session.append, ChatMessage.user("needle two", timestamp=timestamp(2)))

    with closing(sqlite3.connect(recall.index_path, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(DatabaseUnavailableError):
            await recall.search_passages(passage_request("needle"))
        blocker.execute("ROLLBACK")

    assert _index_identity(recall.index_path)["database_id"] == identity["database_id"]
    page = await recall.search_passages(passage_request("needle"))
    assert len(page.hits) == 1 and "needle two" in page.hits[0].text


async def test_passage_index_reports_shared_fork_history_once(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    source = sessions.create("coder", session_id="source")
    for day in range(1, 4):
        source.append(ChatMessage.user(f"needle story {day} " * 120, timestamp=timestamp(day)))
    older = await sessions.fork(source.address)
    newer = await sessions.fork(source.address)
    recall = backend(tmp_path, sessions)

    complete = await recall.search_passages(passage_request("needle"))
    without_origin = await recall.search_passages(
        dataclasses.replace(passage_request("needle"), excluded_session_ids=("source",))
    )

    assert complete.hits
    assert {hit.session_id for hit in complete.hits} == {"source"}
    assert {hit.session_id for hit in without_origin.hits} == {newer.id}
    assert [hit.passage_id for hit in without_origin.hits] == [
        hit.passage_id for hit in complete.hits
    ]

    # Deleting the origin copies its history into both forks and bumps their
    # history revision: the forks are reindexed and report it once.
    await asyncio.to_thread(sessions.delete, source.address)
    after_delete = await recall.search_passages(passage_request("needle"))

    assert {hit.session_id for hit in after_delete.hits} == {newer.id}
    assert sorted(str(hit.passage_id) for hit in after_delete.hits) == sorted(
        str(hit.passage_id) for hit in complete.hits
    )
    with closing(sqlite3.connect(recall.index_path)) as connection:
        stamps = {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT session_id, generation_id, history_revision FROM indexed_sessions"
            )
        }
    revisions = await asyncio.to_thread(sessions.list_history_revisions, "coder")
    assert stamps == {
        revision.address.session_id: (revision.generation_id, revision.history_revision)
        for revision in revisions
    }
    assert set(stamps) == {older.id, newer.id}


async def test_passage_index_attributes_own_fork_content_to_the_fork(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    source = sessions.create("coder", session_id="source")
    source.append(ChatMessage.user("shared needle " * 150, timestamp=timestamp(1)))
    fork = await sessions.fork(source.address)
    await asyncio.to_thread(
        fork.append, ChatMessage.user("fork needle only " * 150, timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions)

    page = await recall.search_passages(passage_request("needle"))

    by_session = {hit.session_id for hit in page.hits}
    assert by_session == {"source", fork.id}
    assert len({hit.passage_id for hit in page.hits}) == len(page.hits)
    assert all("fork needle only" in hit.text for hit in page.hits if hit.session_id == fork.id)
    assert all(
        "fork needle only" not in hit.text for hit in page.hits if hit.session_id == "source"
    )


async def test_sqlite_fts_recovers_from_corrupt_index(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="corrupt-session").append(
        ChatMessage.user("Corrupt index still searchable", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    recall.index_path.parent.mkdir(parents=True, exist_ok=True)
    recall.index_path.write_text("not sqlite", encoding="utf-8")

    page = await recall.search_passages(passage_request("corrupt searchable"))

    assert page.hits[0].session_id == "corrupt-session"


async def test_sqlite_fts_phrase_and_any_term_modes(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="match-session")
    session.append(ChatMessage.user("alpha beta", timestamp=timestamp(1)))
    session.append(ChatMessage.user("gamma", timestamp=timestamp(2)))
    recall = backend(tmp_path, sessions)

    phrase_data = await recall.search_page(request(query="alpha beta", match_mode="phrase"))
    any_data = await recall.search_page(request(query="missing gamma", match_mode="any_term"))

    assert [match.text for match in phrase_data.hits] == ["alpha beta"]
    assert [match.text for match in any_data.hits] == ["gamma"]


async def test_sqlite_search_text_matches_canonical_scanner_sources(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="sources-session")
    content_blocks: list[Any] = [
        TextBlock(type="text", text="visible block text"),
        FileBlock(
            type="file",
            attachment_id="attachment-1",
            filename="contract.pdf",
            media_type="application/pdf",
        ),
    ]
    session.append(ChatMessage.user(content_blocks, timestamp=timestamp(1)))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="tool request",
            reasoning="private recall clue",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="grep",
                    arguments={"pattern": "indexed-argument"},
                )
            ],
            timestamp=timestamp(2),
        )
    )
    recall = backend(tmp_path, sessions)

    block_data = await recall.search_page(request(query="contract pdf"))
    reasoning_data = await recall.search_page(request(query="private clue"))
    tool_call_data = await recall.search_page(request(query="indexed argument"))

    assert block_data.hits[0].session_id == "sources-session"
    assert reasoning_data.hits == ()
    assert tool_call_data.hits == ()


async def test_sqlite_fts_finds_substring_within_token(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="substring-session")
    session.append(ChatMessage.user("Switched the agent to gpt4o today", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    data = await recall.search_page(request(query="gpt"))

    assert data.hits[0].session_id == "substring-session"


async def test_sqlite_fts_case_insensitive_substring(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="case-session")
    session.append(ChatMessage.user("Running GPT4O benchmark", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    data = await recall.search_page(request(query="gpt4o"))

    assert data.hits[0].session_id == "case-session"


async def test_sqlite_fts_short_query_falls_back_to_canonical_substring(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="short-session")
    session.append(ChatMessage.user("Go fast", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    data = await recall.search_page(request(query="go"))

    assert data.hits[0].session_id == "short-session"


def _project_request(*, query: str, project_id: str | None, limit: int = 20) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        offset=0,
        session_id=None,
        query=query,
        since=None,
        until=None,
        roles=("user", "assistant", "tool", "error", "compaction_checkpoint"),
        match_mode="all_terms",
        limit=limit,
        order="newest",
        project_id=project_id,
    )


async def test_sqlite_fts_project_recall_finds_only_project_sessions(tmp_path: Path) -> None:
    """A project-scoped recall searches only the project's Sessions."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="global-s").append(
        ChatMessage.user("global release notes", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="proj-s", project_id="alpha").append(
        ChatMessage.user("project release notes", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions)

    project = await recall.search_page(_project_request(query="release", project_id="alpha"))
    identity = await recall.search_page(_project_request(query="release", project_id=None))

    assert [m.session_id for m in project.hits] == ["proj-s"]
    assert [m.session_id for m in identity.hits] == ["global-s"]


async def test_sqlite_fts_same_uuid_global_and_project_do_not_collide(tmp_path: Path) -> None:
    """The same session UUID under two scopes indexes and matches separately."""

    sessions = ChatSessionManager(tmp_path)
    shared_id = "22222222-2222-2222-2222-222222222222"
    sessions.create("coder", session_id=shared_id).append(
        ChatMessage.user("global carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id=shared_id, project_id="alpha").append(
        ChatMessage.user("project bananas", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions)

    # Each scope sees only its own content for the shared UUID.
    global_data = await recall.search_page(_project_request(query="carrots", project_id=None))
    project_data = await recall.search_page(_project_request(query="bananas", project_id="alpha"))

    assert [m.text for m in global_data.hits] == ["global carrots"]
    assert [m.text for m in project_data.hits] == ["project bananas"]
    # The global scope must not surface the project-only term and vice versa.
    assert (await recall.search_page(_project_request(query="bananas", project_id=None))).hits == ()
    assert (
        await recall.search_page(_project_request(query="carrots", project_id="alpha"))
    ).hits == ()


async def test_sqlite_fts_identity_recall_unchanged_by_project_field(tmp_path: Path) -> None:
    """Explicit ``project_id=None`` matches the implicit-default behavior."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="s1").append(
        ChatMessage.user("Release deploy plan", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)

    explicit_none = await recall.search_page(
        _project_request(query="release deploy", project_id=None)
    )
    default = await recall.search_page(request(query="release deploy"))

    assert [m.session_id for m in explicit_none.hits] == ["s1"]
    assert [m.session_id for m in default.hits] == ["s1"]


async def test_tool_inclusive_substring_search_uses_complete_bounded_fallback(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    target = ChatMessage.tool(
        tool_call_id="call-target",
        name="read",
        content='{"ok":true,"data":"prefixneedle"}',
        timestamp=timestamp(1),
    )
    append_tool_fixture(sessions.create("coder", session_id="tool-substring"), target)
    try:
        page = await backend(tmp_path, sessions).search_page(
            message_request("needle", roles=("tool",))
        )

        assert [hit.message_id for hit in page.hits] == [target.id]
        assert page.ranking == "substring_scan_newest"
        assert page.degraded is False
        assert page.degradation_reason is None
    finally:
        sessions.close()


async def test_large_canonical_fallback_reports_partial_instead_of_false_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.sessions import FtsHealth, _store_fts
    from core.sessions import _store_values as store_module

    monkeypatch.setattr(store_module, "_SEARCH_CANDIDATE_LIMIT", 3)
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="bounded-fallback")
    target = ChatMessage.user("prefixhiddenneedle", timestamp=timestamp(1))
    session.append_many(
        [target]
        + [ChatMessage.user(f"filler {day}", timestamp=timestamp(day)) for day in range(2, 8)]
    )
    monkeypatch.setattr(
        _store_fts,
        "_fts_health_from_connection",
        lambda *_args, **_kwargs: FtsHealth(state="unavailable", reason="test fallback"),
    )
    try:
        page = await backend(tmp_path, sessions).search_page(message_request("hiddenneedle"))

        assert page.hits == ()
        assert page.ranking == "substring_scan_newest"
        assert page.degraded is True
        assert page.degradation_reason
    finally:
        sessions.close()


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("order", ["relevance", "newest", "oldest"])
async def test_fallback_reason_names_the_scan_order_actually_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: RecallOrder,
    complete: bool,
) -> None:
    from core.sessions import FtsHealth, _store_fts
    from core.sessions import _store_values as store_module

    if not complete:
        monkeypatch.setattr(store_module, "_SEARCH_CANDIDATE_LIMIT", 3)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="fallback-order").append_many(
        [ChatMessage.user(f"needle {day}", timestamp=timestamp(day)) for day in range(1, 8)]
    )
    monkeypatch.setattr(
        _store_fts,
        "_fts_health_from_connection",
        lambda *_args, **_kwargs: FtsHealth(state="unavailable", reason="test fallback"),
    )
    try:
        page = await backend(tmp_path, sessions).search_page(
            replace(message_request("needle"), order=order)
        )
    finally:
        sessions.close()

    scan_order, other_order = ("oldest", "newest") if order == "oldest" else ("newest", "oldest")
    assert page.ranking == f"substring_scan_{scan_order}"
    assert page.degraded is True
    reason = page.degradation_reason or ""
    assert f"{scan_order}-first" in reason
    assert f"{other_order}-first" not in reason
    assert ("relevance" in reason.lower()) == (order == "relevance" and complete)
    assert ("incomplete" in reason.lower()) == (not complete)


def _short_term_history(sessions: ChatSessionManager) -> list[ChatMessage]:
    """Store Messages whose better-ranked ``c`` tokens do not contain ``C#``."""
    session = sessions.create("coder", session_id="languages")
    session.append_many([ChatMessage.user("c c c", timestamp=timestamp(1)) for _ in range(30)])
    matches = [
        ChatMessage.user(f"We compared C# generics with Java, part {index}", timestamp=timestamp(2))
        for index in range(12)
    ]
    session.append_many(matches)
    return matches


async def test_short_term_pages_stay_full_when_better_ranked_tokens_do_not_match(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    matches = _short_term_history(sessions)
    try:
        recall = backend(tmp_path, sessions)
        first = await recall.search_page(message_request("C#", limit=10))
        second = await recall.search_page(
            replace(message_request("C#", limit=10), offset=10, snapshot_id=first.snapshot_id)
        )

        assert (len(first.hits), first.has_more) == (10, True)
        assert (len(second.hits), second.has_more) == (2, False)
        assert {hit.message_id for hit in first.hits + second.hits} == {
            message.id for message in matches
        }
        assert first.ranking == "bm25"
        assert not first.degraded and not second.degraded
    finally:
        sessions.close()


async def test_candidate_budget_marks_rejected_candidates_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.sessions import _store_values

    monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", 5)
    sessions = ChatSessionManager(tmp_path)
    _short_term_history(sessions)
    try:
        page = await backend(tmp_path, sessions).search_page(message_request("C#", limit=10))

        assert page.hits == ()
        assert page.has_more is False
        assert page.ranking == "bm25"
        assert page.degraded is True
        assert page.degradation_reason == CANONICAL_FALLBACK_PARTIAL_REASON
    finally:
        sessions.close()


async def test_time_ordered_pages_return_the_newest_or_oldest_matches(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="history")
    # Short older Messages outrank the longer newer ones.
    older = [ChatMessage.user("needle", timestamp=timestamp(day)) for day in range(1, 11)]
    newer = [
        ChatMessage.user("one needle in a much longer deployment note", timestamp=timestamp(day))
        for day in range(11, 16)
    ]
    session.append_many(older + newer)
    try:
        recall = backend(tmp_path, sessions)
        newest = replace(message_request("needle", limit=3), order="newest")
        newest_page = await recall.search_page(newest)
        oldest_page = await recall.search_page(replace(newest, order="oldest"))

        assert [hit.message_id for hit in newest_page.hits] == [
            message.id for message in reversed(newer[-3:])
        ]
        assert [hit.message_id for hit in oldest_page.hits] == [message.id for message in older[:3]]
        assert newest_page.ranking == "message_time_newest"
        assert newest_page.has_more and oldest_page.has_more
    finally:
        sessions.close()


def _stored_passages(recall: SqliteFtsRecallBackend) -> dict[str, int]:
    with closing(sqlite3.connect(recall.index_path)) as connection:
        for table in ("passages_fts", "passages_fts_tokens"):
            # rank=1 compares every indexed row with its external content.
            connection.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
        return {
            str(passage_id): int(passage_ref)
            for passage_ref, passage_id in connection.execute(
                "SELECT passage_ref, passage_id FROM passages"
            )
        }


async def test_passage_index_rewrites_only_changed_passages(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="growing")
    session.append_many(
        [
            ChatMessage.user(f"needle part {index} " + "x" * 1000, timestamp=timestamp(index + 1))
            for index in range(4)
        ]
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("needle"))
    before = _stored_passages(recall)

    session.append(ChatMessage.user("needle tail " + "y" * 1000, timestamp=timestamp(9)))
    page = await recall.search_passages(passage_request("tail"))
    after = _stored_passages(recall)

    kept = before.keys() & after.keys()
    assert kept
    assert all(before[passage_id] == after[passage_id] for passage_id in kept)
    assert after.keys() - before.keys()
    assert page.hits and {hit.session_id for hit in page.hits} == {"growing"}
    with closing(sqlite3.connect(recall.index_path)) as connection:
        stamp = connection.execute(
            "SELECT generation_id, history_revision FROM indexed_sessions"
        ).fetchall()
    version = sessions.list_history_versions([session.address])[session.address]
    assert stamp == [(version[0], version[1])]


async def test_short_term_passages_use_the_token_index_without_loading_histories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    for index in range(30):
        sessions.create("coder", session_id=f"noise-{index}").append(
            ChatMessage.user("c c c", timestamp=timestamp(1))
        )
    for index in range(12):
        sessions.create("coder", session_id=f"csharp-{index}").append(
            ChatMessage.user(
                f"We compared C# generics with Java, part {index}", timestamp=timestamp(2)
            )
        )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(passage_request("generics"))

    def reject_load(*_args: object) -> None:
        raise AssertionError("an indexed short-term search must not load Session histories")

    for method in ("load", "load_active", "load_since"):
        monkeypatch.setattr(ChatSession, method, reject_load)
    first = await recall.search_passages(passage_request("C#", limit=10))
    second = await recall.search_passages(replace(passage_request("C#", limit=10), offset=10))

    assert first.ranking == "bm25_token"
    assert (len(first.hits), first.has_more) == (10, True)
    assert (len(second.hits), second.has_more) == (2, False)
    assert {hit.session_id for hit in first.hits + second.hits} == {
        f"csharp-{index}" for index in range(12)
    }


async def test_passage_index_that_cannot_be_rebuilt_fails_the_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="broken").append(
        ChatMessage.user("needle", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    attempts = 0

    async def broken(*_args: object) -> None:
        nonlocal attempts
        attempts += 1
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(recall._catalog, "refresh", broken)

    with pytest.raises(sqlite3.DatabaseError):
        await recall.search_passages(passage_request("needle"))
    assert attempts == 2
