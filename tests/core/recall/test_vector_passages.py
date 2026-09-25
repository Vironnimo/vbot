"""Vector: passages behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.recall.passages import build_session_passages
from core.sessions import ChatSessionManager
from tests.core.recall.vector_helpers import (
    _count_vec_rows,
    _passage_rows,
    _StubEmbeddings,
    backend,
    request,
    timestamp,
)
from tests.core.sessions.history_fixtures import append_tool_fixture, complete_run

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


async def test_vector_backend_append_embeds_only_new_or_changed_passages(
    tmp_path: Path,
) -> None:
    """Appending to an indexed Session embeds only Passages that did not exist before.

    Unchanged Passages keep their rows and vectors. The former tail Passage may
    grow, and new Passages cover the appended turn; only texts without a stored
    vector reach the embedding provider.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="growing")
    for day in range(1, 8):
        session.append(ChatMessage.user(f"turn {day} lorem ipsum " * 150, timestamp=timestamp(day)))
    embeddings = _StubEmbeddings()
    backend_ = backend(tmp_path, sessions, embeddings=embeddings)
    await backend_.search_page(request(query="lorem", limit=2))
    old_passages = build_session_passages(session.load_active())
    before = _passage_rows(backend_.store.path, "coder", "growing")
    assert sorted(before.values()) == sorted(passage.text for passage in old_passages)

    session.append(ChatMessage.user("brand new content " * 20, timestamp=timestamp(8)))
    documents_before = len(embeddings.document_inputs)
    await backend_.search_page(request(query="brand new", limit=2))

    new_passages = build_session_passages(session.load_active())
    added = [passage for passage in new_passages if passage not in old_passages]
    vanished = [passage for passage in old_passages if passage not in new_passages]
    embedded = embeddings.document_inputs[documents_before:]
    assert sorted(embedded) == sorted(
        {passage.text for passage in added} - {passage.text for passage in old_passages}
    )
    assert 0 < len(embedded) < len(new_passages)
    after = _passage_rows(backend_.store.path, "coder", "growing")
    assert sorted(after.values()) == sorted(passage.text for passage in new_passages)
    # Every unchanged Passage keeps its row; only vanished Passages lose theirs.
    kept = set(before.items()) & set(after.items())
    assert len(kept) == len(old_passages) - len(vanished)
    assert any("brand new" in text for text in after.values())


# Staleness — sessions that stop producing chunks must drop their old rows
async def test_vector_backend_drops_chunks_when_session_no_longer_produces_any(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Session whose canonical history no longer yields Passages is purged.

    Its stale rows must not survive the refresh and surface in later searches.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="becomes-empty")
    session.start_run("r2").append(
        ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1))
    )
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
    monkeypatch.setattr("core.recall._passage_catalog.build_session_passages", lambda _messages: [])

    second = await recall.search_page(request(query="fruit", limit=2))

    assert "becomes-empty" not in [match.session_id for match in second.hits]
    assert _count_vec_rows(recall.store.path, "coder", "becomes-empty") == 0


async def test_vector_backend_search_succeeds_when_first_indexed_session_yields_no_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-Passage Session on a brand-new index must not crash the search.

    The first refresh creates the schema for a Session that contributes no
    rows, then records its freshness stamp so later searches skip it.
    """

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="empty-ish").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    monkeypatch.setattr("core.recall._passage_catalog.build_session_passages", lambda _messages: [])

    # Must not raise. With nothing indexed the KNN has no candidates, so the
    # semantic search returns zero matches gracefully (an empty index is a
    # valid state, not an error — the bug was the bare ``no such table``).
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    data = await recall.search_page(request(query="carrot"))

    assert data.hits == ()
    assert set(await recall.store.list_indexed_sessions("coder")) == {"empty-ish"}


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
    session = session.start_run("r1")
    complete_run(
        session,
        ChatMessage.run_summary(run_id="r1", status="completed", timing=timing, iteration_count=1),
    )
    session.start_run("r2").append(
        ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1))
    )

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
    append_tool_fixture(
        session,
        ChatMessage.tool(
            tool_call_id="c1",
            name="bash",
            content="banana fruit raw ansi terminal dump",
            timestamp=timestamp(1),
        ),
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
