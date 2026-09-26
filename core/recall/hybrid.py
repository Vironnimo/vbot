"""Hybrid Recall backend combining Passage FTS and Vector rankings.

Search prepares both arms concurrently once, then applies Reciprocal Rank Fusion
with adaptive candidate depth over their rankings. It preserves multiple Passages
per Session and reports one-arm degradation explicitly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from typing import Protocol

from core.recall.canonical import (
    CanonicalSessionRecallBackend,
)
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.recall.sqlite_fts import SqliteFtsRecallBackend
from core.recall.vector import VectorRecallBackend

_RRF_RANK_CONSTANT = 60
_RRF_INITIAL_DEPTH = 20

# Agent-facing query guidance for session_search when this backend is active.
# Static capability text — actual semantic availability is surfaced per-call via
# the notice propagated from the vector arm.
_HYBRID_SEARCH_GUIDANCE = (
    "Literal terms or a short topic description. Every whitespace-separated term is required "
    "by literal search; the same query is also searched by meaning. "
    "Matches combine both rankings by relevance."
)
_HYBRID_TOOL_SUMMARY = (
    "Find past conversations using both keywords and meaning, ranked by combined relevance."
)
# Agent-facing degradation reasons, by which arm failed or answered incompletely.
_SEMANTIC_ONLY_REASON = (
    "Only semantic search succeeded; keyword matches may be missing. "
    "Use a topic description, and do not treat these results as an "
    "exhaustive keyword search."
)
_SEMANTIC_ONLY_PARTIAL_REASON = (
    "Only semantic search succeeded, and it has not finished indexing the eligible Sessions; "
    "results are incomplete. Repeat the search later, and do not treat these results as an "
    "exhaustive keyword search."
)
_LITERAL_ONLY_REASON = (
    "Only keyword search succeeded; paraphrases may be missing. "
    "Use words likely to appear in the conversation; rephrasing cannot "
    "restore semantic search."
)
_SEMANTIC_PARTIAL_REASON = (
    "Semantic search has not finished indexing the eligible Sessions and continues in the "
    "background, so matches by meaning may be missing; keyword matches are complete. Repeat "
    "the search later for complete results."
)


class HybridRecallBackend(CanonicalSessionRecallBackend):
    """Recall backend that fuses FTS literal matches with vector semantic matches."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.logger = context.logger
        self._fts = SqliteFtsRecallBackend(context)
        self._vector = VectorRecallBackend(context)

    def search_capabilities(self) -> RecallSearchCapabilities:
        return RecallSearchCapabilities(
            result_type="passage",
            guidance=_HYBRID_SEARCH_GUIDANCE,
            tool_summary=_HYBRID_TOOL_SUMMARY,
            query_description=_HYBRID_SEARCH_GUIDANCE,
            match_argument="literal_match",
            match_modes=("all_terms", "any_term", "phrase"),
            order_modes=("relevance",),
            default_order="relevance",
        )

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        depth = max(_RRF_INITIAL_DEPTH, request.offset + request.limit + 1)
        # Each arm reconciles its index, and the semantic arm embeds the query,
        # once per search; adaptive depth growth reruns only the rankings.
        arm_request = dataclasses.replace(request, offset=0, limit=depth, snapshot_id=None)
        # Both arms search the same candidates, read once from the Session store.
        scope = await self.sessions.run_async(self._read_scope, arm_request)
        literal_prepared, semantic_prepared = await asyncio.gather(
            self._fts.prepare_passage_search(arm_request, scope),
            self._vector.prepare_search(arm_request, scope),
            return_exceptions=True,
        )
        literal_arm = None if isinstance(literal_prepared, BaseException) else literal_prepared
        semantic_arm = None if isinstance(semantic_prepared, BaseException) else semantic_prepared
        if literal_arm is None and semantic_arm is None:
            raise _hybrid_unavailable()
        literal_page: RecallSearchPage | None = None
        semantic_page: RecallSearchPage | None = None
        fused: list[RecallSearchHit] = []

        while True:
            literal_result, semantic_result = await asyncio.gather(
                _arm_page(literal_arm, depth),
                _arm_page(semantic_arm, depth),
                return_exceptions=True,
            )
            literal_page = literal_result if isinstance(literal_result, RecallSearchPage) else None
            semantic_page = (
                semantic_result if isinstance(semantic_result, RecallSearchPage) else None
            )
            if literal_page is None and semantic_page is None:
                raise _hybrid_unavailable()
            # An arm whose ranking failed stays out for the rest of this search.
            if literal_page is None:
                literal_arm = None
            if semantic_page is None:
                semantic_arm = None
            fused = _fuse_rrf(literal_page, semantic_page)
            if _rrf_page_is_stable(
                fused,
                literal_page,
                semantic_page,
                request.offset + request.limit,
                depth,
            ):
                break
            depth *= 2

        snapshot_id = _hybrid_snapshot(literal_page, semantic_page)
        if request.snapshot_id is not None and request.snapshot_id != snapshot_id:
            raise RecallSearchError(
                "stale_cursor", "Session search source changed; repeat the search."
            )
        selected = fused[request.offset : request.offset + request.limit]
        reason = _degradation_reason(literal_page, semantic_page)
        total_sessions = max(
            literal_page.total_candidate_sessions if literal_page is not None else 0,
            semantic_page.total_candidate_sessions if semantic_page is not None else 0,
        )
        return RecallSearchPage(
            hits=tuple(selected),
            result_type="passage",
            ranking="reciprocal_rank_fusion",
            snapshot_id=snapshot_id,
            has_more=(
                request.offset + len(selected) < len(fused)
                or (literal_page is not None and literal_page.has_more)
                or (semantic_page is not None and semantic_page.has_more)
            ),
            total_candidate_sessions=total_sessions,
            degraded=reason is not None,
            degradation_reason=reason,
        )

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict one session from both fused arms' derived indexes."""
        await asyncio.gather(
            self._fts.remove_session(agent_id, session_id, project_id),
            self._vector.remove_session(agent_id, session_id, project_id),
        )

    async def aclose(self) -> None:
        """Stop background indexing and release both arms' index databases."""
        await asyncio.gather(self._fts.aclose(), self._vector.aclose())

    def close(self) -> None:
        self._fts.close()
        self._vector.close()


