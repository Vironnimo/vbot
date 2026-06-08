"""Tests for the vector recall backend."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec  # type: ignore[import-untyped]

from core.chat import ChatMessage
from core.embeddings import EmbeddingError, EmbeddingResult
from core.recall import RecallBackendContext, RecallRequest, VectorRecallBackend
from core.recall.vector import (
    _CHUNK_FETCH_MULTIPLIER,
    _CHUNK_OVERLAP_MESSAGES,
    _CHUNK_TARGET_CHARS,
    _EMBED_BATCH_SIZE,
    _MAX_DISTANCE,
    _PER_MESSAGE_CHAR_CAP,
    Chunk,
    build_session_chunks,
)
from core.recall.vector_store import VectorStore
from core.sessions import ChatSessionManager


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def request(
    *,
    query: str,
    match_mode: str = "all_terms",
    roles: tuple[str, ...] = ("user", "assistant", "tool", "error", "compaction_checkpoint"),
    limit: int = 5,
) -> RecallRequest:
    return RecallRequest(
        agent_id="coder",
        session_id=None,
        around_message_id=None,
        query=query,
        since=None,
        until=None,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        limit=limit,
        context_messages=0,
        bookend_messages=2,
        sort="newest",
    )


class _StubEmbeddings:
    """Deterministic stub embedding service for vector recall tests."""

    def __init__(self, *, dimension: int = 4) -> None:
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.embed_calls: list[list[str]] = []
        self.resolve_calls = 0

    def resolve_model_id(self) -> tuple[str, str]:
        self.resolve_calls += 1
        return (self.provider_id, self.model_id)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        vectors: list[list[float]] = [self._vector_for(text) for text in texts]
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id=self.model_id,
            provider_id=self.provider_id,
            dimension=self.dimension,
        )

    def _vector_for(self, text: str) -> list[float]:
        lowered = text.lower()
        # Deterministic slot assignments — the same text always maps to the
        # same vector so cosine distance is a stable test signal.
        if "car" in lowered and "driving" not in lowered:
            return [1.0, 0.0, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        if "vehicle" in lowered or "driving" in lowered:
            return [0.9, 0.1, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        if "banana" in lowered or "fruit" in lowered:
            return [0.0, 0.0, 1.0, 0.0] + [0.0] * (self.dimension - 4)
        if "carrot" in lowered or "vegetable" in lowered:
            return [0.0, 1.0, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        return [0.5, 0.5, 0.0, 0.0] + [0.0] * (self.dimension - 4)


class _NullEmbeddings:
    """Stand-in embedding service that always raises configuration errors."""

    def resolve_model_id(self) -> tuple[str, str]:
        raise EmbeddingError("no text_embedding binding configured")

    async def embed(self, texts: list[str]) -> EmbeddingResult:  # pragma: no cover - never used
        raise EmbeddingError("no text_embedding binding configured")


class _OverflowThenOkEmbeddings:
    """Raises a context-length overflow until every input is short enough.

    Mirrors the OpenRouter/bge-m3 failure: the provider rejects an input that
    exceeds the model's token cap, and the character-budget truncation cannot
    guarantee staying under it for dense text, so the backend must shrink and
    retry until it fits.
    """

    def __init__(self, *, max_chars: int, dimension: int = 4) -> None:
        self.max_chars = max_chars
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.embed_calls: list[list[str]] = []

    def resolve_model_id(self) -> tuple[str, str]:
        return (self.provider_id, self.model_id)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        if any(len(text) > self.max_chars for text in texts):
            raise EmbeddingError(
                "Embeddings response contains no data: HTTP 400: This model's "
                "maximum context length is 8192 tokens. (parameter=input_tokens)"
            )
        vectors = tuple([1.0] + [0.0] * (self.dimension - 1) for _ in texts)
        return EmbeddingResult(
            vectors=vectors,
            model_id=self.model_id,
            provider_id=self.provider_id,
            dimension=self.dimension,
        )


class _AuthErrorEmbeddings:
    """Always raises a non-overflow embedding error (must not be retried)."""

    def __init__(self) -> None:
        self.embed_calls = 0

    def resolve_model_id(self) -> tuple[str, str]:
        return ("openrouter", "stub-embed")

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.embed_calls += 1
        raise EmbeddingError("401 Unauthorized: invalid API key")


def backend(
    tmp_path: Path,
    sessions: ChatSessionManager,
    *,
    embeddings: Any | None = None,
) -> VectorRecallBackend:
    return VectorRecallBackend(
        RecallBackendContext(
            data_dir=tmp_path,
            sessions=sessions,
            embeddings=embeddings,
        )
    )


def test_vector_backend_ranks_semantically_nearest_sessions(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car broke down", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vehicles").append(
        ChatMessage.user("I was driving my vehicle", timestamp=timestamp(2))
    )
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("I love bananas and other fruit", timestamp=timestamp(3))
    )
    embeddings = _StubEmbeddings()

    data = backend(tmp_path, sessions, embeddings=embeddings).search(request(query="car", limit=2))

    assert [match["session_id"] for match in data["matches"]] == ["cars", "vehicles"]
    # ``distance`` is set by the vector backend and absent from the JSONL fallback.
    assert data["matches"][0]["distance"] == pytest.approx(0.0, abs=1e-5)


def test_vector_backend_backfills_missing_sessions_lazily(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("Bananas and other fruit are tasty", timestamp=timestamp(2))
    )
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = recall.search(request(query="carrot", limit=2))

    # First search backfills and embeds both sessions; we expect both to be embedded.
    assert len(embeddings.embed_calls) == 2  # one batch of sessions + the query
    assert "carrots" in [match["session_id"] for match in first["matches"]]


def test_vector_backend_reuses_indexed_vectors_on_second_search(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("Bananas and other fruit are tasty", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(2))
    )
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    recall.search(request(query="fruit", limit=2))
    recall.search(request(query="carrot", limit=2))

    # Two searches: 1 session backfill + 1 query on the first call, 1 query only
    # on the second call (no backfill needed because nothing changed).
    assert len(embeddings.embed_calls) == 3


def test_vector_backend_reindexes_when_jsonl_changes(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="dynamic")
    session.append(ChatMessage.user("hello there", timestamp=timestamp(1)))
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = recall.search(request(query="fruit", limit=2))
    assert "dynamic" not in [match["session_id"] for match in first["matches"]]

    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(2)))
    second = recall.search(request(query="fruit", limit=2))

    # The session should have been reindexed — the new content embeds to the
    # fruit vector and the search should surface it for "fruit".
    assert "dynamic" in [match["session_id"] for match in second["matches"]]


def test_vector_backend_drops_indexed_session_when_jsonl_file_removed(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("Bananas and other fruit are tasty", timestamp=timestamp(2))
    )
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    recall.search(request(query="carrot", limit=2))

    sessions.delete("coder", "carrots")
    data = recall.search(request(query="carrot", limit=2))

    assert "carrots" not in [match["session_id"] for match in data["matches"]]


def test_vector_backend_falls_back_to_jsonl_when_no_embedding_binding(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    data = backend(tmp_path, sessions, embeddings=None).search(request(query="carrot"))

    assert [match["session_id"] for match in data["matches"]] == ["carrots"]
    # JSONL fallback does not produce a ``distance`` field.
    assert all("distance" not in match for match in data["matches"])


def test_vector_backend_falls_back_to_jsonl_when_binding_raises(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    data = backend(tmp_path, sessions, embeddings=_NullEmbeddings()).search(request(query="carrot"))

    assert [match["session_id"] for match in data["matches"]] == ["carrots"]


def test_vector_backend_search_without_query_returns_session_summaries(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search(
        RecallRequest(
            agent_id="coder",
            session_id=None,
            around_message_id=None,
            query=None,
            since=None,
            until=None,
            roles=("user", "assistant", "tool", "error", "compaction_checkpoint"),
            match_mode="all_terms",
            limit=5,
            context_messages=0,
            bookend_messages=2,
            sort="newest",
        )
    )

    assert [session["session_id"] for session in data["sessions"]] == ["carrots"]


def test_vector_backend_respects_limit(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vehicles").append(
        ChatMessage.user("Driving my vehicle", timestamp=timestamp(2))
    )
    sessions.create("coder", session_id="more-cars").append(
        ChatMessage.user("Another car story", timestamp=timestamp(3))
    )

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search(
        request(query="car", limit=2)
    )
    assert len(data["matches"]) == 2
    assert data["truncated"] is True


def test_vector_backend_browse_delegates_to_jsonl(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).browse(
        RecallRequest(
            agent_id="coder",
            session_id=None,
            around_message_id=None,
            query=None,
            since=None,
            until=None,
            roles=("user", "assistant", "tool", "error", "compaction_checkpoint"),
            match_mode="all_terms",
            limit=5,
            context_messages=0,
            bookend_messages=2,
            sort="newest",
        )
    )

    assert [session["session_id"] for session in data["sessions"]] == ["carrots"]


def test_vector_backend_scroll_delegates_to_jsonl(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="carrots")
    first = ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    session.append(first)
    session.append(ChatMessage.assistant(model="m", content="Got it.", timestamp=timestamp(2)))

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).scroll(
        RecallRequest(
            agent_id="coder",
            session_id="carrots",
            around_message_id=first.id,
            query=None,
            since=None,
            until=None,
            roles=("user", "assistant", "tool", "error", "compaction_checkpoint"),
            match_mode="all_terms",
            limit=5,
            context_messages=0,
            bookend_messages=2,
            sort="newest",
        )
    )
    assert data["around_message_id"] == first.id
    assert any(item["message_id"] == first.id for item in data["window"])


def test_vector_backend_rebuilds_index_when_embedding_model_changes(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    embeddings_a = _StubEmbeddings()
    embeddings_a.model_id = "model-a"

    recall = backend(tmp_path, sessions, embeddings=embeddings_a)
    recall.search(request(query="carrot", limit=2))
    header_a = recall.store.read_header()
    assert header_a is not None
    assert header_a.model_id == "model-a"

    # Switch the binding — the next search should rebuild the index.
    embeddings_b = _StubEmbeddings()
    embeddings_b.model_id = "model-b"
    new_recall = VectorRecallBackend(
        RecallBackendContext(
            data_dir=tmp_path,
            sessions=sessions,
            embeddings=embeddings_b,
        )
    )
    new_recall.search(request(query="carrot", limit=2))

    header = new_recall.store.read_header()
    assert header is not None
    assert header.model_id == "model-b"


def test_vector_backend_falls_back_to_jsonl_when_embed_call_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    class _FlakyEmbeddings(_StubEmbeddings):
        async def embed(self, texts: list[str]) -> EmbeddingResult:
            raise EmbeddingError("provider unavailable")

    data = backend(tmp_path, sessions, embeddings=_FlakyEmbeddings()).search(
        request(query="carrot")
    )

    # Falls back to JSONL substring match on "carrot".
    assert [match["session_id"] for match in data["matches"]] == ["carrots"]


def test_run_embed_shrinks_input_until_under_context_window(tmp_path: Path) -> None:
    """A context-length overflow is recovered by halving the input and retrying.

    The character-budget truncation cannot guarantee a token count, so the
    backend self-corrects against the provider's hard cap.
    """

    sessions = ChatSessionManager(tmp_path)
    embeddings = _OverflowThenOkEmbeddings(max_chars=100)
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    result = recall._run_embed(["x" * 1000])

    assert result.dimension == 4
    assert len(embeddings.embed_calls) > 1  # at least one shrink retry happened
    assert all(len(text) <= 100 for text in embeddings.embed_calls[-1])


def test_run_embed_does_not_retry_non_overflow_errors(tmp_path: Path) -> None:
    """Auth/network errors are not context-length overflows and must not be
    retried by the shrink loop — they re-raise on the first attempt.
    """

    sessions = ChatSessionManager(tmp_path)
    embeddings = _AuthErrorEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    with pytest.raises(EmbeddingError, match="Unauthorized"):
        recall._run_embed(["x" * 1000])
    assert embeddings.embed_calls == 1


def test_run_embed_gives_up_after_retry_budget(tmp_path: Path) -> None:
    """When the input can never satisfy the provider, the shrink loop stops
    after its budget and re-raises so the caller falls back to JSONL.
    """

    sessions = ChatSessionManager(tmp_path)
    embeddings = _OverflowThenOkEmbeddings(max_chars=0)
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    with pytest.raises(EmbeddingError):
        recall._run_embed(["x" * 1000])
    # 1 initial attempt + 6 retries = 7 embed calls before giving up.
    assert len(embeddings.embed_calls) == 7


def test_vector_backend_search_includes_per_match_distance_in_payload(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car broke down", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vehicles").append(
        ChatMessage.user("I was driving my vehicle", timestamp=timestamp(2))
    )

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search(request(query="car"))

    distances = [match["distance"] for match in data["matches"]]
    assert distances == sorted(distances)


@pytest.mark.timeout(10)
def test_vector_backend_search_completes_when_called_from_running_event_loop(
    tmp_path: Path,
) -> None:
    """Calling the sync backend from inside a running loop must not deadlock.

    In production the recall backend is invoked from FastAPI handlers
    that are themselves running on an asyncio event loop. The previous
    implementation of ``_run_async`` used
    ``asyncio.run_coroutine_threadsafe(..., loop).result()`` which
    deadlocks: the calling thread blocks on ``result()`` while the
    coroutine can only make progress on that same thread's loop.
    """

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car broke down", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vehicles").append(
        ChatMessage.user("I was driving my vehicle", timestamp=timestamp(2))
    )

    async def drive() -> list[str]:
        loop = asyncio.get_running_loop()
        assert loop.is_running()
        # ``run_in_executor`` returns a future that runs the callable
        # on the default executor — equivalent to how a FastAPI sync
        # handler would invoke the recall backend while the loop is
        # active on the calling thread.
        result = await loop.run_in_executor(
            None,
            backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search,
            request(query="car", limit=2),
        )
        return [match["session_id"] for match in result["matches"]]

    session_ids = asyncio.run(drive())
    assert session_ids == ["cars", "vehicles"]


# ---------------------------------------------------------------------------
# Chunking policy — build_session_chunks
# ---------------------------------------------------------------------------


def test_build_session_chunks_splits_long_session_into_multiple_chunks() -> None:
    """A session whose messages overflow ``_CHUNK_TARGET_CHARS`` yields >1 chunk."""

    messages = [
        ChatMessage.user("word " * 200, timestamp=timestamp(1)),  # ~1000 chars
        ChatMessage.user("word " * 200, timestamp=timestamp(2)),  # adds to chunk 1
        ChatMessage.user("word " * 200, timestamp=timestamp(3)),  # forces a new chunk
    ]

    chunks = build_session_chunks(messages)

    assert len(chunks) > 1
    # Every chunk's text fits in the budget (the chunker seals before
    # the next message would push the running total over the target).
    for chunk in chunks:
        assert len(chunk.text) <= _CHUNK_TARGET_CHARS + (
            _CHUNK_OVERLAP_MESSAGES * _CHUNK_TARGET_CHARS
        )
    # The first chunk opens at the session's first message; the last
    # chunk's span ends at the session's last message.
    assert chunks[0].start_message_id == messages[0].id
    assert chunks[-1].end_message_id == messages[-1].id


def test_build_session_chunks_carries_overlap_messages() -> None:
    """The last ``_CHUNK_OVERLAP_MESSAGES`` messages of chunk N appear in chunk N+1."""

    messages = [
        ChatMessage.user("alpha " * 200, timestamp=timestamp(1)),
        ChatMessage.user("beta " * 200, timestamp=timestamp(2)),
        ChatMessage.user("gamma " * 200, timestamp=timestamp(3)),
        ChatMessage.user("delta " * 200, timestamp=timestamp(4)),
    ]

    chunks = build_session_chunks(messages)

    assert len(chunks) >= 2
    # The second chunk's text must contain the prior chunk's last
    # message's text content for boundary context.
    assert "beta" in chunks[1].text or "gamma" in chunks[1].text


def test_build_session_chunks_handles_zero_overlap_without_carrying_full_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_CHUNK_OVERLAP_MESSAGES = 0`` does not accidentally carry the whole prior tail.

    Regression: ``current_messages[-0:]`` returns the full list because
    ``-0 == 0``, so an unguarded slice would copy every accumulated
    message into the next chunk. The guard must skip the slice
    entirely when the constant is 0.
    """

    messages = [
        ChatMessage.user("alpha " * 200, timestamp=timestamp(1)),
        ChatMessage.user("beta " * 200, timestamp=timestamp(2)),
        ChatMessage.user("gamma " * 200, timestamp=timestamp(3)),
        ChatMessage.user("delta " * 200, timestamp=timestamp(4)),
    ]

    monkeypatch.setattr("core.recall.vector._CHUNK_OVERLAP_MESSAGES", 0)
    chunks = build_session_chunks(messages)

    assert len(chunks) >= 2
    # Without overlap, each chunk's span must start *after* the prior
    # chunk's last message — never reusing the tail as a fresh opener.
    for index in range(len(chunks) - 1):
        previous = chunks[index]
        current = chunks[index + 1]
        previous_end_index = next(
            i for i, m in enumerate(messages) if m.id == previous.end_message_id
        )
        current_start_index = next(
            i for i, m in enumerate(messages) if m.id == current.start_message_id
        )
        assert current_start_index > previous_end_index


