"""Vector: index lifecycle behavior."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingSpaceIdentity,
)
from core.recall import (
    RecallBackendContext,
    RecallSearchError,
    VectorRecallBackend,
)
from core.recall.passages import build_session_passages
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.recall.vector_helpers import (
    _passage_rows,
    _StubEmbeddings,
    backend,
    forbid_database_calls_on_loop,
    forbid_event_loop_calls,
    request,
    timestamp,
)

pytestmark = pytest.mark.asyncio


class _NullEmbeddings:
    """Stand-in embedding service that always raises configuration errors."""

    def resolve_space(self) -> EmbeddingSpaceIdentity:
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
    assert set(await recall.store.list_indexed_sessions("coder")) == {"fruit"}


async def test_vector_backend_filtered_search_prunes_deleted_sessions_of_whole_scope(
    tmp_path: Path,
) -> None:
    """Pruning follows the complete live scope, not the Session filter of the request."""

    sessions = ChatSessionManager(tmp_path)
    for session_id, text in (("carrots", "I bought some carrots"), ("fruit", "Fruit is tasty")):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(text, timestamp=timestamp(1))
        )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    await recall.search_page(request(query="carrot"))

    sessions.delete(SessionAddress(project_id=None, agent_id="coder", session_id="carrots"))
    await recall.search_page(dataclasses.replace(request(query="fruit"), session_id="fruit"))

    assert set(await recall.store.list_indexed_sessions("coder")) == {"fruit"}
    assert _passage_rows(recall.store.path, "coder", "carrots") == {}


async def test_vector_backend_history_edit_replaces_changed_and_vanished_passages(
    tmp_path: Path,
) -> None:
    """An edited history drops Passages of the removed turns and indexes the replacement."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="edited")
    session.append(ChatMessage.user("I love bananas and fruit " * 80, timestamp=timestamp(1)))
    target = ChatMessage.user("My car broke down " * 80, timestamp=timestamp(2))
    session.append(target)
    session.append(ChatMessage.user("car repair advice " * 150, timestamp=timestamp(3)))
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(request(query="car"))
    old_passages = build_session_passages(session.load_active())

    session.apply_edit(
        target.id, [ChatMessage.user("I bought some carrots", timestamp=timestamp(4))]
    )
    documents_before = len(embeddings.document_inputs)
    page = await recall.search_page(request(query="car", limit=10))

    new_passages = build_session_passages(session.load_active())
    rows = _passage_rows(recall.store.path, "coder", "edited")
    assert sorted(rows.values()) == sorted(passage.text for passage in new_passages)
    assert not any("broke down" in text or "repair" in text for text in rows.values())
    assert all("broke down" not in hit.text and "repair" not in hit.text for hit in page.hits)
    embedded = embeddings.document_inputs[documents_before:]
    assert sorted(embedded) == sorted(
        {passage.text for passage in new_passages if passage not in old_passages}
        - {passage.text for passage in old_passages}
    )
    assert len(embedded) < len(new_passages)


async def test_vector_backend_fork_shares_stored_passages_and_reports_them_once(
    tmp_path: Path,
) -> None:
    """A fork shows its origin's stored Passages: nothing is embedded or returned twice."""

    sessions = ChatSessionManager(tmp_path)
    source = sessions.create("coder", session_id="source")
    for day in range(1, 4):
        source.append(ChatMessage.user(f"fruit story {day} " * 120, timestamp=timestamp(day)))
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    first = await recall.search_page(request(query="fruit", limit=20))
    documents_before = len(embeddings.document_inputs)

    fork = await sessions.fork(source.address)
    page = await recall.search_page(request(query="fruit", limit=20))

    assert embeddings.document_inputs[documents_before:] == []
    assert _passage_rows(recall.store.path, "coder", fork.id) == _passage_rows(
        recall.store.path, "coder", "source"
    )
    # The origin owns the shared history and is eligible, so it keeps every hit.
    assert [(hit.session_id, hit.passage_id) for hit in page.hits] == [
        (hit.session_id, hit.passage_id) for hit in first.hits
    ]
    assert {hit.session_id for hit in page.hits} == {"source"}