class _PreparedArm(Protocol):
    """One retrieval arm whose freshness work is done; it only ranks."""

    async def page(self, offset: int, limit: int) -> RecallSearchPage: ...


async def _arm_page(arm: _PreparedArm | None, depth: int) -> RecallSearchPage | None:
    if arm is None:
        return None
    return await arm.page(0, depth)


def _degradation_reason(
    literal_page: RecallSearchPage | None, semantic_page: RecallSearchPage | None
) -> str | None:
    """Explain a failed arm, or a semantic arm that has not indexed everything yet."""
    semantic_partial = semantic_page is not None and semantic_page.degraded
    if literal_page is None:
        return _SEMANTIC_ONLY_PARTIAL_REASON if semantic_partial else _SEMANTIC_ONLY_REASON
    if semantic_page is None:
        return _LITERAL_ONLY_REASON
    return _SEMANTIC_PARTIAL_REASON if semantic_partial else None


def _hybrid_unavailable() -> RecallSearchError:
    return RecallSearchError(
        "hybrid_unavailable",
        "Both literal and semantic retrieval are unavailable.",
    )


def _fuse_rrf(
    literal_page: RecallSearchPage | None,
    semantic_page: RecallSearchPage | None,
) -> list[RecallSearchHit]:
    hits_by_key: dict[tuple[str, str], RecallSearchHit] = {}
    scores: dict[tuple[str, str], float] = {}
    sources: dict[tuple[str, str], list[str]] = {}
    for source, page in (("literal", literal_page), ("semantic", semantic_page)):
        if page is None:
            continue
        for rank, hit in enumerate(page.hits, start=1):
            key = (hit.session_id, hit.passage_id or hit.message_id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_RANK_CONSTANT + rank)
            if key not in hits_by_key or source == "literal":
                hits_by_key[key] = hit
            source_list = sources.setdefault(key, [])
            if source not in source_list:
                source_list.append(source)
    fused = [
        dataclasses.replace(hit, score=scores[key], sources=tuple(sources[key]))
        for key, hit in hits_by_key.items()
    ]
    fused.sort(
        key=lambda hit: (
            -hit.score,
            hit.session_id,
            hit.passage_id or hit.message_id,
        )
    )
    return fused


def _rrf_page_is_stable(
    fused: list[RecallSearchHit],
    literal_page: RecallSearchPage | None,
    semantic_page: RecallSearchPage | None,
    needed: int,
    depth: int,
) -> bool:
    literal_more = literal_page is not None and literal_page.has_more
    semantic_more = semantic_page is not None and semantic_page.has_more
    if not literal_more and not semantic_more:
        return True
    if len(fused) < needed:
        return False
    # Membership alone does not establish the order within the prefix or its
    # source labels: an unseen contribution can still reorder selected hits.
    # Every selected score must be final before the outside bound can settle it.
    for hit in fused[:needed]:
        if (literal_more and "literal" not in hit.sources) or (
            semantic_more and "semantic" not in hit.sources
        ):
            return False
    literal_bound = 1.0 / (_RRF_RANK_CONSTANT + depth + 1) if literal_more else 0.0
    semantic_bound = 1.0 / (_RRF_RANK_CONSTANT + depth + 1) if semantic_more else 0.0
    competitor_bound = literal_bound + semantic_bound
    for hit in fused[needed:]:
        upper = hit.score
        if literal_more and "literal" not in hit.sources:
            upper += literal_bound
        if semantic_more and "semantic" not in hit.sources:
            upper += semantic_bound
        competitor_bound = max(competitor_bound, upper)
    return fused[needed - 1].score > competitor_bound


def _hybrid_snapshot(
    literal_page: RecallSearchPage | None,
    semantic_page: RecallSearchPage | None,
) -> str:
    literal = literal_page.snapshot_id if literal_page is not None else "unavailable"
    semantic = semantic_page.snapshot_id if semantic_page is not None else "unavailable"
    return hashlib.sha256(f"{literal}\0{semantic}".encode()).hexdigest()


__all__ = ["HybridRecallBackend"]
