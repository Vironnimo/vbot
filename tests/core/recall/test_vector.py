"""The ``vector`` backend: typed semantic search over the Passage index's vectors."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, override

import pytest

from core.chat import ChatMessage
from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingSpaceIdentity,
    EmbeddingUsage,
)
from core.recall import RecallSearchError
from core.recall import passage_index as passage_index_module
from core.recall.vector import SEMANTIC_PARTIAL_REASON
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.recall.recall_test_support import (
    StubEmbeddings,
    VectorBackendFactory,
    embed_documents,
    pending_count,
    request,
    timestamp,
)

pytestmark = pytest.mark.asyncio


class _CapturingLogger:
    def __init__(self) -> None:
        self.calls: dict[str, list[tuple[str, tuple[object, ...]]]] = {
            "debug": [],
            "info": [],
            "warning": [],
        }

    def debug(self, message: str, *args: object) -> None:
        self.calls["debug"].append((message, args))

    def info(self, message: str, *args: object) -> None:
        self.calls["info"].append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.calls["warning"].append((message, args))


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


async def test_search_returns_ranked_passages_without_session_dedup(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    sessions.create("coder", session_id="fruit-heavy").append(
        ChatMessage.user("fruit banana " * 400, timestamp=timestamp(1))
    )
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)

    page = await recall.search_page(request("fruit"))

    assert page.result_type == "passage"
    assert page.ranking == "cosine_distance"
    assert len(page.hits) > 1
    assert {hit.session_id for hit in page.hits} == {"fruit-heavy"}
    assert len({hit.passage_id for hit in page.hits}) == len(page.hits)
    assert all(hit.sources == ("semantic",) for hit in page.hits)


async def test_search_ranks_by_cosine_distance_without_a_cutoff_or_literal_fallback(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    for session_id, text, day in (
        ("cars", "My car broke down", 1),
        ("vehicles", "I was driving my vehicle", 2),
        ("fruit", "I love bananas and other fruit", 3),
    ):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(text, timestamp=timestamp(day))
        )
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)

    top = await recall.search_page(request("car", limit=2))
    everything = await recall.search_page(request("car"))

    assert [hit.session_id for hit in top.hits] == ["cars", "vehicles"]
    assert top.has_more is True
    assert top.hits[0].score == pytest.approx(0.0, abs=1e-5)
    # A far Passage without the query term still ranks: no cutoff, no keyword check.
    assert [hit.session_id for hit in everything.hits] == ["cars", "vehicles", "fruit"]
    assert everything.hits[-1].score > 0.7


async def test_search_prefilters_time_inside_knn(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    sessions.create("coder", session_id="old").append(
        ChatMessage.user("banana fruit old", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="new").append(
        ChatMessage.user("banana fruit new", timestamp=timestamp(3))
    )
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)

    page = await recall.search_page(request("fruit", since=datetime(2026, 5, 2, tzinfo=UTC)))

    assert [hit.session_id for hit in page.hits] == ["new"]


async def test_search_beyond_the_knn_limit_pages_without_failing(
    sessions: ChatSessionManager,
    vector_backend: VectorBackendFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page deeper than the index can rank reports more instead of failing."""

    for index in range(4):
        sessions.create("coder", session_id=f"fruit-{index}").append(
            ChatMessage.user(f"banana fruit {index}", timestamp=timestamp(index + 1))
        )
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    monkeypatch.setattr(passage_index_module, "KNN_MAX_K", 2)

    within = await recall.search_page(request("fruit", limit=2))
    capped = await recall.search_page(request("fruit", limit=3))
    beyond = await recall.search_page(request("fruit", limit=2, offset=2))

    assert (len(within.hits), within.has_more) == (2, True)
    assert (len(capped.hits), capped.has_more) == (2, True)
    assert (beyond.hits, beyond.has_more) == ((), False)
    assert pending_count(recall.index.path) == 0


