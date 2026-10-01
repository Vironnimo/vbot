"""Vector Recall backend over the Passage index's sqlite-vec vectors.

Search builds source-derived Passages, applies scope/Session/time filters inside
KNN, and returns pure semantic top-K Passages without Session deduplication,
a universal distance cutoff, or literal fallback. Each Passage is reported for
one candidate Session, so a fork and its origin never both return the history
they share.

A search embeds only its query. It refreshes the catalog for its candidates
structurally, ranks the Passages that have a vector, and reports partial
coverage while some still wait. Document embedding belongs to the
:class:`~core.recall.semantic_indexer.SemanticIndexer`; a search that finds
waiting Passages nudges it through ``on_waiting``.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from core.database import DatabaseError
from core.model_tasks import EmbeddingError, EmbeddingResult, EmbeddingUsage
from core.recall._passage_catalog import Candidates
from core.recall.canonical import CanonicalSessionRecallBackend, RecallScope
from core.recall.passage_index import (
    PassageIndex,
    PassageIndexError,
    VectorHeader,
    space_change_reason,
)
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)

_T = TypeVar("_T")

# Agent-facing query guidance for session_search when this backend is active.
# Static: it describes the capability, not the current availability — actual
# availability is surfaced per-call in the error result.
_SEMANTIC_SEARCH_GUIDANCE = (
    "Short topic description to find by meaning. Bare keywords anchor poorly and exact "
    "occurrences may be missed. Matches are ranked by semantic "
    "relevance."
)
_SEMANTIC_TOOL_SUMMARY = "Find past conversations about a topic, ranked by similarity in meaning."
# Agent-facing: the search answered before every eligible Passage had a vector.
SEMANTIC_PARTIAL_REASON = (
    "Semantic search has not finished indexing the eligible Sessions and continues in the "
    "background. Results are incomplete: repeat the search later for complete results. "
    "An empty result does not establish that no related conversation exists."
)
# Failures that make semantic search unavailable for this request.
_SEMANTIC_FAILURES = (PassageIndexError, EmbeddingError, DatabaseError, sqlite3.Error, OSError)


@dataclass
class _QueryUsage:
    """Usage of the query embeddings of one search."""

    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    provider_id: str = ""
    model_id: str = ""

    def add(self, result: EmbeddingResult) -> None:
        self.usage = self.usage.combined(result.usage)
        self.provider_id = result.provider_id
        self.model_id = result.actual_model_id


@dataclass(frozen=True)
class PreparedSemanticSearch:
    """One semantic search whose catalog is fresh and whose query is embedded.

    ``page`` ranks at any depth without repeating freshness or query embedding.
    ``header`` is ``None`` when the request selects no candidate Session.
    ``pending`` counts the candidates' Passages in the period still waiting
    for a vector; while any wait, every page reports partial coverage.
    """

    backend: VectorRecallBackend
    request: RecallSearchRequest
    snapshot_id: str
    candidates: Candidates | None = None
    header: VectorHeader | None = None
    query_vector: tuple[float, ...] = ()
    pending: int = 0

    async def page(self, offset: int, limit: int) -> RecallSearchPage:
        return await self.backend._search_prepared(self, offset=offset, limit=limit)


class VectorRecallBackend(CanonicalSessionRecallBackend):
    """Recall backend backed by sqlite-vec Passage vectors.

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
        self._owns_index = index is None
        self.index = index if index is not None else PassageIndex(context.data_dir)
        self.logger = context.logger
        self.embeddings = context.embeddings
        self._on_waiting = on_waiting

    def search_capabilities(self) -> RecallSearchCapabilities:
        return RecallSearchCapabilities(
            result_type="passage",
            guidance=_SEMANTIC_SEARCH_GUIDANCE,
            tool_summary=_SEMANTIC_TOOL_SUMMARY,
            query_description=_SEMANTIC_SEARCH_GUIDANCE,
            order_modes=("relevance",),
            default_order="relevance",
        )

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        prepared = await self.prepare_search(request)
        return await prepared.page(request.offset, request.limit)

    async def prepare_search(
        self,
        request: RecallSearchRequest,
        scope: RecallScope | None = None,
        *,
        refreshed: bool = False,
    ) -> PreparedSemanticSearch:
        """Refresh the request's candidates in the catalog and embed the query.

        A caller that already read the request's scope passes it; one that
        already refreshed the catalog for it passes ``refreshed``. A damaged
        index is discarded and the preparation runs once more, refreshing the
        rebuilt catalog.
        """

        usage = _QueryUsage()
        attempts = 0

        async def prepare() -> PreparedSemanticSearch:
            nonlocal attempts
            attempts += 1
            return await self._prepare(
                request, scope, refresh=not refreshed or attempts > 1, usage=usage
            )

        try:
            return await self._semantic_operation(prepare, recover=True)
        finally:
            self._log_query_usage(usage)

    async def aclose(self) -> None:
        """Release the index database when this backend owns it."""
        self.close()

    def close(self) -> None:
        if self._owns_index:
            self.index.close()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def _prepare(
        self,
        request: RecallSearchRequest,
        scope: RecallScope | None,
        *,
        refresh: bool,
        usage: _QueryUsage,
    ) -> PreparedSemanticSearch:
        binding = await asyncio.to_thread(self._binding_header)
        if binding is None:
            raise RecallSearchError(
                "semantic_unavailable",
                "Semantic search is unavailable because no embedding model is configured.",
            )
        if scope is None:
            scope = await self.sessions.run_async(self._read_scope, request)
        stored = await self.index.read_header()
        pinned = stored if stored is not None and stored.same_space(binding) else None
        if pinned is not None:
            # A pinned header already names the complete embedding space, so a
            # stale continuation fails before the query is paid for.
            self._check_snapshot(request, self._vector_snapshot(scope, pinned))
        if not scope.candidates:
            snapshot_id = self._vector_snapshot(scope, pinned or binding)
            self._check_snapshot(request, snapshot_id)
            return PreparedSemanticSearch(self, request, snapshot_id)

        # The query embedding observes the live dimension and actual model.
        query_vector, header = await self._embed_query(binding, request.query, usage)
        snapshot_id = self._vector_snapshot(scope, header)
        self._check_snapshot(request, snapshot_id)
        if stored != header and await self.index.use_space(header):
            self._log_space_change(stored, header)
        if refresh:
            await self.index.refresh(self.sessions, request.agent_id, request.project_id, scope)
        candidates = Candidates.of(request.agent_id, request.project_id, scope)
        pending = await self.index.count_pending(
            candidates, since=request.since, until=request.until
        )
        if pending and self._on_waiting is not None:
            self._on_waiting()
        return PreparedSemanticSearch(
            self,
            request,
            snapshot_id,
            candidates,
            header,
            tuple(query_vector),
            pending,
        )

    async def _search_prepared(
        self,
        prepared: PreparedSemanticSearch,
        *,
        offset: int,
        limit: int,
    ) -> RecallSearchPage:
        header = prepared.header
        candidates = prepared.candidates
        if header is None or candidates is None:
            return RecallSearchPage(
                hits=(),
                result_type="passage",
                ranking="cosine_distance",
                snapshot_id=prepared.snapshot_id,
                has_more=False,
                total_candidate_sessions=0,
            )
        request = prepared.request
        result = await self._semantic_operation(
            lambda: self.index.knn_search(
                header=header,
                query_vector=prepared.query_vector,
                limit=offset + limit + 1,
                candidates=candidates,
                since=request.since,
                until=request.until,
            ),
            recover=False,
        )
        ranked = [
            RecallSearchHit(
                result_type="passage",
                session_id=session_id,
                message_id=passage.start_message_id,
                role=passage.start_role,
                timestamp=passage.start_timestamp,
                text=passage.text,
                score=distance,
                passage_id=passage.passage_id,
                start_message_id=passage.start_message_id,
                end_message_id=passage.end_message_id,
                end_timestamp=passage.end_timestamp,
                sources=("semantic",),
            )
            for passage, session_id, distance in result.matches
        ]
        page_hits = ranked[offset : offset + limit]
        return RecallSearchPage(
            hits=tuple(page_hits),
            result_type="passage",
            ranking="cosine_distance",
            snapshot_id=prepared.snapshot_id,
            # A capped ranking that this page reaches the end of still has more
            # Passages beyond what the index can rank.
            has_more=(
                len(ranked) > offset + len(page_hits) or (result.truncated and bool(page_hits))
            ),
            total_candidate_sessions=len(candidates.creation_orders),
            degraded=prepared.pending > 0,
            degradation_reason=SEMANTIC_PARTIAL_REASON if prepared.pending > 0 else None,
        )

    async def _semantic_operation(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        recover: bool,
    ) -> _T:
        """Run one index operation; map index/provider failures to ``semantic_unavailable``.

        With ``recover`` a damaged index is discarded and the operation retried
        once; without, a damaged index is discarded so the next search
        rebuilds it.
        """

        try:
            if recover:
                return await self.index.recovering(
                    operation,
                    warning=lambda error: self._warning(
                        "Vector recall index failed; rebuilding once: %s", error
                    ),
                )
            try:
                return await operation()
            except Exception as error:
                await self.index.discard_if_damaged(error)
                raise
        except _SEMANTIC_FAILURES as error:
            self._warning("Vector recall failed: %s", error)
            raise RecallSearchError(
                "semantic_unavailable",
                "Semantic search failed; retry or check the embedding provider.",
            ) from error

    @staticmethod
    def _vector_snapshot(scope: RecallScope, header: VectorHeader) -> str:
        payload = (
            f"{scope.snapshot_id}\0{header.provider_id}\0{header.model_id}\0"
            f"{header.space_fingerprint}\0{header.index_policy}"
            f"\0{header.dimension}"
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _check_snapshot(request: RecallSearchRequest, snapshot_id: str) -> None:
        if request.snapshot_id is not None and request.snapshot_id != snapshot_id:
            raise RecallSearchError(
                "stale_cursor",
                "Session search source changed; repeat the search.",
            )

    # ------------------------------------------------------------------
    # Query embedding
    # ------------------------------------------------------------------

    def _binding_header(self) -> VectorHeader | None:
        """Resolve the binding identity without a dimension; ``None`` means no usable binding."""

        if self.embeddings is None:
            return None
        try:
            identity = self.embeddings.resolve_space()
        except EmbeddingError as error:
            self._warning("Vector recall binding lookup failed: %s", error)
            return None
        return VectorHeader.for_space(identity)

    async def _embed_query(
        self, binding: VectorHeader, query: str, usage: _QueryUsage
    ) -> tuple[list[float], VectorHeader]:
        """Embed the query and resolve the live dimension and actual model."""

        embeddings = self.embeddings
        if embeddings is None:
            raise EmbeddingError("no embedding model is configured")
        result: EmbeddingResult = await embeddings.embed([query], purpose="query")
        usage.add(result)
        if result.dimension <= 0:
            raise PassageIndexError(
                f"embedding provider returned empty dimension for {binding.model_id}"
            )
        header = VectorHeader.from_result(result)
        if not header.same_space(binding):
            raise EmbeddingError("embedding space changed while the Recall request was running")
        return list(result.vectors[0]), header

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_space_change(self, stored: VectorHeader | None, header: VectorHeader) -> None:
        if self.logger is not None and hasattr(self.logger, "info"):
            self.logger.info(
                "Pinned Recall embedding space (provider=%s model=%s dimension=%d reason=%s)",
                header.provider_id,
                header.response_model_id or header.model_id,
                header.dimension,
                space_change_reason(stored, header),
            )

    def _log_query_usage(self, aggregate: _QueryUsage) -> None:
        """Emit one normalized DEBUG usage summary per search, never provider payloads."""

        usage = aggregate.usage
        if usage.requests <= 0:
            return
        if self.logger is not None and hasattr(self.logger, "debug"):
            self.logger.debug(
                "Used embeddings (operation=typed_search provider=%s model=%s requests=%d "
                "token_reports=%d input_tokens=%d total_tokens=%d cost_reports=%d "
                "cost=%.12g)",
                aggregate.provider_id,
                aggregate.model_id,
                usage.requests,
                usage.token_reports,
                usage.input_tokens,
                usage.total_tokens,
                usage.cost_reports,
                usage.cost,
            )

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


__all__ = ["SEMANTIC_PARTIAL_REASON", "PreparedSemanticSearch", "VectorRecallBackend"]