async def test_vector_backend_attributes_shared_history_to_the_newest_eligible_fork(
    tmp_path: Path,
) -> None:
    """Shared history an ineligible or deleted origin cannot report goes to one fork."""

    sessions = ChatSessionManager(tmp_path)
    source = sessions.create("coder", session_id="source")
    for day in range(1, 4):
        source.append(ChatMessage.user(f"fruit story {day} " * 120, timestamp=timestamp(day)))
    older = await sessions.fork(source.address)
    newer = await sessions.fork(source.address)
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    complete = await recall.search_page(request(query="fruit", limit=20))

    excluding = dataclasses.replace(
        request(query="fruit", limit=20), excluded_session_ids=("source",)
    )
    page = await recall.search_page(excluding)

    assert {hit.session_id for hit in complete.hits} == {"source"}
    assert {hit.session_id for hit in page.hits} == {newer.id}
    assert [hit.passage_id for hit in page.hits] == [hit.passage_id for hit in complete.hits]

    # Deleting the origin copies its history into the forks and bumps their
    # history revision; the forks are reindexed and still report it once.
    await asyncio.to_thread(sessions.delete, source.address)
    after_delete = await recall.search_page(request(query="fruit", limit=20))

    revisions = await asyncio.to_thread(sessions.list_history_revisions, "coder")
    assert await recall.store.list_indexed_sessions("coder") == {
        revision.address.session_id: (revision.generation_id, revision.history_revision)
        for revision in revisions
    }
    assert {hit.session_id for hit in after_delete.hits} == {newer.id}
    assert [hit.passage_id for hit in after_delete.hits] == [
        hit.passage_id for hit in complete.hits
    ]
    assert older.id not in {hit.session_id for hit in (*page.hits, *after_delete.hits)}


async def test_vector_backend_reconciles_excluded_session_once_it_is_included(
    tmp_path: Path,
) -> None:
    """An excluded Session is not embedded while excluded and catches up when included."""

    sessions = ChatSessionManager(tmp_path)
    current = sessions.create("coder", session_id="current")
    current.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))
    sessions.create("coder", session_id="other").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(2))
    )
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    excluding = dataclasses.replace(request(query="fruit"), excluded_session_ids=("current",))

    first = await recall.search_page(excluding)
    current.append(ChatMessage.user("more fruit talk", timestamp=timestamp(3)))
    documents_before = len(embeddings.document_inputs)
    second = await recall.search_page(excluding)

    assert set(await recall.store.list_indexed_sessions("coder")) == {"other"}
    assert embeddings.document_inputs[documents_before:] == []
    assert "current" not in {hit.session_id for hit in (*first.hits, *second.hits)}

    included = await recall.search_page(request(query="fruit"))

    assert set(await recall.store.list_indexed_sessions("coder")) == {"current", "other"}
    assert included.hits[0].session_id == "current"
    assert "more fruit talk" in included.hits[0].text


async def test_vector_backend_does_not_reread_session_without_passages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Session that yields no Passages is stamped and not reloaded while unchanged."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="inert").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    builds: list[int] = []

    def no_passages(messages: list[ChatMessage]) -> list[object]:
        builds.append(len(messages))
        return []

    monkeypatch.setattr("core.recall._passage_catalog.build_session_passages", no_passages)
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    await recall.search_page(request(query="carrot"))
    await recall.search_page(request(query="carrot"))

    assert builds == [1]
    assert set(await recall.store.list_indexed_sessions("coder")) == {"inert"}


async def test_vector_search_keeps_session_and_store_reads_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="fruit")
    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    calls = forbid_event_loop_calls(monkeypatch, sessions._store)
    database_calls = forbid_database_calls_on_loop(monkeypatch)

    await recall.search_page(request(query="fruit"))
    await sessions.run_async(session.append, ChatMessage.user("more fruit", timestamp=timestamp(2)))
    page = await recall.search_page(request(query="fruit"))

    assert page.hits
    assert "list_history_revisions" in calls
    assert {"recall_vectors.read", "recall_vectors.write"} <= set(database_calls)


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
    header_a = await recall.store.read_header()
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

    header = await new_recall.store.read_header()
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