async def test_search_skips_a_session_deleted_during_reconciliation(
    sessions: ChatSessionManager,
    vector_backend: VectorBackendFactory,
    monkeypatch: pytest.MonkeyPatch,
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
    recall = vector_backend(embeddings=StubEmbeddings())

    page = await recall.search_page(request("fruit"))

    assert page.hits == ()
    assert await recall.index.list_indexed_sessions("coder") == {}


# ---------------------------------------------------------------------------
# Coverage: a search embeds only its query
# ---------------------------------------------------------------------------


async def test_search_embeds_only_its_query_and_reports_waiting_passages(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = StubEmbeddings()
    nudges: list[None] = []
    recall = vector_backend(embeddings=embeddings, on_waiting=lambda: nudges.append(None))

    waiting = await recall.search_page(request("fruit"))

    # The search refreshed the catalog, embedded its query and nudged the indexer.
    assert embeddings.embed_calls == [["fruit"]]
    assert embeddings.embed_purposes == ["query"]
    assert waiting.hits == ()
    assert waiting.degraded is True
    assert waiting.degradation_reason == SEMANTIC_PARTIAL_REASON
    assert pending_count(recall.index.path) == 1
    assert len(nudges) == 1

    await embed_documents(recall.index, sessions, embeddings)
    complete = await recall.search_page(request("fruit"))

    assert [hit.session_id for hit in complete.hits] == ["one"]
    assert complete.degraded is False
    assert complete.degradation_reason is None
    assert len(nudges) == 1


async def test_search_answers_from_indexed_passages_while_new_ones_wait(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    await asyncio.to_thread(
        sessions.create("coder", session_id="two").append,
        ChatMessage.user("more fruit", timestamp=timestamp(2)),
    )

    page = await recall.search_page(request("fruit"))
    earlier = await recall.search_page(request("fruit", until=timestamp(1)))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert page.degradation_reason == SEMANTIC_PARTIAL_REASON
    # Coverage follows the request's period: nothing in it still waits.
    assert [hit.session_id for hit in earlier.hits] == ["one"]
    assert earlier.degraded is False


class _UsageEmbeddings(StubEmbeddings):
    @override
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


async def test_search_logs_the_usage_of_its_query_embedding(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    logger = _CapturingLogger()
    recall = vector_backend(embeddings=_UsageEmbeddings(), logger=logger)

    await recall.search_page(request("fruit"))

    # operation, provider, model, requests, token reports, input tokens, total tokens,
    # cost reports, cost
    assert [args for _message, args in logger.calls["debug"]] == [
        ("openrouter", "stub-embed", 1, 1, 1, 1, 1, pytest.approx(0.01))
    ]


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class _UnconfiguredEmbeddings(StubEmbeddings):
    @override
    def resolve_space(self) -> EmbeddingSpaceIdentity:
        raise EmbeddingError("no text_embedding binding configured")


class _FailingEmbeddings(StubEmbeddings):
    @override
    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        raise EmbeddingError("provider unavailable")


@pytest.mark.parametrize(
    "embeddings",
    [None, _UnconfiguredEmbeddings(), _FailingEmbeddings()],
    ids=["no-binding", "binding-raises", "embed-fails"],
)
async def test_search_without_working_embeddings_is_semantic_unavailable(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory, embeddings: Any
) -> None:
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )

    with pytest.raises(RecallSearchError, match="Semantic search") as error:
        await vector_backend(embeddings=embeddings).search_page(request("carrot"))

    assert error.value.code == "semantic_unavailable"


async def test_search_cancellation_reaches_the_embedding_call(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    """Cancelling a Run stops its in-flight semantic provider request."""

    cancelled = asyncio.Event()

    class _SlowEmbeddings(StubEmbeddings):
        @override
        async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
            # Cancel the search only once its Provider request is in flight.
            task.cancel()
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
        vector_backend(embeddings=_SlowEmbeddings()).search_page(request("semantic content"))
    )

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# Embedding spaces
# ---------------------------------------------------------------------------


async def test_served_model_name_change_keeps_the_index(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    # A router such as OpenRouter answers one configured model from several
    # hosts that report different model names; the vectors stay usable.
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit one", timestamp=timestamp(1))
    )
    embeddings = StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    first = await recall.search_page(request("fruit", limit=1))
    documents_before = len(embeddings.document_inputs)

    embeddings.response_model_id = "served/embed-b"
    page = await recall.search_page(
        request("fruit", limit=1, offset=0, snapshot_id=first.snapshot_id)
    )
    await embed_documents(recall.index, sessions, embeddings)

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert not page.degraded
    assert len(embeddings.document_inputs) == documents_before


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("model_id", "model-b", "binding"),
        ("space_fingerprint", "stub-space-b", "binding"),
        ("dimension", 6, "dimension"),
    ],
    ids=["model", "fingerprint", "dimension"],
)
async def test_embedding_space_change_invalidates_continuations_and_requeues_everything(
    sessions: ChatSessionManager,
    vector_backend: VectorBackendFactory,
    field: str,
    value: Any,
    reason: str,
) -> None:
    for session_id, day in (("one", 1), ("two", 2)):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(f"banana fruit {session_id}", timestamp=timestamp(day))
        )
    embeddings = StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    logger = _CapturingLogger()
    recall = vector_backend(embeddings=embeddings, logger=logger)
    await embed_documents(recall.index, sessions, embeddings)
    first = await recall.search_page(request("fruit", limit=1))
    continuation = request("fruit", limit=1, offset=1, snapshot_id=first.snapshot_id)
    documents_before = len(embeddings.document_inputs)

    setattr(embeddings, field, value)
    with pytest.raises(RecallSearchError) as error:
        await recall.search_page(continuation)
    waiting = await recall.search_page(request("fruit"))

    # The search pinned the new space: no vector of the old one is usable.
    assert error.value.code == "stale_cursor"
    header = await recall.index.read_header()
    assert header is not None and getattr(header, field) == value
    assert (waiting.hits, waiting.degradation_reason) == ((), SEMANTIC_PARTIAL_REASON)
    pinned = [args for message, args in logger.calls["info"] if "Pinned" in message]
    assert pinned[-1][-1] == reason

    await embed_documents(recall.index, sessions, embeddings)
    page = await recall.search_page(request("fruit"))

    assert {hit.session_id for hit in page.hits} == {"one", "two"}
    assert sorted(embeddings.document_inputs[documents_before:]) == [
        "banana fruit one",
        "banana fruit two",
    ]
