"""Tests for the hybrid recall backend.

The hybrid backend fuses the SQLite FTS and vector arms. These tests
construct it directly (no registry) and exercise the headline
behaviors: literal-keyword coverage, conceptual-query fallback,
shared-session tagging, graceful degradation when no embedding
binding is configured, ordering rules, over-fetch, and short queries
that fall through the FTS trigram path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.model_tasks import EmbeddingResult
from core.recall import RecallBackendContext, RecallSearchRequest
from core.recall.hybrid import (
    HybridRecallBackend,
)
from core.sessions import ChatSessionManager

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

    def resolve_model_id(self) -> tuple[str, str]:
        self.resolve_calls += 1
        return (self.provider_id, self.model_id)

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


# ---------------------------------------------------------------------------
# Literal-keyword coverage (the ``bild`` regression)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Conceptual query → semantic-only
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session hit by both arms → tagged ``both`` with literal snippet + distance
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# No embedding binding → literal-only, no crash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Ordering: literal/both first, then semantic; literal honors sort, semantic by distance
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Over-fetch: a single session with many literal hits cannot starve the budget
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Short query (2 chars) — FTS trigram falls back, vector still embeds
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


async def test_hybrid_arms_run_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from core.recall import RecallSearchPage

    recall = backend(tmp_path, ChatSessionManager(tmp_path))
    literal_started = asyncio.Event()
    semantic_started = asyncio.Event()

    async def literal(request: RecallSearchRequest) -> RecallSearchPage:
        literal_started.set()
        await semantic_started.wait()
        return RecallSearchPage((), "passage", "literal", "literal-snapshot", False, 0)

    async def semantic(request: RecallSearchRequest) -> RecallSearchPage:
        semantic_started.set()
        await literal_started.wait()
        return RecallSearchPage((), "passage", "semantic", "semantic-snapshot", False, 0)

    monkeypatch.setattr(recall._fts, "search_passages", literal)
    monkeypatch.setattr(recall._vector, "search_page", semantic)
    page = await asyncio.wait_for(recall.search_page(search_request("query")), timeout=2)
    assert page.degraded is False
    assert page.hits == ()


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