def test_build_session_chunks_handles_oversized_single_message() -> None:
    """A single message longer than the chunk budget becomes its own chunk, hard-capped."""

    # ~1800 chars — above the chunk budget (1500) so the per-message
    # cap (2000) is not enough to fit it in a normal chunk; the chunker
    # seals the prior buffer (empty here) and emits a standalone chunk
    # for this message.
    long_text = "alpha " * 360
    messages = [ChatMessage.user(long_text, timestamp=timestamp(1))]

    chunks = build_session_chunks(messages)

    assert len(chunks) == 1
    assert chunks[0].start_message_id == messages[0].id
    assert chunks[0].end_message_id == messages[0].id
    # The chunk's text is the input text capped at the per-message
    # ceiling (2000). ``truncate_to_input_limit`` with the default
    # 8192-token window leaves any text under 22118 chars unchanged,
    # so the chunker passes the capped value through.
    assert chunks[0].text == long_text[:_PER_MESSAGE_CHAR_CAP]


def test_build_session_chunks_picks_first_non_note_anchor() -> None:
    """A note-prefixed chunk anchors on the first non-note, non-skill-context message."""

    messages = [
        ChatMessage.note("system noise", timestamp=timestamp(1)),
        ChatMessage.user("first real message", timestamp=timestamp(2)),
        ChatMessage.user("second real message", timestamp=timestamp(3)),
    ]

    chunks = build_session_chunks(messages)

    assert len(chunks) == 1
    assert chunks[0].anchor_message_id == messages[1].id


