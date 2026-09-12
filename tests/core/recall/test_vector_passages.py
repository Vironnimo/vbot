"""Vector: passages behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec  # type: ignore[import-untyped]

from core.chat import ChatMessage
from core.sessions import ChatSessionManager
from tests.core.recall.vector_helpers import (
    _count_vec_rows,
    _StubEmbeddings,
    backend,
    request,
    timestamp,
)

pytestmark = pytest.mark.asyncio


async def test_vector_backend_indexing_splits_long_session_into_multiple_vec_rows(
    tmp_path: Path,
) -> None:
    """A session whose messages overflow the chunk budget is indexed with multiple vec0 rows."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="long")
    for day in range(1, 5):
        session.append(ChatMessage.user("lorem ipsum " * 200, timestamp=timestamp(day)))

    backend_ = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    await backend_.search_page(request(query="lorem", limit=2))

    # 4 messages × ~2400 chars each — well over ``_CHUNK_TARGET_CHARS``
    # (1500) so the chunker must produce several chunks per session.
    chunk_count = _count_vec_rows(backend_.store.path, "coder", "long")
    assert chunk_count > 1


async def test_vector_backend_mid_session_match_anchors_at_matching_chunk(
    tmp_path: Path,
) -> None:
    """A query whose match is in the *middle* of a long session is anchored there.

    Regression for the ``Bild``-style failure: the first chunk's anchor
    was previously the session opener, so a search for content that
    only appears later in the session would return the wrong snippet.
    With chunk-level vectors the matching chunk's anchor — the message
    near the match — is returned instead.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="mixed")
    session.append(ChatMessage.user("My car broke down on the highway", timestamp=timestamp(1)))
    # Filler messages long enough to push the "fruit" message into its
    # own chunk.
    for day in range(2, 5):
        session.append(
            ChatMessage.user("unrelated filler content " * 200, timestamp=timestamp(day))
        )
    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(5)))

    data = await backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search_page(
        request(query="fruit", limit=2)
    )

    assert data.hits
    match = data.hits[0]
    assert match.session_id == "mixed"
    # The anchor must be the *last* message (the fruit one), not the
    # car opener at the start of the session.
    assert match.end_message_id == session.load()[-1].id
    # The chunk snippet contains the matched region's keyword.
    assert "fruit" in match.text.lower()


async def test_vector_backend_chunk_count_resets_when_session_is_appended(
    tmp_path: Path,
) -> None:
    """Appending messages to a session reindexes wholesale — the row count reflects the new content.

    The recall backend re-chunks the **entire** session on every canonical
    change (chunks are not deltas). After appending new content the
    chunk table must hold rows whose chunk text comes from the
    up-to-date message list, with no rows left over from the prior
    pass — ``upsert_many_chunks`` wipes the session's chunks before
    inserting the fresh batch.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="growing")
    for day in range(1, 4):
        session.append(ChatMessage.user("lorem ipsum " * 200, timestamp=timestamp(day)))

    backend_ = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    await backend_.search_page(request(query="lorem", limit=2))
    first_chunk_count = _count_vec_rows(backend_.store.path, "coder", "growing")
    assert first_chunk_count > 0

    # Append more content; the reindex must reflect the new total.
    for day in range(4, 8):
        session.append(ChatMessage.user("brand new content " * 200, timestamp=timestamp(day)))
    await backend_.search_page(request(query="brand new", limit=2))
    second_chunk_count = _count_vec_rows(backend_.store.path, "coder", "growing")
    assert second_chunk_count > 0
    # The new total message count is higher, so the reindexed chunk
    # count must be at least as large (the chunker produces the same
    # number of chunks for a uniform message stream regardless of
    # message count, but never fewer).
    assert second_chunk_count >= first_chunk_count

    # Read every chunk's text to confirm the reindex covered the new
    # content. The chunk table must not hold a row referencing only
    # the old "lorem ipsum" stream — the wholesale delete-then-insert
    # in ``upsert_many_chunks`` is what guarantees that.
    connection = sqlite3.connect(backend_.store.path)
    try:
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        chunk_texts = [
            str(row["snippet"])
            for row in connection.execute(
                "SELECT snippet FROM chunks WHERE agent_id = ? AND session_id = ?",
                ("coder", "growing"),
            ).fetchall()
        ]
    finally:
        connection.close()
    # At least one chunk's snippet must reference the new content.
    assert any("brand new" in snippet.lower() for snippet in chunk_texts)


