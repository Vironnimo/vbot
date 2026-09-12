"""Vector Recall backend over a sqlite-vec Passage index.

Search builds source-derived Passages, applies scope/Session/time filters inside
KNN, and returns pure semantic top-K Passages without Session deduplication,
a universal distance cutoff, or literal fallback. One disposable store pins the
embedding-space fingerprint, index policy, and dimension; incompatible changes
trigger a complete rebuild.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any, cast

from core.model_tasks import (
    EmbeddingError,
    EmbeddingPurpose,
    EmbeddingResult,
    EmbeddingService,
    EmbeddingSpaceIdentity,
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
    Passage,
    build_session_passages,
)
from core.recall.recall import (
    JsonObject,
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.recall.vector_store import (
    ChunkVectorRecord,
    VectorHeader,
    VectorStore,
    VectorStoreError,
    format_started_at,
)
from core.sessions import SessionAddress, SessionNotFoundError

# Maximum number of texts embedded in one provider call. The provider
# contract has no hard limit, but splitting keeps the per-request
# payload predictable and the shrink-retry path bounded per batch.
_EMBED_BATCH_SIZE = 64
# Agent-facing query guidance for session_search when this backend is active.
# Static: it describes the capability, not the current availability — actual
# availability is surfaced per-call in the error result.
_SEMANTIC_SEARCH_GUIDANCE = (
    "Short topic description to find by meaning. Bare keywords anchor poorly and exact "
    "occurrences may be missed. Omit to list recent Sessions. Matches are ranked by semantic "
    "relevance."
)
_SEMANTIC_TOOL_SUMMARY = (
    "Find persisted Sessions and semantically related passages from past conversations."
)
# Sentinel stored for the identity/global scope (``project_id is None``) in the
# chunk-key tuple. An empty string keeps the store's UNIQUE constraint reliable —
# SQLite treats NULLs as distinct, which would break per-scope uniqueness.
_GLOBAL_PROJECT_SCOPE = ""
_INDEX_POLICY = (
    f"passage-v{PASSAGE_POLICY_VERSION}:target={PASSAGE_TARGET_CHARS}:"
    f"overlap={PASSAGE_OVERLAP_CHARS}"
)


def _project_scope(project_id: str | None) -> str:
    """Map a recall project scope to the vector store's stored scope value.

    ``None`` (identity/global recall) maps to the ``_GLOBAL_PROJECT_SCOPE``
    sentinel so the on-disk chunk rows for the global scope never share a key
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
        usage = _EmbeddingOperationUsage()
        try:
            return await self._search_page_with_usage(request, usage)
        finally:
            self._log_embedding_usage("typed_search", usage)

    async def _search_page_with_usage(
        self,
        request: RecallSearchRequest,
        usage: _EmbeddingOperationUsage,
    ) -> RecallSearchPage:
        summaries = await asyncio.to_thread(self._search_candidate_summaries, request)
        try:
            async with self._index_lock:
                for attempt in range(2):
                    try:
                        binding_header = await asyncio.to_thread(
                            self._resolve_header,
                            self.store,
                            _INDEX_POLICY,
                        )
                        if binding_header is None:
                            raise RecallSearchError(
                                "semantic_unavailable",
                                "Semantic search is unavailable because no embedding model "
                                "is configured.",
                            )
                        snapshot_id = self._vector_snapshot(request, summaries, binding_header)
                        if request.snapshot_id is not None and request.snapshot_id != snapshot_id:
                            raise RecallSearchError(
                                "stale_cursor",
                                "Session search source changed; repeat the search.",
                            )
                        if not summaries:
                            return RecallSearchPage(
                                hits=(),
                                result_type="passage",
                                ranking="cosine_distance",
                                snapshot_id=snapshot_id,
                                has_more=False,
                                total_candidate_sessions=0,
                            )

                        await self._ensure_fresh_index(
                            request,
                            binding_header,
                            store=self.store,
                            index_policy=_INDEX_POLICY,
                            usage=usage,
                        )
                        query_vector, query_header = await self._embed_query(
                            binding_header,
                            request.query,
                            index_policy=_INDEX_POLICY,
                            usage=usage,
                        )
                        await self._ensure_query_header(
                            request,
                            query_header,
                            store=self.store,
                            index_policy=_INDEX_POLICY,
                            usage=usage,
                        )
                        resolved_snapshot_id = self._vector_snapshot(
                            request,
                            summaries,
                            query_header,
                        )
                        if (
                            request.snapshot_id is not None
                            and request.snapshot_id != resolved_snapshot_id
                        ):
                            raise RecallSearchError(
                                "stale_cursor",
                                "Session search source changed; repeat the search.",
                            )
                        snapshot_id = resolved_snapshot_id
                        candidates = await asyncio.to_thread(
                            self.store.knn_search,
                            header=query_header,
                            query_vector=query_vector,
                            limit=request.offset + request.limit + 1,
                            agent_id=request.agent_id,
                            project_id=_project_scope(request.project_id),
                            session_id=request.session_id,
                            excluded_session_ids=request.excluded_session_ids,
                            since=request.since,
                            until=request.until,
                        )
                        records = await asyncio.to_thread(
                            self.store.get_chunks_by_rowids,
                            [rowid for rowid, _ in candidates],
                        )
                        break
                    except (VectorStoreError, OSError, sqlite3.Error) as error:
                        if attempt > 0:
                            raise
                        self._warning("Vector recall index failed; rebuilding once: %s", error)
                        await asyncio.to_thread(self.store.reset_index)
        except (VectorStoreError, EmbeddingError, OSError, sqlite3.Error) as error:
            self._warning("Vector recall failed: %s", error)
            raise RecallSearchError(
                "semantic_unavailable",
                "Semantic search failed; retry or check the embedding provider.",
            ) from error

        ranked: list[RecallSearchHit] = []
        for rowid, distance in candidates:
            record = records.get(rowid)
            if record is None:
                continue
            ranked.append(
                RecallSearchHit(
                    result_type="passage",
                    session_id=record.session_id,
                    message_id=record.start_message_id,
                    role=record.start_role,
                    timestamp=record.start_timestamp,
                    text=record.text,
                    score=distance,
                    passage_id=record.passage_id,
                    start_message_id=record.start_message_id,
                    end_message_id=record.end_message_id,
                    end_timestamp=record.end_timestamp,
                    sources=("semantic",),
                )
            )
        page_hits = ranked[request.offset : request.offset + request.limit]
        return RecallSearchPage(
            hits=tuple(page_hits),
            result_type="passage",
            ranking="cosine_distance",
            snapshot_id=snapshot_id,
            has_more=len(ranked) > request.offset + len(page_hits),
            total_candidate_sessions=len(summaries),
        )

    def _vector_snapshot(
        self,
        request: RecallSearchRequest,
        summaries: list[JsonObject],
        header: VectorHeader,
    ) -> str:
        source = self._search_snapshot(request, summaries)
        payload = (
            f"{source}\0{header.provider_id}\0{header.model_id}\0"
            f"{header.response_model_id}\0{header.space_fingerprint}\0{header.index_policy}"
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict one session's chunk vectors from the store (delete-time cleanup).

        Active counterpart to the on-search staleness drop in
        ``_ensure_fresh_index``: session deletion calls it so a removed session
        leaves semantic search immediately. ``project_id`` maps through
        ``_project_scope`` to match how chunks are keyed in the store.
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
            resolve_space = getattr(self.embeddings, "resolve_space", None)
            if callable(resolve_space):
                identity = cast(EmbeddingSpaceIdentity, resolve_space())
            else:
                provider_id, model_id = self.embeddings.resolve_model_id()
                identity = EmbeddingSpaceIdentity(
                    provider_id=provider_id,
                    model_id=model_id,
                    fingerprint="",
                )
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
        """Embed a single query string and pin the dimension from the first response."""

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

    async def _embed_chunks(
        self,
        texts: list[str],
        expected: VectorHeader,
        *,
        index_policy: str,
        usage: _EmbeddingOperationUsage,
    ) -> tuple[list[list[float]], VectorHeader]:
        """Embed a batch of chunk texts and return vectors with the resolved header."""

        result = await self._run_embed(texts, purpose="document")
        usage.add(result, purpose="document", input_count=len(texts))
        header = self._header_from_result(
            result,
            expected,
            index_policy=index_policy,
            allow_response_model_change=True,
        )
        return [list(vector) for vector in result.vectors], header

    async def _ensure_query_header(
        self,
        request: RecallSearchRequest,
        query_header: VectorHeader,
        *,
        store: VectorStore,
        index_policy: str,
        usage: _EmbeddingOperationUsage,
    ) -> None:
        """Ensure the live query vector is comparable with every stored vector."""

        stored = await asyncio.to_thread(store.read_header)
        if stored is None:
            await asyncio.to_thread(store.ensure_index, query_header)
            return
        if self._headers_match(stored, query_header, include_dimension=True):
            return
        if not self._headers_match(
            stored,
            query_header,
            include_response_model=False,
        ):
            raise EmbeddingError("embedding space changed while the Recall request was running")

        if stored.response_model_id != query_header.response_model_id:
            self._warning(
                "Embedding response model changed (%s → %s); rebuilding vector index",
                stored.response_model_id,
                query_header.response_model_id,
            )
        else:
            self._warning(
                "Embedding dimension changed (%d → %d); rebuilding vector index",
                stored.dimension,
                query_header.dimension,
            )
        await asyncio.to_thread(store.reset_index)
        await self._ensure_fresh_index(
            request,
            query_header,
            store=store,
            index_policy=index_policy,
            usage=usage,
        )
        rebuilt = await asyncio.to_thread(store.read_header)
        if rebuilt is None:
            await asyncio.to_thread(store.ensure_index, query_header)
        elif not self._headers_match(rebuilt, query_header, include_dimension=True):
            raise VectorStoreError("rebuilt vector store header does not match the live query")

    async def _run_embed(
        self,
        texts: list[str],
        *,
        purpose: EmbeddingPurpose = "document",
    ) -> EmbeddingResult:
        """Embed *texts*, batching into ``_EMBED_BATCH_SIZE`` groups.

        A context overflow on a multi-input call recursively divides that call
        until each accepted provider request fits. A single rejected text is
        never modified here: chunk/Passage construction owns any truncation so
        the text stored beside a vector remains byte-for-byte honest. Results
        are concatenated in input order and every configured/actual model,
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
    # Freshness + backfill
    # ------------------------------------------------------------------

    async def _ensure_fresh_index(
        self,
        request: RecallSearchRequest,
        header: VectorHeader,
        *,
        store: VectorStore,
        index_policy: str,
        usage: _EmbeddingOperationUsage,
        allow_header_rebuild: bool = True,
    ) -> None:
        """Make sure every canonical session in this scope has fresh chunk vectors."""

        agent_id = request.agent_id
        scope = _project_scope(request.project_id)
        summaries = await asyncio.to_thread(
            self.sessions.list_summaries,
            request.agent_id,
            request.project_id,
        )
        active = {str(summary["id"]): summary for summary in summaries}
        indexed = await asyncio.to_thread(store.list_indexed_sessions, agent_id, scope)

        # Drop canonical Sessions that have been removed since last index.
        stale_to_remove = sorted(set(indexed) - set(active))
        if stale_to_remove:
            await asyncio.to_thread(
                store.drop_indexed_sessions,
                agent_id,
                scope,
                stale_to_remove,
            )

        # Collect every Session whose canonical message history revision changed.
        all_chunks = await asyncio.to_thread(
            self._collect_stale_chunks,
            request,
            active,
            indexed,
            store,
        )
        if not all_chunks:
            if header.dimension > 0:
                await asyncio.to_thread(store.ensure_index, header)
            return

        texts = [chunk.text for _, _, _, chunk in all_chunks]
        vectors, resolved_header = await self._embed_chunks(
            texts,
            header,
            index_policy=index_policy,
            usage=usage,
        )
        if resolved_header.dimension <= 0:
            raise VectorStoreError("embedding provider returned no vectors")
        if header.dimension > 0 and not self._headers_match(
            header,
            resolved_header,
            include_dimension=True,
        ):
            if not allow_header_rebuild:
                raise EmbeddingError("embedding header changed repeatedly during index rebuild")
            if header.response_model_id != resolved_header.response_model_id:
                self._warning(
                    "Embedding response model changed during backfill (%s → %s); "
                    "rebuilding full index",
                    header.response_model_id,
                    resolved_header.response_model_id,
                )
            else:
                self._warning(
                    "Embedding dimension drift (%d → %d); rebuilding full index",
                    header.dimension,
                    resolved_header.dimension,
                )
            await asyncio.to_thread(store.reset_index)
            await self._ensure_fresh_index(
                request,
                resolved_header,
                store=store,
                index_policy=index_policy,
                usage=usage,
                allow_header_rebuild=False,
            )
            return

        # Per-session running counter: chunk_index must be unique within
        # ``(project_id, agent_id, session_id)`` and start at 0 — the store's
        # ``UNIQUE(project_id, agent_id, session_id, chunk_index)`` constraint
        # will reject duplicates, so a stable, ordered counter is required.
        records: list[tuple[ChunkVectorRecord, list[float]]] = []
        per_session_index: dict[str, int] = {}
        for (summary, generation_id, history_revision, chunk), vector in zip(
            all_chunks, vectors, strict=True
        ):
            session_id = str(summary["id"])
            index = per_session_index.get(session_id, 0)
            per_session_index[session_id] = index + 1
            records.append(
                (
                    ChunkVectorRecord(
                        session_id=session_id,
                        agent_id=agent_id,
                        project_id=scope,
                        started_at=format_started_at(summary.get("created_at")),
                        generation_id=generation_id,
                        history_revision=history_revision,
                        anchor_message_id=chunk.start_message_id,
                        snippet=chunk.text,
                        chunk_index=index,
                        start_message_id=chunk.start_message_id,
                        end_message_id=chunk.end_message_id,
                        passage_id=chunk.passage_id,
                        text=chunk.text,
                        start_timestamp=chunk.start_timestamp,
                        end_timestamp=chunk.end_timestamp,
                        start_role=chunk.start_role,
                        end_role=chunk.end_role,
                    ),
                    vector,
                )
            )

        await asyncio.to_thread(
            store.upsert_many_chunks,
            header=resolved_header,
            records=records,
        )

    def _collect_stale_chunks(
        self,
        request: RecallSearchRequest,
        active: dict[str, JsonObject],
        indexed: dict[str, tuple[str, int]],
        store: VectorStore,
    ) -> list[tuple[JsonObject, str, int, Passage]]:
        """Load changed Sessions and pack their chunks off the event loop."""

        agent_id = request.agent_id
        scope = _project_scope(request.project_id)
        stale_sessions: list[tuple[JsonObject, str, int, list[Any]]] = []
        # One batched canonical-freshness query instead of one per Session.
        addresses = [
            SessionAddress(project_id=request.project_id, agent_id=agent_id, session_id=session_id)
            for session_id in active
        ]
        versions = self.sessions.list_history_versions(addresses)
        for session_id, summary in active.items():
            address = SessionAddress(
                project_id=request.project_id, agent_id=agent_id, session_id=session_id
            )
            version = versions.get(address)
            if version is None:
                # The Session vanished between listing and indexing; treat as
                # unchanged so the stale-row cleanup handles it.
                continue
            generation_id, history_revision = version
            cached = indexed.get(session_id)
            if cached is not None and cached == (generation_id, history_revision):
                continue
            try:
                messages = self.sessions.get(address).load_active()
            except SessionNotFoundError:
                continue
            stale_sessions.append((summary, generation_id, history_revision, messages))

        # A session that yields zero indexable chunks is not covered by
        # ``upsert_many_chunks``. Clear its old rows explicitly.
        all_chunks: list[tuple[JsonObject, str, int, Passage]] = []
        for summary, generation_id, history_revision, messages in stale_sessions:
            chunks = build_session_passages(messages)
            if not chunks:
                store.delete_session(agent_id, scope, str(summary["id"]))
                continue
            all_chunks.extend((summary, generation_id, history_revision, chunk) for chunk in chunks)
        return all_chunks

    # ------------------------------------------------------------------
    # Hydration
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


__all__ = ["VectorRecallBackend"]