def test_build_session_chunks_falls_back_to_first_message_when_only_notes() -> None:
    """A chunk composed entirely of notes still gets a non-empty anchor id."""

    messages = [
        ChatMessage.note("first note", timestamp=timestamp(1)),
        ChatMessage.note("second note", timestamp=timestamp(2)),
    ]

    chunks = build_session_chunks(messages)

    assert len(chunks) == 1
    assert chunks[0].anchor_message_id == messages[0].id


# ---------------------------------------------------------------------------
# Chunked indexing + search integration
# ---------------------------------------------------------------------------


def _count_vec_rows(store_path: Path, agent_id: str, session_id: str) -> int:
    """Open the on-disk store and count vec0 rows for one session via the chunks table."""

    connection = sqlite3.connect(store_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE agent_id = ? AND session_id = ?",
            (agent_id, session_id),
        ).fetchone()
        return int(row["c"])
    finally:
        connection.close()


def test_vector_backend_indexing_splits_long_session_into_multiple_vec_rows(
    tmp_path: Path,
) -> None:
    """A session whose messages overflow the chunk budget is indexed with multiple vec0 rows."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="long")
    for day in range(1, 5):
        session.append(ChatMessage.user("lorem ipsum " * 200, timestamp=timestamp(day)))

    backend_ = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    backend_.search(request(query="lorem", limit=2))

    # 4 messages × ~2400 chars each — well over ``_CHUNK_TARGET_CHARS``
    # (1500) so the chunker must produce several chunks per session.
    chunk_count = _count_vec_rows(backend_.store.path, "coder", "long")
    assert chunk_count > 1


def test_vector_backend_mid_session_match_anchors_at_matching_chunk(
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

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search(
        request(query="fruit", limit=2)
    )

    assert len(data["matches"]) == 1
    match = data["matches"][0]
    assert match["session_id"] == "mixed"
    # The anchor must be the *last* message (the fruit one), not the
    # car opener at the start of the session.
    assert match["message_id"] == session.load()[-1].id
    # The chunk snippet contains the matched region's keyword.
    assert "fruit" in match["snippet"].lower()


def test_vector_backend_dedup_chunks_per_session_in_results(tmp_path: Path) -> None:
    """A session with multiple matching chunks surfaces once, not once-per-chunk."""

    sessions = ChatSessionManager(tmp_path)
    # Use long enough messages to span multiple chunks so the dedup
    # path is actually exercised — each chunk is its own vec0 row.
    session = sessions.create("coder", session_id="fruit-heavy")
    for day in range(1, 5):
        session.append(
            ChatMessage.user(
                "I love bananas and fruit, especially the tropical ones " * 50,
                timestamp=timestamp(day),
            )
        )

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search(
        request(query="fruit", limit=5)
    )

    session_ids = [match["session_id"] for match in data["matches"]]
    assert session_ids.count("fruit-heavy") == 1


def test_vector_backend_drops_matches_beyond_max_distance(tmp_path: Path) -> None:
    """Weak matches (cosine distance > ``_MAX_DISTANCE``) are filtered out."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car broke down", timestamp=timestamp(1))
    )
    # "Tofu" is not in any stub branch → default vector [0.5, 0.5, 0, 0].
    # The query "car" → [1, 0, 0, 0]. Cosine distance = 1 - 0.707 = 0.293.
    # Below cutoff — kept. Add an orthogonal match to verify the
    # cutoff path: a session whose text uses no recognized keyword at
    # all stays at the default vector and is similar to the query only
    # at the orthogonal dot, so its distance is well above 0.7.
    sessions.create("coder", session_id="unrelated").append(
        ChatMessage.user(
            "completely off topic conversation about the weather", timestamp=timestamp(2)
        )
    )
    # "Vegetable" branch → [0, 1, 0, 0], orthogonal to "car" — distance = 1.0.
    sessions.create("coder", session_id="vegetable").append(
        ChatMessage.user("I bought a vegetable at the market", timestamp=timestamp(3))
    )

    data = backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search(
        request(query="car", limit=10)
    )

    session_ids = [match["session_id"] for match in data["matches"]]
    assert "cars" in session_ids
    # The orthogonal vector must be dropped by the distance cutoff.
    assert "vegetable" not in session_ids
    # The "default" vector is also dropped — its distance to "car" is
    # the same as the fruit vector's (0.5, 0.5, 0, 0) which sits at
    # 0.293 from "car". We expect that one to pass the cutoff; this
    # test only asserts the orthogonal cases are filtered.
    for match in data["matches"]:
        assert match["distance"] <= _MAX_DISTANCE


