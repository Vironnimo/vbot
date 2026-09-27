"""The ``sqlite_fts`` backend: integrated Message search and the Passage FTS index."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage, ToolCall
from core.chat.content_blocks import FileBlock, TextBlock
from core.database import APPLICATION_IDS, DatabaseUnavailableError
from core.recall import RecallBackendContext, RecallOrder, SqliteFtsRecallBackend
from core.recall._passage_catalog import PassageCatalog
from core.recall.canonical import CANONICAL_FALLBACK_PARTIAL_REASON
from core.sessions import ChatSession, ChatSessionManager, FtsHealth, _store_fts, _store_values
from tests.core.recall.recall_test_support import ALL_ROLES, request, timestamp
from tests.core.sessions.history_fixtures import append_tool_fixture

pytestmark = pytest.mark.asyncio


def backend(tmp_path: Path, sessions: ChatSessionManager) -> SqliteFtsRecallBackend:
    return SqliteFtsRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))


def _index_identity(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        identity: dict[str, object] = dict(
            connection.execute("SELECT key, value FROM kernel_meta").fetchall()
        )
        identity["application_id"] = connection.execute("PRAGMA application_id").fetchone()[0]
    return identity


def _without_integrated_fts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _store_fts,
        "_fts_health_from_connection",
        lambda *_args, **_kwargs: FtsHealth(state="unavailable", reason="test fallback"),
    )


# ---------------------------------------------------------------------------
# Message search over the Sessions-owned FTS
# ---------------------------------------------------------------------------


def _searchable_history(session: ChatSession) -> dict[str, ChatMessage]:
    messages = {
        "phrase": ChatMessage.user("alpha beta", timestamp=timestamp(1)),
        "term": ChatMessage.user("gamma", timestamp=timestamp(2)),
        "lower": ChatMessage.user("Switched the agent to gpt4o today", timestamp=timestamp(3)),
        "upper": ChatMessage.user("Running GPT4O benchmark", timestamp=timestamp(4)),
        "short": ChatMessage.user("Go fast", timestamp=timestamp(5)),
        "blocks": ChatMessage.user(
            [
                TextBlock(type="text", text="visible block text"),
                FileBlock(
                    type="file",
                    attachment_id="attachment-1",
                    filename="contract.pdf",
                    media_type="application/pdf",
                ),
            ],
            timestamp=timestamp(6),
        ),
        "assistant": ChatMessage.assistant(
            model="openai/gpt-5",
            content="tool request",
            reasoning="private recall clue",
            tool_calls=[
                ToolCall(id="call-1", name="grep", arguments={"pattern": "indexed-argument"})
            ],
            timestamp=timestamp(7),
        ),
    }
    session.append_many(list(messages.values()))
    return messages


@pytest.mark.parametrize(
    ("query", "match_mode", "expected"),
    [
        ("alpha beta", "phrase", ["phrase"]),
        ("missing gamma", "any_term", ["term"]),
        # Substrings inside tokens match case-insensitively.
        ("pt4", "all_terms", ["upper", "lower"]),
        # Queries below the trigram length use whole tokens.
        ("go", "all_terms", ["short"]),
        # Attachment names are searchable; reasoning and Tool arguments are not.
        ("contract pdf", "all_terms", ["blocks"]),
        ("private clue", "all_terms", []),
        ("indexed argument", "all_terms", []),
    ],
    ids=["phrase", "any-term", "substring", "short-token", "file-block", "reasoning", "tool-args"],
)
async def test_message_search_matches_conversation_text(
    tmp_path: Path,
    sessions: ChatSessionManager,
    query: str,
    match_mode: str,
    expected: list[str],
) -> None:
    messages = _searchable_history(sessions.create("coder", session_id="history"))

    page = await backend(tmp_path, sessions).search_page(
        request(query, match_mode=match_mode, roles=ALL_ROLES, order="newest")
    )

    assert [hit.message_id for hit in page.hits] == [messages[name].id for name in expected]


async def test_message_search_sees_appended_messages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="stale-session")
    session.append(ChatMessage.user("Initial release notes", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    first = await recall.search_page(request("release"))
    session.append(ChatMessage.user("SQLite recall needle", timestamp=timestamp(2)))
    second = await recall.search_page(request("sqlite recall"))

    assert len(first.hits) == 1
    assert [hit.text for hit in second.hits] == ["SQLite recall needle"]


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
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    matches = _short_term_history(sessions)
    recall = backend(tmp_path, sessions)
    first = await recall.search_page(request("C#"))
    second = await recall.search_page(request("C#", offset=10, snapshot_id=first.snapshot_id))

    assert (len(first.hits), first.has_more) == (10, True)
    assert (len(second.hits), second.has_more) == (2, False)
    assert {hit.message_id for hit in first.hits + second.hits} == {
        message.id for message in matches
    }
    assert first.ranking == "bm25"
    assert not first.degraded and not second.degraded


async def test_candidate_budget_marks_rejected_candidates_partial(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", 5)
    _short_term_history(sessions)

    page = await backend(tmp_path, sessions).search_page(request("C#"))

    assert page.hits == ()
    assert page.has_more is False
    assert page.ranking == "bm25"
    assert page.degraded is True
    assert page.degradation_reason == CANONICAL_FALLBACK_PARTIAL_REASON


async def test_time_ordered_pages_return_the_newest_or_oldest_matches(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="history")
    # Short older Messages outrank the longer newer ones.
    older = [ChatMessage.user("needle", timestamp=timestamp(day)) for day in range(1, 11)]
    newer = [
        ChatMessage.user("one needle in a much longer deployment note", timestamp=timestamp(day))
        for day in range(11, 16)
    ]
    session.append_many(older + newer)
    recall = backend(tmp_path, sessions)

    newest_page = await recall.search_page(request("needle", limit=3, order="newest"))
    oldest_page = await recall.search_page(request("needle", limit=3, order="oldest"))

    assert [hit.message_id for hit in newest_page.hits] == [
        message.id for message in reversed(newer[-3:])
    ]
    assert [hit.message_id for hit in oldest_page.hits] == [message.id for message in older[:3]]
    assert newest_page.ranking == "message_time_newest"
    assert newest_page.has_more and oldest_page.has_more


# ---------------------------------------------------------------------------
# Substring-scan fallback
# ---------------------------------------------------------------------------


async def test_tool_inclusive_substring_search_uses_complete_bounded_fallback(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    target = ChatMessage.tool(
        tool_call_id="call-target",
        name="read",
        content='{"ok":true,"data":"prefixneedle"}',
        timestamp=timestamp(1),
    )
    append_tool_fixture(sessions.create("coder", session_id="tool-substring"), target)

    page = await backend(tmp_path, sessions).search_page(request("needle", roles=("tool",)))

    assert [hit.message_id for hit in page.hits] == [target.id]
    assert page.ranking == "substring_scan_newest"
    assert page.degraded is False
    assert page.degradation_reason is None


async def test_large_canonical_fallback_reports_partial_instead_of_false_empty(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", 3)
    session = sessions.create("coder", session_id="bounded-fallback")
    target = ChatMessage.user("prefixhiddenneedle", timestamp=timestamp(1))
    session.append_many(
        [target]
        + [ChatMessage.user(f"filler {day}", timestamp=timestamp(day)) for day in range(2, 8)]
    )
    _without_integrated_fts(monkeypatch)

    page = await backend(tmp_path, sessions).search_page(request("hiddenneedle"))

    assert page.hits == ()
    assert page.ranking == "substring_scan_newest"
    assert page.degraded is True
    assert page.degradation_reason


@pytest.mark.parametrize(
    ("order", "complete"),
    [("relevance", True), ("relevance", False), ("oldest", True), ("newest", False)],
)
async def test_fallback_reason_names_the_scan_order_actually_used(
    tmp_path: Path,
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    order: RecallOrder,
    complete: bool,
) -> None:
    if not complete:
        monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", 3)
    sessions.create("coder", session_id="fallback-order").append_many(
        [ChatMessage.user(f"needle {day}", timestamp=timestamp(day)) for day in range(1, 8)]
    )
    _without_integrated_fts(monkeypatch)

    page = await backend(tmp_path, sessions).search_page(request("needle", order=order))

    scan_order, other_order = ("oldest", "newest") if order == "oldest" else ("newest", "oldest")
    assert page.ranking == f"substring_scan_{scan_order}"
    assert page.degraded is True
    reason = page.degradation_reason or ""
    assert f"{scan_order}-first" in reason
    assert f"{other_order}-first" not in reason
    assert ("relevance" in reason.lower()) == (order == "relevance" and complete)
    assert ("incomplete" in reason.lower()) == (not complete)


async def test_fts_fallback_preserves_original_selection_snapshot(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.create("coder", session_id="one").append(ChatMessage.user("needle"))
    recall = backend(tmp_path, sessions)
    _without_integrated_fts(monkeypatch)

    first = await recall.search_page(request("needle", limit=1))
    continued = await recall.search_page(
        request("needle", limit=1, offset=1, snapshot_id=first.snapshot_id)
    )

    assert first.degraded and continued.degraded
    assert continued.snapshot_id == first.snapshot_id
    assert continued.hits == ()


# ---------------------------------------------------------------------------
# The Passage FTS index (Hybrid's literal arm)
# ---------------------------------------------------------------------------


async def test_passage_arm_returns_multiple_source_faithful_hits(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    original = "needle  spacing\n" * 250
    sessions.create("coder", session_id="passages").append(
        ChatMessage.user(original, timestamp=timestamp(1))
    )

    page = await backend(tmp_path, sessions).search_passages(request("needle", limit=20))

    assert page.result_type == "passage"
    assert page.ranking == "bm25_trigram"
    assert len(page.hits) > 1
    assert all(hit.session_id == "passages" for hit in page.hits)
    assert all(hit.sources == ("literal",) for hit in page.hits)
    assert page.hits[0].text in original


@pytest.mark.parametrize(
    ("query", "match_mode", "expected"),
    [
        ("alpha beta", "phrase", {"phrase"}),
        ("beta alpha", "phrase", set()),
        ("missing gamma", "any_term", {"term"}),
    ],
    ids=["phrase", "phrase-order", "any-term"],
)
async def test_passage_arm_honours_the_match_mode(
    tmp_path: Path,
    sessions: ChatSessionManager,
    query: str,
    match_mode: str,
    expected: set[str],
) -> None:
    for session_id, text in (("phrase", "alpha beta"), ("term", "gamma")):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(text, timestamp=timestamp(1))
        )

    page = await backend(tmp_path, sessions).search_passages(request(query, match_mode=match_mode))

    assert {hit.session_id for hit in page.hits} == expected


async def test_passage_time_filters_include_the_bounds_as_instants(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="bounds").append(
        ChatMessage.user("bounded needle", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    instant = timestamp(1)
    # Another offset names the same instant; the bound compares as canonical text.
    local = instant.astimezone(timezone(timedelta(hours=2)))
    microsecond = timedelta(microseconds=1)

    exact = await recall.search_passages(request("needle", since=local, until=local))
    after = await recall.search_passages(request("needle", since=instant + microsecond))
    before = await recall.search_passages(request("needle", until=instant - microsecond))

    assert [hit.session_id for hit in exact.hits] == ["bounds"]
    assert after.hits == ()
    assert before.hits == ()


async def test_short_term_passages_use_the_token_index_without_loading_histories(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(6):
        sessions.create("coder", session_id=f"noise-{index}").append(
            ChatMessage.user("c c c", timestamp=timestamp(1))
        )
    for index in range(3):
        sessions.create("coder", session_id=f"csharp-{index}").append(
            ChatMessage.user(
                f"We compared C# generics with Java, part {index}", timestamp=timestamp(2)
            )
        )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(request("generics"))

    def reject_load(*_args: object) -> None:
        raise AssertionError("an indexed short-term search must not load Session histories")

    for method in ("load", "load_active", "load_since"):
        monkeypatch.setattr(ChatSession, method, reject_load)
    # The better-ranked ``c c c`` Passages fail the literal check, so pages stay full.
    first = await recall.search_passages(request("C#", limit=2))
    second = await recall.search_passages(request("C#", limit=2, offset=2))

    assert first.ranking == "bm25_token"
    assert (len(first.hits), first.has_more) == (2, True)
    assert (len(second.hits), second.has_more) == (1, False)
    assert {hit.session_id for hit in first.hits + second.hits} == {
        f"csharp-{index}" for index in range(3)
    }


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


async def test_passage_index_rewrites_only_changed_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="growing")
    session.append_many(
        [
            ChatMessage.user(f"needle part {index} " + "x" * 1000, timestamp=timestamp(index + 1))
            for index in range(4)
        ]
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(request("needle"))
    before = _stored_passages(recall)

    session.append(ChatMessage.user("needle tail " + "y" * 1000, timestamp=timestamp(9)))
    page = await recall.search_passages(request("tail"))
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


async def test_passage_index_reports_shared_fork_history_once(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    source = sessions.create("coder", session_id="source")
    for day in range(1, 4):
        source.append(ChatMessage.user(f"needle story {day} " * 120, timestamp=timestamp(day)))
    older = await sessions.fork(source.address)
    newer = await sessions.fork(source.address)
    recall = backend(tmp_path, sessions)

    complete = await recall.search_passages(request("needle", limit=20))
    without_origin = await recall.search_passages(
        request("needle", limit=20, excluded_session_ids=("source",))
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
    after_delete = await recall.search_passages(request("needle", limit=20))

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


async def test_passage_index_attributes_own_fork_content_to_the_fork(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    source = sessions.create("coder", session_id="source")
    source.append(ChatMessage.user("shared needle " * 150, timestamp=timestamp(1)))
    fork = await sessions.fork(source.address)
    await asyncio.to_thread(
        fork.append, ChatMessage.user("fork needle only " * 150, timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions)

    page = await recall.search_passages(request("needle", limit=20))

    by_session = {hit.session_id for hit in page.hits}
    assert by_session == {"source", fork.id}
    assert len({hit.passage_id for hit in page.hits}) == len(page.hits)
    assert all("fork needle only" in hit.text for hit in page.hits if hit.session_id == fork.id)
    assert all(
        "fork needle only" not in hit.text for hit in page.hits if hit.session_id == "source"
    )


# ---------------------------------------------------------------------------
# The Passage index as a kernel disposable projection
# ---------------------------------------------------------------------------


async def test_message_search_needs_no_passage_index_which_holds_only_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("Disposable recall index", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)

    page = await recall.search_page(request("disposable"))
    assert [hit.session_id for hit in page.hits] == ["one"]
    assert not recall.index_path.exists()

    await recall.search_passages(request("disposable"))
    with closing(sqlite3.connect(recall.index_path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
    assert {"passages", "passages_fts", "passages_fts_tokens"} <= tables
    assert not {"messages", "messages_fts"} & tables


def _delete(path: Path) -> None:
    path.unlink()


def _overwrite(path: Path) -> None:
    path.write_text("not sqlite", encoding="utf-8")


def _outdate(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE kernel_meta SET value = '0' WHERE key = 'projection_version'")
        connection.execute("UPDATE passages SET text = 'stale text'")


@pytest.mark.parametrize(
    "damage", [_delete, _overwrite, _outdate], ids=["deleted", "unreadable", "old-version"]
)
async def test_passage_index_is_rebuilt_as_the_current_projection(
    tmp_path: Path, sessions: ChatSessionManager, damage: Callable[[Path], None]
) -> None:
    sessions.create("coder", session_id="rebuild-session").append(
        ChatMessage.user("Disposable recall index", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(request("disposable"))
    old_identity = _index_identity(recall.index_path)
    await recall.aclose()
    damage(recall.index_path)

    restarted = backend(tmp_path, sessions)
    page = await restarted.search_passages(request("disposable"))

    assert [hit.text for hit in page.hits] == ["Disposable recall index"]
    identity = _index_identity(restarted.index_path)
    assert restarted.index_path == tmp_path / "recall" / "session_index.sqlite"
    assert identity["application_id"] == APPLICATION_IDS["recall_index"]
    assert identity["database_name"] == "recall_index"
    assert identity["projection_version"] == "1"
    assert identity["database_id"] != old_identity["database_id"]


@pytest.mark.parametrize("persistent", [False, True], ids=["once", "persistent"])
async def test_passage_index_damaged_during_a_search_is_rebuilt_once(
    tmp_path: Path,
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    persistent: bool,
) -> None:
    sessions.create("coder", session_id="damaged").append(
        ChatMessage.user("needle", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)
    await recall.search_passages(request("needle"))
    old_identity = _index_identity(recall.index_path)
    refresh = PassageCatalog.refresh
    attempts = 0

    async def damaged(self: PassageCatalog, *args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if persistent or attempts == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        await refresh(self, *args)

    monkeypatch.setattr(PassageCatalog, "refresh", damaged)

    if persistent:
        with pytest.raises(sqlite3.DatabaseError):
            await recall.search_passages(request("needle"))
    else:
        page = await recall.search_passages(request("needle"))
        assert [hit.session_id for hit in page.hits] == ["damaged"]
        assert _index_identity(recall.index_path)["database_id"] != old_identity["database_id"]
    assert attempts == 2


async def test_busy_passage_index_fails_the_search_without_discarding_it(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.recall._passage_catalog.WRITE_PATIENCE_S", 0.2)
    session = sessions.create("coder", session_id="busy")
    session.append(ChatMessage.user("needle one", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)
    await recall.search_passages(request("needle"))
    identity = _index_identity(recall.index_path)
    await asyncio.to_thread(session.append, ChatMessage.user("needle two", timestamp=timestamp(2)))

    with closing(sqlite3.connect(recall.index_path, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(DatabaseUnavailableError):
            await recall.search_passages(request("needle"))
        blocker.execute("ROLLBACK")

    assert _index_identity(recall.index_path)["database_id"] == identity["database_id"]
    page = await recall.search_passages(request("needle"))
    assert len(page.hits) == 1 and "needle two" in page.hits[0].text
