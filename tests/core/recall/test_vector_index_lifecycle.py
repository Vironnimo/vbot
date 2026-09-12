"""Vector: index lifecycle behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
)
from core.recall import (
    RecallBackendContext,
    RecallSearchError,
    VectorRecallBackend,
)
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.recall.vector_helpers import (
    _StubEmbeddings,
    backend,
    request,
    timestamp,
)

pytestmark = pytest.mark.asyncio


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