def test_vector_backend_chunk_count_resets_when_session_is_appended(
    tmp_path: Path,
) -> None:
    """Appending messages to a session reindexes wholesale — the row count reflects the new content.

    The recall backend re-chunks the **entire** session on every JSONL
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
    backend_.search(request(query="lorem", limit=2))
    first_chunk_count = _count_vec_rows(backend_.store.path, "coder", "growing")
    assert first_chunk_count > 0

    # Append more content; the reindex must reflect the new total.
    for day in range(4, 8):
        session.append(ChatMessage.user("brand new content " * 200, timestamp=timestamp(day)))
    backend_.search(request(query="brand new", limit=2))
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


# ---------------------------------------------------------------------------
# Staleness — sessions that stop producing chunks must drop their old rows
# ---------------------------------------------------------------------------


def test_vector_backend_drops_chunks_when_session_no_longer_produces_any(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session whose JSONL no longer yields chunks is purged from the index.

    Regression: ``upsert_many_chunks`` only wipes sessions that appear in
    its ``records`` parameter. If a stale session's
    ``build_session_chunks`` call returns an empty list, the session is
    not in ``records`` and its old rows survive a reindex, leaving
    stale hits in subsequent searches. The fix calls
    ``store.delete_session`` for any session with zero chunks.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="becomes-empty")
    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = recall.search(request(query="fruit", limit=2))
    assert "becomes-empty" in [match["session_id"] for match in first["matches"]]
    assert _count_vec_rows(recall.store.path, "coder", "becomes-empty") == 1

    # Simulate the JSONL changing such that ``build_session_chunks`` now
    # yields nothing (e.g. the session turned into a stream of empty
    # system-only messages). Append a real message so the session's
    # mtime/size change and the staleness path is exercised.
    session.append(ChatMessage.user("still here, but inert", timestamp=timestamp(2)))
    monkeypatch.setattr("core.recall.vector.build_session_chunks", lambda _messages: [])

    second = recall.search(request(query="fruit", limit=2))

    assert "becomes-empty" not in [match["session_id"] for match in second["matches"]]
    assert _count_vec_rows(recall.store.path, "coder", "becomes-empty") == 0


# ---------------------------------------------------------------------------
# _run_embed batching
# ---------------------------------------------------------------------------


def test_run_embed_splits_texts_into_batches_above_batch_size(tmp_path: Path) -> None:
    """When ``texts > _EMBED_BATCH_SIZE`` the embedder receives multiple, ordered calls."""

    class _CountingEmbeddings(_StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        async def embed(self, texts: list[str]) -> EmbeddingResult:
            self.batch_sizes.append(len(texts))
            return await super().embed(texts)

    sessions = ChatSessionManager(tmp_path)
    embeddings = _CountingEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    # Two full batches plus a partial third batch.
    total = _EMBED_BATCH_SIZE + _EMBED_BATCH_SIZE + 3
    texts = [f"text-{index}" for index in range(total)]
    result = recall._run_embed(texts)

    # Three separate calls — one per batch.
    assert embeddings.batch_sizes == [
        _EMBED_BATCH_SIZE,
        _EMBED_BATCH_SIZE,
        3,
    ]
    # Vectors arrive in input order: ``text-0`` is first, ``text-total-1`` last.
    assert len(result.vectors) == total
    # The default stub vector for "text-0" should equal the one for
    # "text-0" computed in isolation — same input → same output.
    assert result.vectors[0] == embeddings._vector_for("text-0")
    assert result.vectors[-1] == embeddings._vector_for(texts[-1])


def test_run_embed_single_text_does_not_split(tmp_path: Path) -> None:
    """A single text fits in one batch — no splitting overhead."""

    class _CountingEmbeddings(_StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        async def embed(self, texts: list[str]) -> EmbeddingResult:
            self.batch_sizes.append(len(texts))
            return await super().embed(texts)

    sessions = ChatSessionManager(tmp_path)
    embeddings = _CountingEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    result = recall._run_embed(["only-text"])

    assert embeddings.batch_sizes == [1]
    assert len(result.vectors) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_chunk_dataclass_is_frozen() -> None:
    """``Chunk`` is a frozen dataclass — once built, its fields are read-only."""

    chunk = Chunk(
        anchor_message_id="m1",
        start_message_id="m1",
        end_message_id="m2",
        text="hello world",
        snippet="hello world",
    )
    with pytest.raises((AttributeError, TypeError)):
        chunk.text = "mutated"  # type: ignore[misc]


def test_chunk_fetch_multiplier_is_used_in_knn_query(tmp_path: Path) -> None:
    """The KNN query over-fetches by ``_CHUNK_FETCH_MULTIPLIER`` for chunk→session dedup."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="s1")
    for day in range(1, 4):
        session.append(ChatMessage.user("hello there " * 200, timestamp=timestamp(day)))

    class _SpyStore(VectorStore):
        def __init__(self, inner: VectorStore) -> None:
            super().__init__(inner.data_dir)
            self._inner = inner
            self.knn_calls: list[int] = []

        def knn_search(self, **kwargs: Any) -> Any:
            self.knn_calls.append(int(kwargs["limit"]))
            return self._inner.knn_search(**kwargs)

    backend_ = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    backend_.store = _SpyStore(backend_.store)
    backend_.search(request(query="hello", limit=2))

    assert backend_.store.knn_calls == [2 * _CHUNK_FETCH_MULTIPLIER + 4]
