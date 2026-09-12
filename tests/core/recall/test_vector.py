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
from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingSpaceIdentity,
    EmbeddingUsage,
)
from core.recall import (
    RecallBackendContext,
    RecallSearchError,
    RecallSearchRequest,
    VectorRecallBackend,
)
from core.recall.vector import _EMBED_BATCH_SIZE
from core.sessions import ChatSessionManager, SessionAddress

pytestmark = pytest.mark.asyncio


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def request(
    *,
    query: str,
    match_mode: str = "all_terms",
    roles: tuple[str, ...] = ("user", "assistant", "tool", "error", "compaction_checkpoint"),
    limit: int = 5,
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


class _StubEmbeddings:
    """Deterministic stub embedding service for vector recall tests."""

    def __init__(self, *, dimension: int = 4) -> None:
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.response_model_id = ""
        self.space_fingerprint = "stub-space-a"
        self.embed_calls: list[list[str]] = []
        self.embed_purposes: list[str | None] = []
        self.resolve_calls = 0

    def resolve_model_id(self) -> tuple[str, str]:
        self.resolve_calls += 1
        return (self.provider_id, self.model_id)

    def resolve_space(self) -> EmbeddingSpaceIdentity:
        self.resolve_calls += 1
        return EmbeddingSpaceIdentity(
            provider_id=self.provider_id,
            model_id=self.model_id,
            fingerprint=self.space_fingerprint,
        )

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        self.embed_purposes.append(purpose)
        vectors: list[list[float]] = [self._vector_for(text) for text in texts]
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id=self.model_id,
            provider_id=self.provider_id,
            dimension=self.dimension,
            space_fingerprint=self.space_fingerprint,
            response_model_id=self.response_model_id,
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

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:  # pragma: no cover - never used
        del purpose
        raise EmbeddingError("no text_embedding binding configured")


class _OverflowThenOkEmbeddings:
    """Raises a context-length overflow until the aggregate batch fits.

    This models providers that apply the token cap to one whole batch. Every
    individual text can fit unchanged once the backend recursively divides the
    request.
    """

    def __init__(self, *, max_chars: int, dimension: int = 4) -> None:
        self.max_chars = max_chars
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.embed_calls: list[list[str]] = []

    def resolve_model_id(self) -> tuple[str, str]:
        return (self.provider_id, self.model_id)

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:
        del purpose
        self.embed_calls.append(list(texts))
        if sum(len(text) for text in texts) > self.max_chars:
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

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:
        del texts, purpose
        self.embed_calls += 1
        raise EmbeddingError("401 Unauthorized: invalid API key")


class _CapturingLogger:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, tuple[object, ...]]] = []
        self.warning_calls: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.info_calls.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warning_calls.append((message, args))


def backend(
    tmp_path: Path,
    sessions: ChatSessionManager,
    *,
    embeddings: Any | None = None,
    logger: Any | None = None,
) -> VectorRecallBackend:
    return VectorRecallBackend(
        RecallBackendContext(
            data_dir=tmp_path,
            sessions=sessions,
            embeddings=embeddings,
            logger=logger,
        )
    )


def search_request(
    query: str,
    *,
    limit: int = 10,
    since: datetime | None = None,
    session_id: str | None = None,
    offset: int = 0,
    snapshot_id: str | None = None,
) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        session_id=session_id,
        query=query,
        since=since,
        until=None,
        roles=("user", "assistant", "error", "compaction_checkpoint"),
        match_mode="all_terms",
        order="relevance",
        offset=offset,
        limit=limit,
        snapshot_id=snapshot_id,
    )


