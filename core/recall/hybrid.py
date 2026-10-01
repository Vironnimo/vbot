"""Hybrid Recall backend combining literal and semantic Passage rankings.

Both arms rank Passages of the one Passage index. A search refreshes the
catalog for its candidates once, embeds its query once, then applies Reciprocal
Rank Fusion with adaptive candidate depth over both rankings. The depth stops
at ``_RRF_MAX_DEPTH``, where the fused prefix is accepted as it stands. It
preserves multiple Passages per Session and reports one-arm degradation
explicitly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Callable
from typing import Protocol

from core.recall._passage_catalog import Candidates, StoredPassage
from core.recall.canonical import (
    CanonicalSessionRecallBackend,
    RecallScope,
    first_match_span,
)
from core.recall.passage_index import (
    KNN_MAX_K,
    TRIGRAM_RANKING,
    LiteralQuery,
    PassageIndex,
    literal_query,
)
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.recall.vector import VectorRecallBackend

_RRF_RANK_CONSTANT = 60
_RRF_INITIAL_DEPTH = 20
# The deepest ranking either arm is asked for. A semantic page of this depth
# asks the vector index for one row more, which must stay within its KNN limit.
_RRF_MAX_DEPTH = 2048
assert _RRF_MAX_DEPTH < KNN_MAX_K

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
    """Recall backend that fuses literal Passage matches with semantic matches.

    The Runtime passes its one shared Passage ``index`` and the indexer's
    ``on_waiting`` nudge. Without an index the backend opens and owns its own.
    """

    def __init__(
        self,
        context: RecallBackendContext,
        *,
        index: PassageIndex | None = None,
        on_waiting: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.logger = context.logger
        self._owns_index = index is None
        self.index = index if index is not None else PassageIndex(context.data_dir)
        self._vector = VectorRecallBackend(context, index=self.index, on_waiting=on_waiting)

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
        depth = min(max(_RRF_INITIAL_DEPTH, request.offset + request.limit + 1), _RRF_MAX_DEPTH)
        # The catalog is refreshed and the query embedded once per search;
        # adaptive depth growth reruns only the rankings.
        arm_request = dataclasses.replace(request, offset=0, limit=depth, snapshot_id=None)
        # Both arms search the same candidates, read once from the Session store.
        scope = await self.sessions.run_async(self._read_scope, arm_request)
        if scope.candidates:
            await self._refresh(arm_request, scope)
        literal_arm: _PreparedArm | None = self._prepare_literal(arm_request, scope)
        semantic_arm: _PreparedArm | None
        try:
            semantic_arm = await self._vector.prepare_search(arm_request, scope, refreshed=True)
        except Exception:
            # The vector backend logged the failure; keyword results remain.
            semantic_arm = None
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
            if depth >= _RRF_MAX_DEPTH or _rrf_page_is_stable(
                fused,
                literal_page,
                semantic_page,
                request.offset + request.limit,
                depth,
            ):
                break
            depth = min(depth * 2, _RRF_MAX_DEPTH)

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
        arms_have_more = (literal_page is not None and literal_page.has_more) or (
            semantic_page is not None and semantic_page.has_more
        )
        if depth >= _RRF_MAX_DEPTH:
            # At the depth limit the fused ranking is all a search can page
            # through; a page that reaches its end still has unranked matches.
            has_more = request.offset + len(selected) < len(fused) or (
                arms_have_more and bool(selected)
            )
        else:
            has_more = request.offset + len(selected) < len(fused) or arms_have_more
        return RecallSearchPage(
            hits=tuple(selected),
            result_type="passage",
            ranking="reciprocal_rank_fusion",
            snapshot_id=snapshot_id,
            has_more=has_more,
            total_candidate_sessions=total_sessions,
            degraded=reason is not None,
            degradation_reason=reason,
        )

    def _prepare_literal(self, request: RecallSearchRequest, scope: RecallScope) -> _PreparedArm:
        """The literal arm over the catalog this search refreshed."""
        return _PreparedLiteralSearch(self.index, request, scope, literal_query(request))

    async def _refresh(self, request: RecallSearchRequest, scope: RecallScope) -> None:
        """Bring the candidates' Passages up to date for both arms.

        A damaged index is discarded and refreshed once. Any other failure
        leaves neither arm usable.
        """
        try:
            await self.index.recovering(
                lambda: self.index.refresh(
                    self.sessions, request.agent_id, request.project_id, scope
                ),
                warning=lambda error: self._warning(
                    "Passage index failed; rebuilding once: %s", error
                ),
            )
        except Exception as error:
            self._warning("Hybrid recall could not refresh the Passage index: %s", error)
            raise _hybrid_unavailable() from error

    async def aclose(self) -> None:
        """Release the index database when this backend owns it."""
        self.close()

    def close(self) -> None:
        if self._owns_index:
            self.index.close()

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


class _PreparedLiteralSearch:
    """Literal Passage ranking over a catalog refreshed once for one search."""

    def __init__(
        self,
        index: PassageIndex,
        request: RecallSearchRequest,
        scope: RecallScope,
        query: LiteralQuery | None,
    ) -> None:
        self._index = index
        self._request = request
        self._scope = scope
        self._query = query

    async def page(self, offset: int, limit: int) -> RecallSearchPage:
        query = self._query
        if query is None or not self._scope.candidates:
            return RecallSearchPage(
                hits=(),
                result_type="passage",
                ranking=TRIGRAM_RANKING if query is None else query.ranking,
                snapshot_id=self._scope.snapshot_id,
                has_more=False,
                total_candidate_sessions=len(self._scope.candidates),
            )
        request = self._request
        candidates = Candidates.of(request.agent_id, request.project_id, self._scope)
        try:
            matches = await self._index.literal_search(
                request, candidates, query, offset + limit + 1
            )
        except Exception as error:
            await self._index.discard_if_damaged(error)
            raise
        hits = tuple(
            _literal_hit(passage, session_id, rank, request)
            for passage, session_id, rank in matches[offset : offset + limit]
        )
        return RecallSearchPage(
            hits=hits,
            result_type="passage",
            ranking=query.ranking,
            snapshot_id=self._scope.snapshot_id,
            has_more=len(matches) > offset + limit,
            total_candidate_sessions=len(self._scope.candidates),
        )


def _literal_hit(
    passage: StoredPassage, session_id: str, rank: float, request: RecallSearchRequest
) -> RecallSearchHit:
    start, end = first_match_span(passage.text, request.query, request.match_mode)
    return RecallSearchHit(
        result_type="passage",
        session_id=session_id,
        message_id=passage.start_message_id,
        role=passage.start_role,
        timestamp=passage.start_timestamp,
        text=passage.text,
        score=rank,
        passage_id=passage.passage_id,
        start_message_id=passage.start_message_id,
        end_message_id=passage.end_message_id,
        end_timestamp=passage.end_timestamp,
        match_start=start,
        match_end=end,
        sources=("literal",),
    )


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