# Staleness — sessions that stop producing chunks must drop their old rows
async def test_vector_backend_drops_chunks_when_session_no_longer_produces_any(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session whose canonical no longer yields chunks is purged from the index.

    Regression: ``upsert_many_chunks`` only wipes sessions that appear in
    its ``records`` parameter. If a stale session's
    ``build_session_passages`` call returns an empty list, the session is
    not in ``records`` and its old rows survive a reindex, leaving
    stale hits in subsequent searches. The fix calls
    ``store.delete_session`` for any session with zero chunks.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="becomes-empty")
    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = await recall.search_page(request(query="fruit", limit=2))
    assert "becomes-empty" in [match.session_id for match in first.hits]
    assert _count_vec_rows(recall.store.path, "coder", "becomes-empty") == 1

    # Simulate the canonical history changing such that ``build_session_passages`` now
    # yields nothing (e.g. the session turned into a stream of empty
    # system-only messages). Append a real message so the session's
    # mtime/size change and the staleness path is exercised.
    session.append(ChatMessage.user("still here, but inert", timestamp=timestamp(2)))
    monkeypatch.setattr("core.recall.vector.build_session_passages", lambda _messages: [])

    second = await recall.search_page(request(query="fruit", limit=2))

    assert "becomes-empty" not in [match.session_id for match in second.hits]
    assert _count_vec_rows(recall.store.path, "coder", "becomes-empty") == 0


async def test_vector_backend_search_succeeds_when_first_indexed_session_yields_no_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-chunk session on a brand-new index must not crash the search.

    Regression for ``no such table: chunks``: on a fresh index the eager
    backfill calls ``store.delete_session`` for any candidate session
    whose ``build_session_passages`` returns nothing — and that happens
    *before* any upsert has created the chunk table. The delete must be
    a no-op on a schema-less store rather than raising a bare
    ``sqlite3.OperationalError`` that escapes the canonical fallback.
    """

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="empty-ish").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    monkeypatch.setattr("core.recall.vector.build_session_passages", lambda _messages: [])

    # Must not raise. With nothing indexed the KNN has no candidates, so the
    # semantic search returns zero matches gracefully (an empty index is a
    # valid state, not an error — the bug was the bare ``no such table``).
    data = await backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search_page(
        request(query="carrot")
    )

    assert data.hits == ()


async def test_vector_backend_never_surfaces_run_summary_as_a_match(tmp_path: Path) -> None:
    """run_summary annotations must never appear as recall results.

    Regression: run_summary is not a supported recall role, yet the vector
    backend used to anchor chunks on it and return it (empty-snippet,
    clustered-distance noise). The chunk must anchor on the real message and
    the result role must be that message's role.
    """

    timing = {
        "started_at": "2026-05-01T12:00:00+00:00",
        "completed_at": "2026-05-01T12:00:01+00:00",
        "duration_ms": 1000,
    }
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="mixed")
    session.append(
        ChatMessage.run_summary(run_id="r1", status="completed", timing=timing, iteration_count=1)
    )
    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))

    data = await backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search_page(
        request(query="fruit", limit=5)
    )

    assert [match.session_id for match in data.hits] == ["mixed"]
    assert all(match.role != "run_summary" for match in data.hits)
    assert data.hits[0].role == "user"


async def test_vector_backend_default_search_snippet_is_conversation_not_tool_headline(
    tmp_path: Path,
) -> None:
    """A default (tool-excluded) search renders the conversation anchor, never tool JSON.

    The chunk embeds every role, so a tool result's text is part of the chunk's
    headline. When a default search surfaces such a chunk, hydration re-anchors
    onto the request-eligible conversation message and the snippet must come from
    that message — not the chunk headline, which would leak the raw tool output.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="mixed")
    # Tool result first → it is the chunk's recorded anchor and the head of the
    # chunk headline. Its text carries the "fruit" signal so the chunk matches.
    session.append(
        ChatMessage.tool(
            tool_call_id="c1",
            name="bash",
            content="banana fruit raw ansi terminal dump",
            timestamp=timestamp(1),
        )
    )
    session.append(ChatMessage.user("I love fruit too", timestamp=timestamp(2)))

    conversation_only = ("user", "assistant", "error", "compaction_checkpoint")
    data = await backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search_page(
        request(query="fruit", roles=conversation_only, limit=5)
    )

    assert data.hits
    match = data.hits[0]
    # Re-anchored onto the conversation message, not the tool result.
    assert match.role == "user"
    assert match.end_message_id == session.load()[-1].id
    # The snippet is the conversation message, with no tool noise leaking in.
    assert "fruit" in match.text.lower()
    assert "ansi" not in match.text.lower()
