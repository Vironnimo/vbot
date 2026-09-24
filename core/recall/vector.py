"""Vector Recall backend over a sqlite-vec Passage index.

Search builds source-derived Passages, applies scope/Session/time filters inside
KNN, and returns pure semantic top-K Passages without Session deduplication,
a universal distance cutoff, or literal fallback. One disposable store pins the
embedding-space fingerprint, index policy, and dimension; incompatible changes
trigger a complete rebuild. Within one space, a changed Session embeds only the
Passage texts that have no stored vector yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar, cast

from core.model_tasks import (
    EmbeddingError,
    EmbeddingPurpose,
    EmbeddingResult,
    EmbeddingService,
    EmbeddingUsage,
)
from core.models.models import ModelRegistry
from core.recall.canonical import (
    CanonicalSessionRecallBackend,
)
from core.recall.passages import (
    PASSAGE_OVERLAP_CHARS,
    PASSAGE_POLICY_VERSION,
    PASSAGE_TARGET_CHARS,
    build_session_passages,
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
    RefreshPlan,
    SessionPassages,
    SessionVersion,
    StoredPassage,
    VectorHeader,
    VectorStore,
    VectorStoreError,
)
from core.sessions import SessionAddress, SessionNotFoundError

_T = TypeVar("_T")

# Maximum number of texts embedded in one provider call. The provider
# contract has no hard limit, but splitting keeps the per-request
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
# Sentinel stored for the identity/global scope (``project_id is None``) in the
# Passage key. An empty string keeps per-scope keys reliable — SQLite treats
# NULLs as distinct, which would break per-scope uniqueness.
_GLOBAL_PROJECT_SCOPE = ""
_INDEX_POLICY = (
    f"passage-v{PASSAGE_POLICY_VERSION}:target={PASSAGE_TARGET_CHARS}:"
    f"overlap={PASSAGE_OVERLAP_CHARS}"
)
# Failures of the disposable index itself; the file is discarded and rebuilt once.
_STORE_FAILURES = (VectorStoreError, OSError, sqlite3.Error)


def _project_scope(project_id: str | None) -> str:
    """Map a recall project scope to the vector store's stored scope value.

    ``None`` (identity/global recall) maps to the ``_GLOBAL_PROJECT_SCOPE``
    sentinel so the on-disk rows for the global scope never share a key
    with a project's same-UUID session.
    """

    return project_id if project_id is not None else _GLOBAL_PROJECT_SCOPE


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
class _ScopeVersions:
    """Live Session versions of one Recall scope, read in one Session-store query."""

    live_session_ids: frozenset[str]
    candidates: dict[str, SessionVersion]
    source_fingerprint: str


@dataclass(frozen=True)
class PreparedSemanticSearch:
    """One semantic search whose index is fresh and whose query is embedded.

    ``page`` ranks at any depth without repeating freshness or query embedding.
    ``header`` is ``None`` when the request selects no candidate Session.
    """

    backend: VectorRecallBackend
    request: RecallSearchRequest
    snapshot_id: str
    candidate_sessions: int
    header: VectorHeader | None = None
    query_vector: tuple[float, ...] = ()

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
        self._index_lock = asyncio.Lock()

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

    async def prepare_search(self, request: RecallSearchRequest) -> PreparedSemanticSearch:
        """Reconcile the request's candidates and embed its query once.

        Emits one embedding Usage summary for the whole operation.
        """

        usage = _EmbeddingOperationUsage()
        try:
            async with self._index_lock:
                return await self._semantic_operation(
                    lambda: self._prepare(request, usage),
                    recover=True,
                )
        finally:
            self._log_embedding_usage("typed_search", usage)

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict one session's Passage vectors from the store (delete-time cleanup).

        Active counterpart to the pruning in ``_plan_refresh``: session deletion
        calls it so a removed session leaves semantic search immediately.
        ``project_id`` maps through ``_project_scope`` to match the stored keys.
        """
        async with self._index_lock:
            await asyncio.to_thread(
                self.store.delete_session,
                agent_id,
                _project_scope(project_id),
                session_id,
            )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def _prepare(
        self,
        request: RecallSearchRequest,
        usage: _EmbeddingOperationUsage,
    ) -> PreparedSemanticSearch:
        binding_header = await asyncio.to_thread(self._resolve_header, self.store, _INDEX_POLICY)
        if binding_header is None:
            raise RecallSearchError(
                "semantic_unavailable",
                "Semantic search is unavailable because no embedding model is configured.",
            )
        scope = await asyncio.to_thread(self._read_scope, request)
        if binding_header.dimension > 0:
            # A pinned header already names the complete embedding space, so a
            # stale continuation fails before any embedding is paid for.
            self._check_snapshot(request, self._vector_snapshot(scope, binding_header))
        if not scope.candidates:
            snapshot_id = self._vector_snapshot(scope, binding_header)
            self._check_snapshot(request, snapshot_id)
            return PreparedSemanticSearch(self, request, snapshot_id, 0)

        # The query embedding observes the live dimension and actual model
        # first; every document vector of this search must match it.
        query_vector, header = await self._embed_query(
            binding_header,
            request.query,
            index_policy=_INDEX_POLICY,
            usage=usage,
        )
        snapshot_id = self._vector_snapshot(scope, header)
        self._check_snapshot(request, snapshot_id)
        await self._refresh_index(request, scope, header, usage=usage)
        return PreparedSemanticSearch(
            self,
            request,
            snapshot_id,
            len(scope.candidates),
            header,
            tuple(query_vector),
        )

    async def _search_prepared(
        self,
        prepared: PreparedSemanticSearch,
        *,
        offset: int,
        limit: int,
    ) -> RecallSearchPage:
        header = prepared.header
        if header is None:
            return RecallSearchPage(
                hits=(),
                result_type="passage",
                ranking="cosine_distance",
                snapshot_id=prepared.snapshot_id,
                has_more=False,
                total_candidate_sessions=0,
            )
        request = prepared.request

        async def nearest() -> list[tuple[StoredPassage, float]]:
            return await asyncio.to_thread(
                self.store.knn_search,
                header=header,
                query_vector=prepared.query_vector,
                limit=offset + limit + 1,
                agent_id=request.agent_id,
                project_id=_project_scope(request.project_id),
                session_id=request.session_id,
                excluded_session_ids=request.excluded_session_ids,
                since=request.since,
                until=request.until,
            )

        async with self._index_lock:
            matches = await self._semantic_operation(nearest, recover=False)
        ranked = [
            RecallSearchHit(
                result_type="passage",
                session_id=passage.session_id,
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
            for passage, distance in matches
        ]
        page_hits = ranked[offset : offset + limit]
        return RecallSearchPage(
            hits=tuple(page_hits),
            result_type="passage",
            ranking="cosine_distance",
            snapshot_id=prepared.snapshot_id,
            has_more=len(ranked) > offset + len(page_hits),
            total_candidate_sessions=prepared.candidate_sessions,
        )

    async def _semantic_operation(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        recover: bool,
    ) -> _T:
        """Run one index operation; map index/provider failures to ``semantic_unavailable``.

        With ``recover`` a failed disposable store is discarded and the
        operation retried once.
        """

        try:
            if recover:
                try:
                    return await operation()
                except _STORE_FAILURES as error:
                    self._warning("Vector recall index failed; rebuilding once: %s", error)
                    await asyncio.to_thread(self.store.reset_index)
            return await operation()
        except (*_STORE_FAILURES, EmbeddingError) as error:
            self._warning("Vector recall failed: %s", error)
            raise RecallSearchError(
                "semantic_unavailable",
                "Semantic search failed; retry or check the embedding provider.",
            ) from error

    def _read_scope(self, request: RecallSearchRequest) -> _ScopeVersions:
        """Read every live Session version of the scope in one Session-store query."""

        live = {
            address.session_id: (generation_id, history_revision)
            for address, generation_id, history_revision in self.sessions.list_history_revisions(
                request.agent_id, request.project_id
            )
        }
        excluded = set(request.excluded_session_ids)
        candidates = {
            session_id: version
            for session_id, version in live.items()
            if session_id not in excluded
            and (request.session_id is None or session_id == request.session_id)
        }
        return _ScopeVersions(
            live_session_ids=frozenset(live),
            candidates=candidates,
            source_fingerprint=self._selection_snapshot(request, candidates),
        )

    @staticmethod
    def _vector_snapshot(scope: _ScopeVersions, header: VectorHeader) -> str:
        payload = (
            f"{scope.source_fingerprint}\0{header.provider_id}\0{header.model_id}\0"
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

    def _resolve_header(
        self,
        store: VectorStore,
        index_policy: str,
    ) -> VectorHeader | None:
        """Resolve the binding identity; ``None`` means no usable binding."""

        if self.embeddings is None:
            return None
        try:
            identity = self.embeddings.resolve_space()
        except EmbeddingError as error:
            self._warning("Vector recall binding lookup failed: %s", error)
            return None

        expected = VectorHeader(
            provider_id=identity.provider_id,
            model_id=identity.model_id,
            dimension=0,
            space_fingerprint=identity.fingerprint,
            index_policy=index_policy,
            response_model_id="",
        )
        stored = store.read_header()
        if stored is not None and not self._headers_match(
            stored,
            expected,
            include_response_model=False,
        ):
            self._warning(
                "Vector recall embedding space or index policy changed "
                "(%s/%s → %s/%s); rebuilding index",
                stored.provider_id,
                stored.model_id,
                identity.provider_id,
                identity.model_id,
            )
            store.reset_index()
            stored = None
        if stored is not None:
            return stored
        return expected

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
    # Freshness
    # ------------------------------------------------------------------

    async def _refresh_index(
        self,
        request: RecallSearchRequest,
        scope: _ScopeVersions,
        header: VectorHeader,
        *,
        usage: _EmbeddingOperationUsage,
    ) -> None:
        """Bring the request's candidate Sessions up to date in *header*'s space.

        Deleted Sessions are pruned against the complete scope. Sessions the
        request excludes cannot appear in its KNN results and are reconciled
        by a later search that includes them.
        """

        stored = await asyncio.to_thread(self.store.read_header)
        if stored is not None and not self._headers_match(stored, header, include_dimension=True):
            if stored.response_model_id != header.response_model_id:
                self._warning(
                    "Embedding response model changed (%s → %s); rebuilding vector index",
                    stored.response_model_id,
                    header.response_model_id,
                )
            else:
                self._warning(
                    "Embedding dimension changed (%d → %d); rebuilding vector index",
                    stored.dimension,
                    header.dimension,
                )
            await asyncio.to_thread(self.store.reset_index)
        plan = await asyncio.to_thread(self._plan_refresh, request, scope, header)
        vectors: list[list[float]] = []
        if plan.texts_to_embed:
            vectors = await self._embed_documents(list(plan.texts_to_embed), header, usage=usage)
        await asyncio.to_thread(self.store.apply_refresh, plan, vectors)

    def _plan_refresh(
        self,
        request: RecallSearchRequest,
        scope: _ScopeVersions,
        header: VectorHeader,
    ) -> RefreshPlan:
        """Reread changed candidate Sessions and diff their Passages off the event loop."""

        agent_id = request.agent_id
        project_scope = _project_scope(request.project_id)
        indexed = self.store.list_indexed_sessions(agent_id, project_scope)
        pruned = {session_id for session_id in indexed if session_id not in scope.live_session_ids}
        targets: list[SessionPassages] = []
        for session_id, version in sorted(scope.candidates.items()):
            previous = indexed.get(session_id)
            if previous == version:
                continue
            address = SessionAddress(
                project_id=request.project_id, agent_id=agent_id, session_id=session_id
            )
            try:
                # One read transaction returns the active history together with
                # the exact version it belongs to.
                batch = self.sessions.get(address).load_since(None)
            except SessionNotFoundError:
                # Vanished since listing: its rows describe no live Session.
                if previous is not None:
                    pruned.add(session_id)
                continue
            if batch is None:  # Only a stale cursor yields no batch; a full read has none.
                continue
            targets.append(
                SessionPassages(
                    session_id=session_id,
                    version=(batch.cursor.generation_id, batch.cursor.history_revision),
                    previous_version=previous,
                    passages=tuple(build_session_passages(batch.active_messages)),
                )
            )
        return self.store.plan_refresh(
            agent_id,
            project_scope,
            header=header,
            sessions=targets,
            pruned_session_ids=pruned,
        )

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
