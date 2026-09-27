"""The ``vector`` backend: typed semantic search, embedding spaces and background indexing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingSpaceIdentity,
    EmbeddingUsage,
)
from core.recall import RecallSearchError
from core.recall import vector as vector_module
from core.recall.vector import SEMANTIC_PARTIAL_REASON
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.recall.recall_test_support import (
    StubEmbeddings,
    pending_count,
    request,
    timestamp,
    vector_backend,
)

pytestmark = pytest.mark.asyncio

# Background indexing is exercised with a small batch so a few Sessions exceed it.
BATCH = 2


@pytest.fixture
def small_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vector_module, "_EMBED_BATCH_SIZE", BATCH)


class _CapturingLogger:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, tuple[object, ...]]] = []
        self.warning_calls: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.info_calls.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warning_calls.append((message, args))


def _fruit_sessions(sessions: ChatSessionManager, count: int) -> None:
    for index in range(count):
        sessions.create("coder", session_id=f"session-{index}").append(
            ChatMessage.user(f"banana fruit {index}", timestamp=timestamp(1))
        )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


async def test_search_returns_ranked_passages_without_session_dedup(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="fruit-heavy").append(
        ChatMessage.user("fruit banana " * 400, timestamp=timestamp(1))
    )

    page = await vector_backend(tmp_path, sessions, embeddings=StubEmbeddings()).search_page(
        request("fruit")
    )

    assert page.result_type == "passage"
    assert page.ranking == "cosine_distance"
    assert len(page.hits) > 1
    assert {hit.session_id for hit in page.hits} == {"fruit-heavy"}
    assert len({hit.passage_id for hit in page.hits}) == len(page.hits)
    assert all(hit.sources == ("semantic",) for hit in page.hits)


async def test_search_ranks_by_cosine_distance_without_a_cutoff_or_literal_fallback(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    for session_id, text, day in (
        ("cars", "My car broke down", 1),
        ("vehicles", "I was driving my vehicle", 2),
        ("fruit", "I love bananas and other fruit", 3),
    ):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(text, timestamp=timestamp(day))
        )
    recall = vector_backend(tmp_path, sessions, embeddings=StubEmbeddings())

    top = await recall.search_page(request("car", limit=2))
    everything = await recall.search_page(request("car"))

    assert [hit.session_id for hit in top.hits] == ["cars", "vehicles"]
    assert top.has_more is True
    assert top.hits[0].score == pytest.approx(0.0, abs=1e-5)
    # A far Passage without the query term still ranks: no cutoff, no keyword check.
    assert [hit.session_id for hit in everything.hits] == ["cars", "vehicles", "fruit"]
    assert everything.hits[-1].score > 0.7


async def test_search_prefilters_time_inside_knn(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="old").append(
        ChatMessage.user("banana fruit old", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="new").append(
        ChatMessage.user("banana fruit new", timestamp=timestamp(3))
    )
    recall = vector_backend(tmp_path, sessions, embeddings=StubEmbeddings())

    page = await recall.search_page(request("fruit", since=datetime(2026, 5, 2, tzinfo=UTC)))

    assert [hit.session_id for hit in page.hits] == ["new"]


async def test_search_skips_a_session_deleted_during_reconciliation(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = sessions.create("coder", session_id="deleted-during-index")
    session.append(ChatMessage.user("banana fruit", timestamp=timestamp(1)))
    address = SessionAddress(project_id=None, agent_id="coder", session_id=session.id)
    list_history_revisions = sessions.list_history_revisions
    deleted = False

    def list_then_delete(agent_id: str, project_id: str | None = None) -> Any:
        nonlocal deleted
        versions = list_history_revisions(agent_id, project_id)
        if not deleted:
            sessions.delete(address)
            deleted = True
        return versions

    monkeypatch.setattr(sessions, "list_history_revisions", list_then_delete)
    recall = vector_backend(tmp_path, sessions, embeddings=StubEmbeddings())

    page = await recall.search_page(request("fruit"))

    assert page.hits == ()
    assert await recall.store.list_indexed_sessions("coder") == {}


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class _UnconfiguredEmbeddings(StubEmbeddings):
    def resolve_space(self) -> EmbeddingSpaceIdentity:
        raise EmbeddingError("no text_embedding binding configured")


class _FailingEmbeddings(StubEmbeddings):
    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        raise EmbeddingError("provider unavailable")


@pytest.mark.parametrize(
    "embeddings",
    [None, _UnconfiguredEmbeddings(), _FailingEmbeddings()],
    ids=["no-binding", "binding-raises", "embed-fails"],
)
async def test_search_without_working_embeddings_is_semantic_unavailable(
    tmp_path: Path, sessions: ChatSessionManager, embeddings: Any
) -> None:
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    with pytest.raises(RecallSearchError, match="Semantic search") as error:
        await vector_backend(tmp_path, sessions, embeddings=embeddings).search_page(
            request("carrot")
        )

    assert error.value.code == "semantic_unavailable"


async def test_search_cancellation_reaches_the_embedding_call(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    """Cancelling a Run stops its in-flight semantic provider request."""

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _SlowEmbeddings(StubEmbeddings):
        async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

    sessions.create("coder", session_id="slow").append(
        ChatMessage.user("semantic content", timestamp=timestamp(1))
    )
    task = asyncio.create_task(
        vector_backend(tmp_path, sessions, embeddings=_SlowEmbeddings()).search_page(
            request("semantic content")
        )
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# Embedding spaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("model_id", "model-b"), ("space_fingerprint", "stub-space-b"), ("dimension", 6)],
)
async def test_embedding_space_change_rebuilds_the_whole_scope(
    tmp_path: Path, sessions: ChatSessionManager, field: str, value: Any
) -> None:
    for session_id, day in (("one", 1), ("two", 2)):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(f"banana fruit {session_id}", timestamp=timestamp(day))
        )
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(request("fruit"))
    documents_before = len(embeddings.document_inputs)

    setattr(embeddings, field, value)
    page = await recall.search_page(request("fruit"))

    assert {hit.session_id for hit in page.hits} == {"one", "two"}
    header = await recall.store.read_header()
    assert header is not None and getattr(header, field) == value
    assert sorted(embeddings.document_inputs[documents_before:]) == [
        "banana fruit one",
        "banana fruit two",
    ]
    assert set(await recall.store.list_indexed_sessions("coder")) == {"one", "two"}


async def test_served_model_change_invalidates_continuations_and_rebuilds(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    logger = _CapturingLogger()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings, logger=logger)
    first_page = await recall.search_page(request("fruit"))
    continuation = request("fruit", offset=1, snapshot_id=first_page.snapshot_id)
    assert (await recall.search_page(continuation)).snapshot_id == first_page.snapshot_id

    embeddings.response_model_id = "served/embed-b"
    with pytest.raises(RecallSearchError) as error:
        await recall.search_page(continuation)
    page = await recall.search_page(request("fruit"))

    assert error.value.code == "stale_cursor"
    assert [hit.session_id for hit in page.hits] == ["one"]
    header = await recall.store.read_header()
    assert header is not None
    assert (header.model_id, header.response_model_id) == ("stub-embed", "served/embed-b")
    assert any("response model changed" in message for message, _args in logger.warning_calls)


async def test_served_model_drift_while_passages_wait_rebuilds(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="one")
    session.append(ChatMessage.user("banana fruit", timestamp=timestamp(1)))
    embeddings = StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(request("fruit"))

    session.append(ChatMessage.user("another banana", timestamp=timestamp(2)))
    embeddings.response_model_id = "served/embed-b"
    page = await recall.search_page(request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    header = await recall.store.read_header()
    assert header is not None
    assert header.response_model_id == "served/embed-b"


# ---------------------------------------------------------------------------
# Bounded search embedding and background indexing
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("small_batches")
async def test_search_embeds_one_bounded_batch_and_backfills_the_rest_in_background(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    _fruit_sessions(sessions, BATCH + 1)
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    gate = asyncio.Event()
    embed = embeddings.embed

    async def held_backfill(texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        if purpose == "document" and len(embeddings.document_inputs) >= BATCH:
            await gate.wait()
        return await embed(texts, purpose=purpose)

    embeddings.embed = held_backfill  # type: ignore[method-assign]

    partial = await recall.search_page(request("fruit"))

    # The search embedded one batch and answered from it while the rest waits.
    assert len(embeddings.document_inputs) == BATCH
    assert len(partial.hits) == BATCH
    assert partial.degraded is True
    assert partial.degradation_reason == SEMANTIC_PARTIAL_REASON
    assert pending_count(recall.store.path) == 1
    task = recall._backfill_task
    assert task is not None and not task.done()

    # A search while the background task embeds never embeds the same text again.
    during = await recall.search_page(request("fruit"))
    assert during.degraded is True
    assert len(embeddings.document_inputs) == BATCH

    gate.set()
    await asyncio.wait_for(task, timeout=10)
    complete = await recall.search_page(request("fruit"))

    assert pending_count(recall.store.path) == 0
    assert len(embeddings.document_inputs) == BATCH + 1
    assert len(complete.hits) == BATCH + 1
    assert complete.degraded is False
    assert complete.degradation_reason is None


@pytest.mark.usefixtures("small_batches")
async def test_embedding_space_change_requeues_every_passage_without_blocking(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    _fruit_sessions(sessions, BATCH + 1)
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(request("fruit"))
    assert recall._backfill_task is not None
    await asyncio.wait_for(recall._backfill_task, timeout=10)
    assert pending_count(recall.store.path) == 0

    embeddings.space_fingerprint = "stub-space-b"
    calls_before = len(embeddings.embed_calls)
    page = await recall.search_page(request("fruit"))

    header = await recall.store.read_header()
    assert header is not None and header.space_fingerprint == "stub-space-b"
    assert page.degraded is True
    assert recall._backfill_task is not None
    await asyncio.wait_for(recall._backfill_task, timeout=10)
    # The search embedded one bounded batch; the background task the remainder.
    document_batches = [
        len(texts)
        for texts, purpose in zip(
            embeddings.embed_calls[calls_before:],
            embeddings.embed_purposes[calls_before:],
            strict=True,
        )
        if purpose == "document"
    ]
    assert document_batches == [BATCH, 1]
    assert pending_count(recall.store.path) == 0


async def test_failed_batch_embedding_answers_from_indexed_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(request("fruit"))
    await asyncio.to_thread(
        sessions.create("coder", session_id="two").append,
        ChatMessage.user("more fruit", timestamp=timestamp(2)),
    )
    embed = embeddings.embed

    async def reject_documents(texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        if purpose == "document":
            raise EmbeddingError("provider rejected the batch")
        return await embed(texts, purpose=purpose)

    embeddings.embed = reject_documents  # type: ignore[method-assign]

    page = await recall.search_page(request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert page.degradation_reason == SEMANTIC_PARTIAL_REASON
    assert recall._backfill_task is not None
    await asyncio.wait_for(recall._backfill_task, timeout=10)
    assert pending_count(recall.store.path) == 1


class _UsageEmbeddings(StubEmbeddings):
    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
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


@pytest.mark.usefixtures("small_batches")
async def test_typed_search_and_backfill_log_their_own_usage(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    _fruit_sessions(sessions, BATCH + 1)
    logger = _CapturingLogger()
    recall = vector_backend(tmp_path, sessions, embeddings=_UsageEmbeddings(), logger=logger)

    await recall.search_page(request("fruit"))
    assert recall._backfill_task is not None
    await asyncio.wait_for(recall._backfill_task, timeout=10)

    assert [
        message.startswith("Embedding usage operation=") for message, _ in logger.info_calls
    ] == [True, True]
    # operation, provider, model, requests, token reports, input tokens, total tokens,
    # cost reports, cost, query inputs, document inputs
    assert logger.info_calls[0][1] == (
        "typed_search",
        "openrouter",
        "stub-embed",
        2,
        2,
        BATCH + 1,
        BATCH + 1,
        2,
        pytest.approx(0.02),
        1,
        BATCH,
    )
    assert logger.info_calls[1][1] == (
        "recall_backfill",
        "openrouter",
        "stub-embed",
        1,
        1,
        1,
        1,
        1,
        pytest.approx(0.01),
        0,
        1,
    )