async def test_typed_vector_search_returns_ranked_passages_without_session_dedup(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="fruit-heavy").append(
        ChatMessage.user("fruit banana " * 400, timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit"))

    assert page.result_type == "passage"
    assert page.ranking == "cosine_distance"
    assert len(page.hits) > 1
    assert {hit.session_id for hit in page.hits} == {"fruit-heavy"}
    assert len({hit.passage_id for hit in page.hits}) == len(page.hits)
    assert all(hit.sources == ("semantic",) for hit in page.hits)


async def test_vector_search_skips_session_deleted_during_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="deleted-during-index")
    session.append(ChatMessage.user("banana fruit", timestamp=timestamp(1)))
    address = SessionAddress(
        project_id=None,
        agent_id="coder",
        session_id=session.id,
    )
    list_history_versions = sessions.list_history_versions
    deleted = False

    def list_then_delete(addresses):
        nonlocal deleted
        versions = list_history_versions(addresses)
        if not deleted:
            sessions.delete(address)
            deleted = True
        return versions

    monkeypatch.setattr(sessions, "list_history_versions", list_then_delete)
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit"))

    assert page.hits == ()


async def test_typed_vector_search_has_no_literal_fallback_or_distance_cutoff(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vegetable").append(
        ChatMessage.user("carrot vegetable", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit", limit=10))

    assert [hit.session_id for hit in page.hits] == ["fruit", "vegetable"]
    assert page.hits[-1].score > 0.7

    none_dir = tmp_path / "none"
    none_dir.mkdir()
    from core.sessions.format import write_bootstrap_marker

    write_bootstrap_marker(none_dir)
    unavailable = backend(none_dir, ChatSessionManager(none_dir))
    with pytest.raises(RecallSearchError):
        await unavailable.search_page(search_request("literal"))


async def test_typed_vector_search_prefilters_time_inside_knn(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="old").append(
        ChatMessage.user("banana fruit old", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="new").append(
        ChatMessage.user("banana fruit new", timestamp=timestamp(3))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit", since=datetime(2026, 5, 2, tzinfo=UTC)))

    assert [hit.session_id for hit in page.hits] == ["new"]


async def test_typed_filtered_search_keeps_other_scope_sessions_indexed(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit one", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="two").append(
        ChatMessage.user("banana fruit two", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    await recall.search_page(search_request("fruit"))
    assert set(recall.store.list_indexed_sessions("coder")) == {"one", "two"}

    page = await recall.search_page(search_request("fruit", session_id="one"))

    assert {hit.session_id for hit in page.hits} == {"one"}
    assert set(recall.store.list_indexed_sessions("coder")) == {"one", "two"}


async def test_typed_search_rebuilds_full_scope_on_native_dimension_change(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    for session_id, day in (("one", 1), ("two", 2)):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(f"banana fruit {session_id}", timestamp=timestamp(day))
        )
    embeddings = _StubEmbeddings(dimension=4)
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(search_request("fruit"))

    embeddings.dimension = 6
    page = await recall.search_page(search_request("fruit"))

    assert {hit.session_id for hit in page.hits} == {"one", "two"}
    header = recall.store.read_header()
    assert header is not None
    assert header.dimension == 6
    assert set(recall.store.list_indexed_sessions("coder")) == {"one", "two"}


async def test_typed_search_rebuilds_when_execution_fingerprint_changes(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(search_request("fruit"))
    calls_before_switch = len(embeddings.embed_calls)

    embeddings.space_fingerprint = "stub-space-b"
    page = await recall.search_page(search_request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert len(embeddings.embed_calls) >= calls_before_switch + 2
    header = recall.store.read_header()
    assert header is not None
    assert header.space_fingerprint == "stub-space-b"


async def test_typed_search_discards_corrupt_index_once_and_rebuilds(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    await recall.search_page(search_request("fruit"))
    recall.store.path.write_bytes(b"not a sqlite database")

    page = await recall.search_page(search_request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert recall.store.read_header() is not None


async def test_typed_search_embeds_documents_and_query_with_explicit_purposes(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    await recall.search_page(search_request("fruit"))

    assert embeddings.embed_purposes == ["document", "query"]


async def test_typed_search_rebuilds_when_provider_response_model_changes(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = _StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    logger = _CapturingLogger()
    recall = backend(tmp_path, sessions, embeddings=embeddings, logger=logger)
    first_page = await recall.search_page(search_request("fruit"))
    continuation = await recall.search_page(
        search_request(
            "fruit",
            offset=1,
            snapshot_id=first_page.snapshot_id,
        )
    )

    embeddings.response_model_id = "served/embed-b"
    with pytest.raises(RecallSearchError) as error_info:
        await recall.search_page(
            search_request(
                "fruit",
                offset=1,
                snapshot_id=first_page.snapshot_id,
            )
        )
    page = await recall.search_page(search_request("fruit"))

    assert continuation.snapshot_id == first_page.snapshot_id
    assert error_info.value.code == "stale_cursor"
    assert [hit.session_id for hit in page.hits] == ["one"]
    header = recall.store.read_header()
    assert header is not None
    assert header.model_id == "stub-embed"
    assert header.response_model_id == "served/embed-b"
    assert any("response model changed" in message for message, _args in logger.warning_calls)


async def test_typed_search_rebuilds_when_response_model_drifts_during_backfill(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="one")
    session.append(ChatMessage.user("banana fruit", timestamp=timestamp(1)))
    embeddings = _StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(search_request("fruit"))

    session.append(ChatMessage.user("another banana", timestamp=timestamp(2)))
    embeddings.response_model_id = "served/embed-b"
    page = await recall.search_page(search_request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    header = recall.store.read_header()
    assert header is not None
    assert header.response_model_id == "served/embed-b"


async def test_run_embed_rejects_actual_model_drift_between_batches(tmp_path: Path) -> None:
    class _DriftingEmbeddings(_StubEmbeddings):
        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            self.response_model_id = f"served/embed-{len(self.embed_calls)}"
            return await super().embed(texts, purpose=purpose)

    sessions = ChatSessionManager(tmp_path)
    recall = backend(tmp_path, sessions, embeddings=_DriftingEmbeddings())
    texts = [f"text-{index}" for index in range(_EMBED_BATCH_SIZE + 1)]

    with pytest.raises(EmbeddingError):
        await recall._run_embed(texts)


async def test_typed_search_logs_usage_aggregated_across_rebuild_batches(
    tmp_path: Path,
) -> None:
    class _UsageEmbeddings(_StubEmbeddings):
        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            result = await super().embed(texts, purpose=purpose)
            return EmbeddingResult(
                vectors=result.vectors,
                model_id=result.model_id,
                provider_id=result.provider_id,
                dimension=result.dimension,
                space_fingerprint=result.space_fingerprint,
                response_model_id=result.response_model_id,
                usage=EmbeddingUsage(
                    requests=1,
                    token_reports=1,
                    cost_reports=1,
                    input_tokens=len(texts),
                    total_tokens=len(texts),
                    cost=0.01,
                ),
            )

    sessions = ChatSessionManager(tmp_path)
    for index in range(_EMBED_BATCH_SIZE + 1):
        sessions.create("coder", session_id=f"session-{index}").append(
            ChatMessage.user(f"banana fruit {index}", timestamp=timestamp(1))
        )
    logger = _CapturingLogger()
    recall = backend(tmp_path, sessions, embeddings=_UsageEmbeddings(), logger=logger)

    await recall.search_page(search_request("fruit"))

    assert len(logger.info_calls) == 1
    message, args = logger.info_calls[0]
    assert message.startswith("Embedding usage operation=")
    assert args == (
        "typed_search",
        "openrouter",
        "stub-embed",
        3,
        3,
        _EMBED_BATCH_SIZE + 2,
        _EMBED_BATCH_SIZE + 2,
        3,
        pytest.approx(0.03),
        1,
        _EMBED_BATCH_SIZE + 1,
    )


async def test_vector_backend_ranks_semantically_nearest_sessions(tmp_path: Path) -> None:
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

    data = await backend(tmp_path, sessions, embeddings=embeddings).search_page(
        request(query="car", limit=2)
    )

    assert [match.session_id for match in data.hits] == ["cars", "vehicles"]
    # ``distance`` is set by the vector backend and absent from the canonical fallback.
    assert data.hits[0].score == pytest.approx(0.0, abs=1e-5)


async def test_vector_backend_backfills_missing_sessions_lazily(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("Bananas and other fruit are tasty", timestamp=timestamp(2))
    )
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = await recall.search_page(request(query="carrot", limit=2))

    # First search backfills and embeds both sessions; we expect both to be embedded.
    assert len(embeddings.embed_calls) == 2  # one batch of sessions + the query
    assert "carrots" in [match.session_id for match in first.hits]


async def test_vector_backend_reuses_indexed_vectors_on_second_search(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("Bananas and other fruit are tasty", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(2))
    )
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(request(query="fruit", limit=2))
    await recall.search_page(request(query="carrot", limit=2))

    # Two searches: 1 session backfill + 1 query on the first call, 1 query only
    # on the second call (no backfill needed because nothing changed).
    assert len(embeddings.embed_calls) == 3


async def test_vector_backend_reindexes_when_canonical_changes(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="dynamic")
    session.append(ChatMessage.user("hello there", timestamp=timestamp(1)))
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = await recall.search_page(request(query="fruit", limit=2))
    assert "fruit" not in first.hits[0].text

    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(2)))
    second = await recall.search_page(request(query="fruit", limit=2))

    # The session should have been reindexed — the new content embeds to the
    # fruit vector and the search should surface it for "fruit".
    assert "fruit" in second.hits[0].text
    assert second.snapshot_id != first.snapshot_id
    assert len(embeddings.embed_calls) == 4


async def test_vector_backend_drops_indexed_session_when_canonical_file_removed(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("Bananas and other fruit are tasty", timestamp=timestamp(2))
    )
    embeddings = _StubEmbeddings()

    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(request(query="carrot", limit=2))

    sessions.delete(SessionAddress(project_id=None, agent_id="coder", session_id="carrots"))
    data = await recall.search_page(request(query="carrot", limit=2))

    assert "carrots" not in [match.session_id for match in data.hits]


async def test_vector_backend_reports_unavailable_when_no_embedding_binding(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    with pytest.raises(RecallSearchError, match="Semantic search"):
        await backend(tmp_path, sessions, embeddings=None).search_page(request(query="carrot"))


async def test_vector_backend_reports_unavailable_when_binding_raises(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    with pytest.raises(RecallSearchError, match="Semantic search"):
        await backend(tmp_path, sessions, embeddings=_NullEmbeddings()).search_page(
            request(query="carrot")
        )


async def test_vector_backend_respects_limit(tmp_path: Path) -> None:
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

    data = await backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search_page(
        request(query="car", limit=2)
    )
    assert len(data.hits) == 2
    assert data.has_more is True


async def test_vector_backend_rebuilds_index_when_embedding_model_changes(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    embeddings_a = _StubEmbeddings()
    embeddings_a.model_id = "model-a"

    recall = backend(tmp_path, sessions, embeddings=embeddings_a)
    await recall.search_page(request(query="carrot", limit=2))
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
    await new_recall.search_page(request(query="carrot", limit=2))

    header = new_recall.store.read_header()
    assert header is not None
    assert header.model_id == "model-b"


async def test_vector_backend_reports_unavailable_when_embed_call_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    class _FlakyEmbeddings(_StubEmbeddings):
        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            del texts, purpose
            raise EmbeddingError("provider unavailable")

    with pytest.raises(RecallSearchError, match="Semantic search"):
        await backend(tmp_path, sessions, embeddings=_FlakyEmbeddings()).search_page(
            request(query="carrot")
        )


async def test_run_embed_recursively_splits_overflowing_batch_without_changing_text(
    tmp_path: Path,
) -> None:
    """Aggregate overflow is recovered by dividing inputs, never their text."""

    sessions = ChatSessionManager(tmp_path)
    embeddings = _OverflowThenOkEmbeddings(max_chars=100)
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    texts = ["a" * 60, "b" * 60, "c" * 60, "d" * 60]

    result = await recall._run_embed(texts)

    assert result.dimension == 4
    assert len(result.vectors) == len(texts)
    assert embeddings.embed_calls[0] == texts
    successful_calls = [call for call in embeddings.embed_calls if sum(map(len, call)) <= 100]
    assert [text for call in successful_calls for text in call] == texts


async def test_run_embed_does_not_retry_non_overflow_errors(tmp_path: Path) -> None:
    """Auth/network errors are not context-length overflows and must not be
    retried by the shrink loop — they re-raise on the first attempt.
    """

    sessions = ChatSessionManager(tmp_path)
    embeddings = _AuthErrorEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    with pytest.raises(EmbeddingError, match="Unauthorized"):
        await recall._run_embed(["x" * 1000])
    assert embeddings.embed_calls == 1


async def test_run_embed_never_truncates_a_single_overlong_text(tmp_path: Path) -> None:
    """A single overflow re-raises after one honest, unchanged provider call."""

    sessions = ChatSessionManager(tmp_path)
    embeddings = _OverflowThenOkEmbeddings(max_chars=100)
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    text = "x" * 1000

    with pytest.raises(EmbeddingError):
        await recall._run_embed([text])
    assert embeddings.embed_calls == [[text]]


@pytest.mark.timeout(10)
async def test_vector_backend_search_completes_when_called_from_running_event_loop(
    tmp_path: Path,
) -> None:
    """The async backend runs directly on the server event loop without deadlocking."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car broke down", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vehicles").append(
        ChatMessage.user("I was driving my vehicle", timestamp=timestamp(2))
    )

    result = await backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search_page(
        request(query="car", limit=2)
    )
    session_ids = [match.session_id for match in result.hits]
    assert session_ids == ["cars", "vehicles"]


async def test_vector_search_cancellation_reaches_embedding_call(tmp_path: Path) -> None:
    """Cancelling a Run stops its in-flight semantic provider request."""

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _SlowEmbeddings(_StubEmbeddings):
        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            del texts, purpose
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="slow").append(
        ChatMessage.user("semantic content", timestamp=timestamp(1))
    )
    task = asyncio.create_task(
        backend(tmp_path, sessions, embeddings=_SlowEmbeddings()).search_page(
            request(query="semantic content")
        )
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# Chunking policy — build_session_passages
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Staleness — sessions that stop producing chunks must drop their old rows
# ---------------------------------------------------------------------------


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


def _project_request(
    *,
    query: str,
    project_id: str | None,
    limit: int = 5,
) -> RecallSearchRequest:
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


async def test_vector_backend_project_recall_finds_only_project_sessions(tmp_path: Path) -> None:
    """A project-scoped recall searches the project's Sessions, not the global ones."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="global-fruit").append(
        ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="proj-fruit", project_id="alpha").append(
        ChatMessage.user("I love bananas and fruit too", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    project = await recall.search_page(_project_request(query="fruit", project_id="alpha"))
    identity = await recall.search_page(_project_request(query="fruit", project_id=None))

    assert [m.session_id for m in project.hits] == ["proj-fruit"]
    assert [m.session_id for m in identity.hits] == ["global-fruit"]


async def test_vector_backend_same_uuid_global_and_project_do_not_collide(tmp_path: Path) -> None:
    """The same session UUID under global and project scope index separately.

    Each scope must surface its own session content — the project session's
    chunk must not overwrite the global one in the shared vector index.
    """

    sessions = ChatSessionManager(tmp_path)
    shared_id = "11111111-1111-1111-1111-111111111111"
    sessions.create("coder", session_id=shared_id).append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id=shared_id, project_id="alpha").append(
        ChatMessage.user("I love bananas and fruit", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    # Global scope: "carrot" hits; "fruit" (only in the project session) does not.
    global_carrot = await recall.search_page(_project_request(query="carrot", project_id=None))
    assert [m.session_id for m in global_carrot.hits] == [shared_id]

    # Project scope: "fruit" hits the project session of the same UUID.
    project_fruit = await recall.search_page(_project_request(query="fruit", project_id="alpha"))
    assert [m.session_id for m in project_fruit.hits] == [shared_id]
    # The project chunk did not overwrite the global one — both vec0 rows exist.
    assert _count_vec_rows(recall.store.path, "coder", shared_id) == 2


async def test_vector_backend_identity_recall_unchanged_by_project_field(tmp_path: Path) -> None:
    """An identity recall (``project_id=None``) behaves exactly as the legacy default.

    Passing ``project_id=None`` explicitly must match the implicit-default
    behavior: same sessions, same matches, distances present.
    """

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car broke down", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vehicles").append(
        ChatMessage.user("I was driving my vehicle", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    explicit_none = await recall.search_page(
        _project_request(query="car", project_id=None, limit=2)
    )
    default = await recall.search_page(request(query="car", limit=2))

    assert [m.session_id for m in explicit_none.hits] == ["cars", "vehicles"]
    assert [m.session_id for m in default.hits] == ["cars", "vehicles"]


async def test_vector_backend_does_not_match_its_own_search_output(tmp_path: Path) -> None:
    """A session_search result is excluded from the index — no self-matching loop.

    The session's only "fruit" text lives in a persisted session_search result;
    searching "fruit" must not surface the session via that artifact. Searching
    "carrot" (the real user message) still surfaces it, anchored on the user
    message — proving the artifact text was dropped, not the whole session.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="selfref")
    session.append(
        ChatMessage.tool(
            tool_call_id="c1",
            name="session_search",
            content="I love bananas and fruit",
            timestamp=timestamp(1),
        )
    )
    session.append(ChatMessage.user("I bought some carrots", timestamp=timestamp(2)))

    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    fruit = await recall.search_page(request(query="fruit", limit=5))
    carrot = await recall.search_page(request(query="carrot", limit=5))

    assert all("fruit" not in hit.text for hit in fruit.hits)
    carrot_matches = [match for match in carrot.hits if match.session_id == "selfref"]
    assert len(carrot_matches) == 1
    assert carrot_matches[0].role == "user"


# ---------------------------------------------------------------------------
# _run_embed batching
# ---------------------------------------------------------------------------


async def test_run_embed_splits_texts_into_batches_above_batch_size(tmp_path: Path) -> None:
    """When ``texts > _EMBED_BATCH_SIZE`` the embedder receives multiple, ordered calls."""

    class _CountingEmbeddings(_StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            self.batch_sizes.append(len(texts))
            return await super().embed(texts, purpose=purpose)

    sessions = ChatSessionManager(tmp_path)
    embeddings = _CountingEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    # Two full batches plus a partial third batch.
    total = _EMBED_BATCH_SIZE + _EMBED_BATCH_SIZE + 3
    texts = [f"text-{index}" for index in range(total)]
    result = await recall._run_embed(texts)

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


async def test_run_embed_single_text_does_not_split(tmp_path: Path) -> None:
    """A single text fits in one batch — no splitting overhead."""

    class _CountingEmbeddings(_StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            self.batch_sizes.append(len(texts))
            return await super().embed(texts, purpose=purpose)

    sessions = ChatSessionManager(tmp_path)
    embeddings = _CountingEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    result = await recall._run_embed(["only-text"])

    assert embeddings.batch_sizes == [1]
    assert len(result.vectors) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
