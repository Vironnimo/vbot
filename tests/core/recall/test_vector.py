"""Tests for the vector recall backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.embeddings import EmbeddingError, EmbeddingResult
from core.recall import RecallBackendContext, RecallRequest, VectorRecallBackend
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
    session.append(ChatMessage.user("I bought some carrots", timestamp=timestamp(1)))
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = recall.search(request(query="carrot", limit=2))
    assert "dynamic" in [match["session_id"] for match in first["matches"]]

    session.append(ChatMessage.user("Now I prefer fruit", timestamp=timestamp(2)))
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
