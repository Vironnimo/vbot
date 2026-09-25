"""Vector Recall backend over a sqlite-vec Passage index.

Search builds source-derived Passages, applies scope/Session/time filters inside
KNN, and returns pure semantic top-K Passages without Session deduplication,
a universal distance cutoff, or literal fallback. Each Passage is reported for
one candidate Session, so a fork and its origin never both return the history
they share. One disposable store pins the embedding-space fingerprint, index
policy, and dimension; a change of space queues every Passage for embedding
again. Within one space, a new Passage embeds only when no stored Passage has
its text.

Embedding never blocks a search beyond one bounded batch: a search indexes its
candidates' Passages structurally, embeds at most ``_EMBED_BATCH_SIZE`` waiting
texts, answers from the Passages that have a vector, and reports partial
coverage while others still wait. A background task embeds the rest.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar, cast

from core.database import DatabaseError
from core.model_tasks import (
    EmbeddingError,
    EmbeddingPurpose,
    EmbeddingResult,
    EmbeddingService,
    EmbeddingUsage,
)
from core.models.models import ModelRegistry
from core.recall._passage_catalog import Candidates
from core.recall.canonical import (
    CanonicalSessionRecallBackend,
    RecallScope,
)
from core.recall.passages import (
    PASSAGE_OVERLAP_CHARS,
    PASSAGE_POLICY_VERSION,
    PASSAGE_TARGET_CHARS,
)
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.recall.vector_store import (
    VectorHeader,
    VectorStore,
    VectorStoreError,
)

_T = TypeVar("_T")

# Maximum number of texts embedded in one provider call, and the most Passage
# texts one search embeds before it answers. Splitting keeps the per-request
# payload predictable and the shrink-retry path bounded per batch.
_EMBED_BATCH_SIZE = 64
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
    "background. Results are incomplete: repeat the search later, or narrow period or "
    "session_id. An empty result does not establish that no related conversation exists."
)
_INDEX_POLICY = (
    f"passage-v{PASSAGE_POLICY_VERSION}:target={PASSAGE_TARGET_CHARS}:"
    f"overlap={PASSAGE_OVERLAP_CHARS}"
)
# Failures that make semantic search unavailable for this request.
_SEMANTIC_FAILURES = (VectorStoreError, EmbeddingError, DatabaseError, sqlite3.Error, OSError)


@dataclass
class _EmbeddingOperationUsage:
    """Request-local aggregate for one Recall Search operation."""

    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    query_inputs: int = 0
    document_inputs: int = 0
    provider_id: str = ""
    model_id: str = ""

    def add(
        self,
        result: EmbeddingResult,
        *,
        purpose: EmbeddingPurpose,
        input_count: int,
    ) -> None:
        self.usage = self.usage.combined(result.usage)
        if purpose == "query":
            self.query_inputs += input_count
        else:
            self.document_inputs += input_count
        self.provider_id = result.provider_id
        self.model_id = result.actual_model_id


@dataclass(frozen=True)
class PreparedSemanticSearch:
    """One semantic search whose index is fresh and whose query is embedded.

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
    """Recall backend backed by sqlite-vec Passage vectors."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.store = VectorStore(context.data_dir)
        self.logger = context.logger
        self.embeddings: EmbeddingService | None = context.embeddings
        self.model_registry: ModelRegistry | None = context.model_registry
        # Text hashes some embedding call of this backend is working on, so a
        # search and the background task never pay for the same text twice.
        self._embedding: set[str] = set()
        self._backfill_task: asyncio.Task[None] | None = None
        self._backfill_header: VectorHeader | None = None
        self._closed = False

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
        self, request: RecallSearchRequest, scope: RecallScope | None = None
    ) -> PreparedSemanticSearch:
        """Index the request's candidates, embed one bounded batch and the query.

        Emits one embedding Usage summary for the whole operation. A caller that
        already read the request's scope passes it.
        """

        usage = _EmbeddingOperationUsage()
        try:
            return await self._semantic_operation(
                lambda: self._prepare(request, usage, scope), recover=True
            )
        finally:
            self._log_embedding_usage("typed_search", usage)

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict one Session from the vector index (delete-time cleanup).

        Passages another indexed Session still shows keep their vectors.
        """
        await self.store.remove_session(agent_id, project_id, session_id)

    async def aclose(self) -> None:
        """Stop background indexing and release the index database."""
        self._closed = True
        task, self._backfill_task = self._backfill_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        self.store.close()

    def close(self) -> None:
        """Synchronous :meth:`aclose`: background indexing is cancelled, not awaited."""
        self._closed = True
        task, self._backfill_task = self._backfill_task, None
        if task is not None and not task.done() and not task.get_loop().is_closed():
            task.get_loop().call_soon_threadsafe(task.cancel)
        self.store.close()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def _prepare(
        self,
        request: RecallSearchRequest,
        usage: _EmbeddingOperationUsage,
        scope: RecallScope | None,
    ) -> PreparedSemanticSearch:
        binding = await asyncio.to_thread(self._binding_header)
        if binding is None:
            raise RecallSearchError(
                "semantic_unavailable",
                "Semantic search is unavailable because no embedding model is configured.",
            )
        if scope is None:
            scope = await self.sessions.run_async(self._read_scope, request)
        stored = await self.store.read_header()
        pinned = (
            stored
            if stored is not None
            and self._headers_match(stored, binding, include_response_model=False)
            else None
        )
        if pinned is not None:
            # A pinned header already names the complete embedding space, so a
            # stale continuation fails before any embedding is paid for.
            self._check_snapshot(request, self._vector_snapshot(scope, pinned))
        if not scope.candidates:
            snapshot_id = self._vector_snapshot(scope, pinned or binding)
            self._check_snapshot(request, snapshot_id)
            return PreparedSemanticSearch(self, request, snapshot_id)

        # The query embedding observes the live dimension and actual model
        # first; every document vector of this search must match it.
        query_vector, header = await self._embed_query(
            binding,
            request.query,
            index_policy=_INDEX_POLICY,
            usage=usage,
        )
        snapshot_id = self._vector_snapshot(scope, header)
        self._check_snapshot(request, snapshot_id)
        if stored != header:
            self._warn_space_change(stored, header)
            await self.store.use_space(header)
        await self.store.refresh(self.sessions, request.agent_id, request.project_id, scope)
        candidates = Candidates.of(request.agent_id, request.project_id, scope)
        await self._embed_search_batch(header, candidates, request, usage)
        pending = await self.store.count_pending(
            candidates, since=request.since, until=request.until
        )
        if pending:
            self._start_backfill(header)
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
        matches = await self._semantic_operation(
            lambda: self.store.knn_search(
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
            for passage, session_id, distance in matches
        ]
        page_hits = ranked[offset : offset + limit]
        return RecallSearchPage(
            hits=tuple(page_hits),
            result_type="passage",
            ranking="cosine_distance",
            snapshot_id=prepared.snapshot_id,
            has_more=len(ranked) > offset + len(page_hits),
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
                return await self.store.recovering(
                    operation,
                    warning=lambda error: self._warning(
                        "Vector recall index failed; rebuilding once: %s", error
                    ),
                )
            try:
                return await operation()
            except Exception as error:
                await self.store.discard_if_damaged(error)
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
            f"{header.response_model_id}\0{header.space_fingerprint}\0{header.index_policy}"
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
    # Embedding helpers
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
        return VectorHeader(
            provider_id=identity.provider_id,
            model_id=identity.model_id,
            dimension=0,
            space_fingerprint=identity.fingerprint,
            index_policy=_INDEX_POLICY,
            response_model_id="",
        )

    def _warn_space_change(self, stored: VectorHeader | None, header: VectorHeader) -> None:
        if stored is None:
            return
        if not self._headers_match(stored, header, include_response_model=False):
            self._warning(
                "Vector recall embedding space or index policy changed "
                "(%s/%s → %s/%s); embedding every Passage again",
                stored.provider_id,
                stored.model_id,
                header.provider_id,
                header.model_id,
            )
        elif stored.response_model_id != header.response_model_id:
            self._warning(
                "Embedding response model changed (%s → %s); embedding every Passage again",
                stored.response_model_id,
                header.response_model_id,
            )
        else:
            self._warning(
                "Embedding dimension changed (%d → %d); embedding every Passage again",
                stored.dimension,
                header.dimension,
            )

    @staticmethod
    def _headers_match(
        left: VectorHeader,
        right: VectorHeader,
        *,
        include_dimension: bool = False,
        include_response_model: bool = True,
    ) -> bool:
        identity_matches = (
            left.provider_id == right.provider_id
            and left.model_id == right.model_id
            and left.space_fingerprint == right.space_fingerprint
            and left.index_policy == right.index_policy
        )
        response_model_matches = (
            not include_response_model or left.response_model_id == right.response_model_id
        )
        return (
            identity_matches
            and response_model_matches
            and (not include_dimension or left.dimension == right.dimension)
        )

    async def _embed_query(
        self,
        header: VectorHeader,
        query: str,
        *,
        index_policy: str,
        usage: _EmbeddingOperationUsage,
    ) -> tuple[list[float], VectorHeader]:
        """Embed a single query string and resolve the live dimension and actual model."""

        result = await self._run_embed([query], purpose="query")
        usage.add(result, purpose="query", input_count=1)
        if result.dimension <= 0:
            raise VectorStoreError(
                f"embedding provider returned empty dimension for {header.model_id}"
            )
        resolved = self._header_from_result(
            result,
            header,
            index_policy=index_policy,
            allow_response_model_change=True,
        )
        return list(result.vectors[0]), resolved

    def _header_from_result(
        self,
        result: EmbeddingResult,
        expected: VectorHeader,
        *,
        index_policy: str,
        allow_response_model_change: bool = False,
    ) -> VectorHeader:
        resolved = VectorHeader(
            provider_id=result.provider_id,
            model_id=result.model_id,
            dimension=result.dimension,
            space_fingerprint=result.space_fingerprint or expected.space_fingerprint,
            index_policy=index_policy,
            response_model_id=result.actual_model_id,
        )
        if not self._headers_match(
            resolved,
            expected,
            include_response_model=False,
        ):
            raise EmbeddingError("embedding space changed while the Recall request was running")
        if (
            not allow_response_model_change
            and expected.response_model_id
            and resolved.response_model_id != expected.response_model_id
        ):
            raise EmbeddingError(
                "embedding response model changed while the Recall request was running: "
                f"{expected.response_model_id} → {resolved.response_model_id}"
            )
        return resolved

    async def _embed_documents(
        self,
        texts: list[str],
        header: VectorHeader,
        *,
        usage: _EmbeddingOperationUsage,
    ) -> list[list[float]]:
        """Embed Passage texts; they must land in exactly the query's space."""

        result = await self._run_embed(texts, purpose="document")
        usage.add(result, purpose="document", input_count=len(texts))
        resolved = self._header_from_result(result, header, index_policy=header.index_policy)
        if resolved.dimension != header.dimension:
            raise EmbeddingError(
                f"embedding dimension changed while the Recall request was running: "
                f"{header.dimension} → {resolved.dimension}"
            )
        return [list(vector) for vector in result.vectors]

    async def _run_embed(
        self,
        texts: list[str],
        *,
        purpose: EmbeddingPurpose = "document",
    ) -> EmbeddingResult:
        """Embed *texts*, batching into ``_EMBED_BATCH_SIZE`` groups.

        A context overflow on a multi-input call recursively divides that call
        until each accepted provider request fits. A single rejected text is
        never modified here: Passage construction owns any truncation so the
        text stored beside a vector remains byte-for-byte honest. Results are
        concatenated in input order and every configured/actual model,
        provider, dimension, and fingerprint must stay consistent.
        """

        if self.embeddings is None:
            raise EmbeddingError("embedding service is not configured")
        if not texts:
            raise EmbeddingError("embedding input is empty")
        if len(texts) == 1:
            return await self._run_embed_batch(texts, purpose=purpose)

        results: list[EmbeddingResult] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            results.append(await self._run_embed_batch(batch, purpose=purpose))
        return self._combine_embedding_results(results)

    async def _run_embed_batch(
        self,
        batch: list[str],
        *,
        purpose: EmbeddingPurpose,
    ) -> EmbeddingResult:
        """Embed one batch, recursively splitting only aggregate overflows."""

        # ``_run_embed`` is the only caller and it raises when the
        # embedding service is missing; the cast keeps mypy happy
        # without re-checking the same condition on every retry.
        embeddings = cast(EmbeddingService, self.embeddings)
        current = list(batch)
        try:
            result = await embeddings.embed(current, purpose=purpose)
            return cast(EmbeddingResult, result)
        except EmbeddingError as error:
            if not _is_context_overflow(error) or len(current) <= 1:
                raise
            midpoint = len(current) // 2
            self._warning(
                "Embedding batch exceeded the model context window; splitting %d inputs "
                "into %d + %d",
                len(current),
                midpoint,
                len(current) - midpoint,
            )
            left = await self._run_embed_batch(current[:midpoint], purpose=purpose)
            right = await self._run_embed_batch(current[midpoint:], purpose=purpose)
            return self._combine_embedding_results([left, right])

    @staticmethod
    def _combine_embedding_results(results: list[EmbeddingResult]) -> EmbeddingResult:
        if not results:
            raise EmbeddingError("embedding result aggregate is empty")
        first = results[0]
        vectors: list[list[float]] = []
        usage = EmbeddingUsage()
        for result in results:
            if result.provider_id != first.provider_id:
                raise EmbeddingError(
                    f"embedding provider changed mid-batch: "
                    f"{first.provider_id} → {result.provider_id}"
                )
            if result.model_id != first.model_id:
                raise EmbeddingError(
                    f"configured embedding model changed mid-batch: "
                    f"{first.model_id} → {result.model_id}"
                )
            if result.actual_model_id != first.actual_model_id:
                raise EmbeddingError(
                    f"embedding response model changed mid-batch: "
                    f"{first.actual_model_id} → {result.actual_model_id}"
                )
            if result.dimension != first.dimension:
                raise EmbeddingError(
                    f"embedding dimension drift: {first.dimension} → {result.dimension}"
                )
            if result.space_fingerprint != first.space_fingerprint:
                raise EmbeddingError("embedding space changed mid-batch")
            vectors.extend(list(vector) for vector in result.vectors)
            usage = usage.combined(result.usage)
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id=first.model_id,
            provider_id=first.provider_id,
            dimension=first.dimension,
            space_fingerprint=first.space_fingerprint,
            response_model_id=first.actual_model_id,
            usage=usage,
        )

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    async def _embed_search_batch(
        self,
        header: VectorHeader,
        candidates: Candidates,
        request: RecallSearchRequest,
        usage: _EmbeddingOperationUsage,
    ) -> None:
        """Embed one bounded batch of the candidates' waiting texts, newest first.

        A provider failure leaves the texts waiting; the search still answers
        from the Passages that have a vector.
        """

        batch = await self.store.pending_texts(
            candidates,
            limit=_EMBED_BATCH_SIZE,
            since=request.since,
            until=request.until,
            exclude=self._embedding,
        )
        if not batch:
            return
        try:
            await self._embed_and_store(header, batch, usage)
        except EmbeddingError as error:
            self._warning("Vector recall could not embed new Passages: %s", error)

    async def _embed_and_store(
        self,
        header: VectorHeader,
        batch: list[tuple[str, str]],
        usage: _EmbeddingOperationUsage,
    ) -> bool:
        """Embed ``(text_hash, text)`` pairs outside any transaction and store them.

        Returns ``False`` when the index left *header*'s space meanwhile.
        """

        hashes = [text_hash for text_hash, _text in batch]
        self._embedding.update(hashes)
        try:
            vectors = await self._embed_documents(
                [text for _text_hash, text in batch], header, usage=usage
            )
            return await self.store.store_vectors(header, dict(zip(hashes, vectors, strict=True)))
        finally:
            self._embedding.difference_update(hashes)

    def _start_backfill(self, header: VectorHeader) -> None:
        """Embed every waiting Passage in the background, in *header*'s space."""

        self._backfill_header = header
        if self._closed or (self._backfill_task is not None and not self._backfill_task.done()):
            return
        self._backfill_task = asyncio.create_task(self._backfill(), name="vector-recall-backfill")

    async def _backfill(self) -> None:
        """Drain the waiting Passages of every scope in batches, newest first.

        Each batch is one provider call followed by one short write, so searches
        never wait behind the whole backfill. It stops when nothing waits, when
        the index leaves the space it embeds for, or on the first failure; the
        next search that finds waiting Passages starts it again.
        """

        usage = _EmbeddingOperationUsage()
        try:
            while not self._closed:
                header = self._backfill_header
                if header is None:
                    return
                batch = await self.store.pending_texts(
                    None, limit=_EMBED_BATCH_SIZE, exclude=self._embedding
                )
                if not batch:
                    return
                if not await self._embed_and_store(header, batch, usage) and (
                    self._backfill_header == header
                ):
                    # The index left this space and no search named a newer one.
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._warning("Vector recall background indexing stopped: %s", error)
            await self.store.discard_if_damaged(error)
        finally:
            self._log_embedding_usage("recall_backfill", usage)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_embedding_usage(
        self,
        operation: str,
        aggregate: _EmbeddingOperationUsage,
    ) -> None:
        """Emit one normalized usage summary per Search, never provider payloads."""

        usage = aggregate.usage
        if usage.requests <= 0:
            return
        if self.logger is not None and hasattr(self.logger, "info"):
            self.logger.info(
                "Embedding usage operation=%s provider=%s model=%s requests=%d "
                "token_reports=%d input_tokens=%d total_tokens=%d cost_reports=%d "
                "cost=%.12g query_inputs=%d document_inputs=%d",
                operation,
                aggregate.provider_id,
                aggregate.model_id,
                usage.requests,
                usage.token_reports,
                usage.input_tokens,
                usage.total_tokens,
                usage.cost_reports,
                usage.cost,
                aggregate.query_inputs,
                aggregate.document_inputs,
            )

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


# ---------------------------------------------------------------------------
# Embedding error classification
# ---------------------------------------------------------------------------


def _is_context_overflow(error: Exception) -> bool:
    """True when an embedding error is the provider's context-length rejection.

    The provider's 4xx body reaches us through ``EmbeddingExecutionError``'s
    message (the recall backend never sees the raw response). OpenRouter wraps
    the upstream ``BadRequestError`` text verbatim, so we match the stable
    phrases that identify a token-window overflow across providers.
    """

    message = str(error).lower()
    return (
        "context length" in message
        or "maximum context" in message
        or "context_length_exceeded" in message
        or "input_tokens" in message
    )


__all__ = ["PreparedSemanticSearch", "VectorRecallBackend"]
