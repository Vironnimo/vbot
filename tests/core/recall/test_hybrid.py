"""Tests for the hybrid recall backend.

The hybrid backend fuses the SQLite FTS and vector arms. These tests
construct it directly (no registry) and exercise the headline
behaviors: literal-keyword coverage, conceptual-query fallback,
shared-session tagging, graceful degradation when no embedding
binding is configured, ordering rules, over-fetch, and short queries
that fall through the FTS trigram path.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.model_tasks import EmbeddingResult, EmbeddingSpaceIdentity
from core.recall import (
    RecallBackendContext,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
    hybrid,
)
from core.recall.canonical import RecallScope
from core.recall.hybrid import (
    HybridRecallBackend,
)
from core.sessions import ChatSessionManager
from tests.core.recall.vector_helpers import (
    forbid_database_calls_on_loop,
    forbid_event_loop_calls,
)

pytestmark = pytest.mark.asyncio


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


class _StubEmbeddings:
    """Deterministic embedding stub for hybrid tests.

    Vector assignment rules — the same text always maps to the same
    vector so cosine distance is a stable signal:

    * ``vehicle``/``driving`` (and other car-like text) → ``[1, 0, 0, 0]``
    * ``bild`` → ``[0, 0, 0, 1]`` — orthogonal to every other cluster,
      so any session whose embedding is not the ``bild`` vector has
      distance ``> 0.7`` to the ``bild`` query. That makes the
      ``bild`` regression test possible: a single-token keyword with
      no semantic context must come back via the literal arm, not via
      a fake semantic match.
    * default → ``[0.5, 0.5, 0, 0]`` (the same default the existing
      vector tests use).
    """

    def __init__(self, *, dimension: int = 4) -> None:
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.embed_calls: list[list[str]] = []
        self.resolve_calls = 0

    def resolve_space(self) -> EmbeddingSpaceIdentity:
        self.resolve_calls += 1
        return EmbeddingSpaceIdentity(self.provider_id, self.model_id, "")

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:
        del purpose
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
        if "vehicle" in lowered or "driving" in lowered or " car " in lowered:
            return [1.0, 0.0, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        # The ``bild`` regression: a single-token query like ``bild`` has
        # almost no semantic context, so its embedding is essentially
        # orthogonal to every session's embedding. Model that by
        # routing the keyword itself to a unique vector *only* when the
        # text is short (the query case). Longer text containing
        # ``bild`` — session messages — falls into the default branch
        # and is well-separated from the query vector.
        if "bild" in lowered and len(lowered.strip()) <= 5:
            return [0.0, 0.0, 0.0, 1.0] + [0.0] * (self.dimension - 4)
        return [0.5, 0.5, 0.0, 0.0] + [0.0] * (self.dimension - 4)


def backend(
    tmp_path: Path,
    sessions: ChatSessionManager,
    *,
    embeddings: Any | None = None,
) -> HybridRecallBackend:
    return HybridRecallBackend(
        RecallBackendContext(
            data_dir=tmp_path,
            sessions=sessions,
            embeddings=embeddings,
        )
    )


def search_request(query: str, *, limit: int = 10) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        session_id=None,
        query=query,
        since=None,
        until=None,
        roles=("user", "assistant", "error", "compaction_checkpoint"),
        match_mode="all_terms",
        order="relevance",
        offset=0,
        limit=limit,
    )


async def test_typed_hybrid_search_uses_passage_rrf_and_source_membership(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="both").append(
        ChatMessage.user("I was driving today", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="semantic").append(
        ChatMessage.user("vehicle maintenance", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("driving"))

    assert page.result_type == "passage"
    assert page.ranking == "reciprocal_rank_fusion"
    assert page.hits[0].session_id == "both"
    assert page.hits[0].sources == ("literal", "semantic")
    assert any(hit.session_id == "semantic" and hit.sources == ("semantic",) for hit in page.hits)


async def test_typed_hybrid_degrades_explicitly_to_literal_passages(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="literal").append(
        ChatMessage.user("telegraminstallation", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)

    page = await recall.search_page(search_request("telegram"))

    assert [hit.session_id for hit in page.hits] == ["literal"]
    assert page.hits[0].sources == ("literal",)
    assert page.degraded is True
    assert page.degradation_reason is not None


async def test_typed_hybrid_keeps_multiple_passages_from_one_session(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="repeated").append(
        ChatMessage.user("needle context " * 400, timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("needle"))

    repeated = [hit for hit in page.hits if hit.session_id == "repeated"]
    assert len(repeated) > 1
    assert len({hit.passage_id for hit in repeated}) == len(repeated)


class _WaitingArm:
    """Prepared arm whose ranking waits until the other arm ranks too."""

    def __init__(self, ranking: str, started: asyncio.Event, other: asyncio.Event) -> None:
        self._ranking = ranking
        self._started = started
        self._other = other

    async def page(self, offset: int, limit: int) -> RecallSearchPage:
        self._started.set()
        await self._other.wait()
        return RecallSearchPage((), "passage", self._ranking, f"{self._ranking}-snapshot", False, 0)


async def test_hybrid_arms_run_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recall = backend(tmp_path, ChatSessionManager(tmp_path))
    literal_prepared = asyncio.Event()
    semantic_prepared = asyncio.Event()
    literal_ranked = asyncio.Event()
    semantic_ranked = asyncio.Event()

    async def literal(request: RecallSearchRequest, scope: RecallScope) -> _WaitingArm:
        literal_prepared.set()
        await semantic_prepared.wait()
        return _WaitingArm("literal", literal_ranked, semantic_ranked)

    async def semantic(request: RecallSearchRequest, scope: RecallScope) -> _WaitingArm:
        semantic_prepared.set()
        await literal_prepared.wait()
        return _WaitingArm("semantic", semantic_ranked, literal_ranked)

    monkeypatch.setattr(recall._fts, "prepare_passage_search", literal)
    monkeypatch.setattr(recall._vector, "prepare_search", semantic)
    page = await asyncio.wait_for(recall.search_page(search_request("query")), timeout=2)
    assert page.degraded is False
    assert page.hits == ()


async def test_hybrid_depth_growth_prepares_each_arm_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deeper fusion pass reruns only the rankings, never freshness or query embedding."""

    sessions = ChatSessionManager(tmp_path)
    for index in range(3):
        sessions.create("coder", session_id=f"drive-{index}").append(
            ChatMessage.user(f"I was driving today {index}", timestamp=timestamp(index + 1))
        )
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    stability_checks = 0

    def unstable_once(*args: Any) -> bool:
        nonlocal stability_checks
        stability_checks += 1
        return stability_checks > 1

    revisions = 0
    list_history_revisions = sessions.list_history_revisions

    def counting_revisions(agent_id: str, project_id: str | None = None) -> Any:
        nonlocal revisions
        revisions += 1
        return list_history_revisions(agent_id, project_id)

    syncs = 0
    refresh_passage_index = recall._fts._catalog.refresh

    async def counting_sync(*args: Any) -> None:
        nonlocal syncs
        syncs += 1
        await refresh_passage_index(*args)

    def reject_listing(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the scope read is the only Session listing of a search")

    monkeypatch.setattr(hybrid, "_rrf_page_is_stable", unstable_once)
    monkeypatch.setattr(sessions, "list_history_revisions", counting_revisions)
    monkeypatch.setattr(sessions, "list_summaries", reject_listing)
    monkeypatch.setattr(sessions, "list_history_versions", reject_listing)
    monkeypatch.setattr(recall._fts._catalog, "refresh", counting_sync)

    page = await recall.search_page(search_request("driving"))

    assert stability_checks == 2
    assert page.degraded is False
    assert {hit.session_id for hit in page.hits} == {"drive-0", "drive-1", "drive-2"}
    assert embeddings.embed_calls.count(["driving"]) == 1
    assert revisions == 1
    assert syncs == 1


async def test_hybrid_search_keeps_session_and_store_reads_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="both").append(
        ChatMessage.user("I was driving today", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    calls = forbid_event_loop_calls(monkeypatch, sessions._store)
    database_calls = forbid_database_calls_on_loop(monkeypatch)

    page = await recall.search_page(search_request("driving"))

    # An arm failure would only degrade the page, so both arms must have succeeded.
    assert page.degraded is False
    assert page.hits[0].sources == ("literal", "semantic")
    assert "list_history_revisions" in calls
    assert {"recall_index.read", "recall_vectors.read"} <= set(database_calls)


async def test_hybrid_short_query_retains_literal_and_semantic_sources(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="short").append(
        ChatMessage.user("Go fast", timestamp=timestamp(1))
    )
    page = await backend(tmp_path, sessions, embeddings=_StubEmbeddings()).search_page(
        search_request("go")
    )
    assert page.hits[0].session_id == "short"
    assert page.hits[0].sources == ("literal", "semantic")


@pytest.mark.parametrize(("offset", "limit"), [(0, 10), (8, 2), (9, 1)])
async def test_hybrid_resolves_late_contributions_before_returning_selected_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset: int, limit: int
) -> None:
    literal = ["a", "b", *[f"shared-{index}" for index in range(1, 9)]]
    semantic = [
        *[f"shared-{index}" for index in range(1, 9)],
        *[f"filler-{index}" for index in range(9, 21)],
        "b",
        *[f"filler-{index}" for index in range(22, 40)],
        "a",
    ]

    class Ranking:
        def __init__(self, names: list[str]) -> None:
            self.names = names

        async def prepare(self, *_args: Any) -> Ranking:
            return self

        async def page(self, start: int, count: int) -> RecallSearchPage:
            return RecallSearchPage(
                hits=tuple(
                    RecallSearchHit(
                        "passage", "session", name, "user", "", name, 0, passage_id=name
                    )
                    for name in self.names[start : start + count]
                ),
                result_type="passage",
                ranking="fixture",
                snapshot_id="fixed",
                has_more=start + count < len(self.names),
                total_candidate_sessions=1,
            )

    recall = backend(tmp_path, ChatSessionManager(tmp_path))
    monkeypatch.setattr(recall._fts, "prepare_passage_search", Ranking(literal).prepare)
    monkeypatch.setattr(recall._vector, "prepare_search", Ranking(semantic).prepare)
    try:
        page = await recall.search_page(
            replace(search_request("query", limit=limit), offset=offset)
        )
    finally:
        await recall.aclose()

    # The top ten members are already known at depth 20, but b's semantic
    # contribution at rank 21 must put it before a (whose other rank is 40).
    expected = [f"shared-{index}" for index in range(1, 9)] + ["b", "a"]
    assert [hit.message_id for hit in page.hits] == expected[offset : offset + limit]
    for hit in page.hits:
        assert hit.sources == ("literal", "semantic")
        assert hit.score == pytest.approx(
            1 / (61 + literal.index(hit.message_id)) + 1 / (61 + semantic.index(hit.message_id))
        )
