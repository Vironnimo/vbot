"""Vector Recall backend over a sqlite-vec Passage index.

The typed first-party search contract builds source-derived Passages, applies
scope/Session/time metadata filters inside KNN, and returns pure semantic top-K
Passages without Session deduplication, a universal distance cutoff, or literal
fallback. The older ``RecallBackend.search`` entry point retains its chunk-based
payload and degraded JSONL behavior solely for compatibility with legacy callers.

Separate disposable stores pin the full embedding-space fingerprint,
index policy, and dimension in their headers; any incompatible change drops
and lazily rebuilds only the affected index.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Iterable
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
from core.recall.jsonl import (
    JsonlSessionRecallBackend,
    compact_text,
    is_context_message,
    is_recall_artifact_message,
    message_index_by_id,
    message_match_payload,
    message_matches_request,
    message_search_text,
    request_payload,
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
    RecallRequest,
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
from core.sessions import SessionAddress

# Target size of an embedded chunk in characters. ~500 tokens for the
# common English case; small chunks give the KNN finer-grained matches
# (the user's "fruit" mention is its own chunk, not buried in a 5k-char
# session blob).
_CHUNK_TARGET_CHARS = 1500
# How many trailing messages of the previous chunk to prepend into the
# next chunk. Carries a sliver of boundary context so a message that
# straddles the chunk boundary still has nearby signal.
_CHUNK_OVERLAP_MESSAGES = 1
# Per-message character cap before packing into a chunk. A single
# pathological user message longer than ``_CHUNK_TARGET_CHARS`` is
# truncated before packing so it does not blow out the chunk budget.
_PER_MESSAGE_CHAR_CAP = 2000
# Maximum number of texts embedded in one provider call. The provider
# contract has no hard limit, but splitting keeps the per-request
# payload predictable and the shrink-retry path bounded per batch.
_EMBED_BATCH_SIZE = 64
# Legacy-result cosine-distance cutoff. Typed Passage search intentionally has
# no universal threshold because distance calibration is model-specific.
_MAX_DISTANCE = 0.7
# Over-fetch multiplier for KNN before chunk→session dedup. The
# recall backend requests ``limit * multiplier + KNN margin`` chunks so
# the per-session nearest-chunk selection still leaves ``limit``
# distinct sessions after the cutoff and structural filters.
_CHUNK_FETCH_MULTIPLIER = 8
# Margin to over-fetch from KNN so structural filters still leave ``limit`` hits.
_KNN_FETCH_MARGIN = 4
# Agent-facing query guidance for session_search when this backend is active.
# Static: it describes the capability, not the current availability — actual
# availability is surfaced per-call in the result (the degradation notices below).
_SEMANTIC_SEARCH_GUIDANCE = (
    "Short topic description to find by meaning. Bare keywords anchor poorly and exact "
    "occurrences may be missed. Omit to list recent Sessions. Matches are ranked by semantic "
    "relevance."
)
_SEMANTIC_TOOL_SUMMARY = (
    "Find persisted Sessions and semantically related passages from past conversations."
)
# Notice attached to a degraded result when semantic search could not run. The
# config case is actionable (configure a model); the transient case is operational.
# Prepended to ``content`` (the model-facing tool output) and also exposed as a
# structured ``notice`` field so a composing backend (hybrid) can re-surface it.
_SEMANTIC_UNAVAILABLE_NOTICE = (
    "Semantic search unavailable (no embedding model configured); showing literal "
    "keyword matches. Configure a text_embedding model in Settings to enable it."
)
_SEMANTIC_FAILED_NOTICE = (
    "Semantic search failed; showing literal keyword matches instead. Results may "
    "miss meaning-related sessions — retry or check the embedding provider."
)
# Sentinel stored for the identity/global scope (``project_id is None``) in the
# chunk-key tuple. An empty string keeps the store's UNIQUE constraint reliable —
# SQLite treats NULLs as distinct, which would break per-scope uniqueness.
_GLOBAL_PROJECT_SCOPE = ""
_TYPED_INDEX_POLICY = (
    f"passage-v{PASSAGE_POLICY_VERSION}:target={PASSAGE_TARGET_CHARS}:"
    f"overlap={PASSAGE_OVERLAP_CHARS}"
)
_LEGACY_INDEX_POLICY_VERSION = 1
_LEGACY_INDEX_POLICY = (
    f"legacy-chunk-v{_LEGACY_INDEX_POLICY_VERSION}:target={_CHUNK_TARGET_CHARS}:"
    f"overlap_messages={_CHUNK_OVERLAP_MESSAGES}:message_cap={_PER_MESSAGE_CHAR_CAP}"
)
_TYPED_INDEX_FILE_NAME = "session_passage_vectors.sqlite"


def _project_scope(project_id: str | None) -> str:
    """Map a recall project scope to the vector store's stored scope value.

    ``None`` (identity/global recall) maps to the ``_GLOBAL_PROJECT_SCOPE``
    sentinel so the on-disk chunk rows for the global scope never share a key
    with a project's same-UUID session.
    """

    return project_id if project_id is not None else _GLOBAL_PROJECT_SCOPE


@dataclass(frozen=True)
class Chunk:
    """One packed, embeddable window of a session's messages.

    ``anchor_message_id`` is the message the chunk is centered on for
    result hydration — by default the first non-skill-context, non-note
    message in the chunk, falling back to the chunk's first message.
    ``start_message_id`` / ``end_message_id`` bound the chunk's message
    span and ``text`` is the concatenated, capped, joined message
    search-text that gets embedded. ``snippet`` is the compact
    headline rendered to the user when this chunk wins the KNN.
    """

    anchor_message_id: str
    start_message_id: str
    end_message_id: str
    text: str
    snippet: str
    passage_id: str = ""
    start_timestamp: str = ""
    end_timestamp: str = ""
    start_role: str = ""
    end_role: str = ""


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


class VectorRecallBackend(JsonlSessionRecallBackend):
    """Recall backend backed by sqlite-vec per-chunk vectors."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        # Keep the established store/file as the legacy Search index; the typed
        # Passage contract gets a physically separate index so neither policy
        # can ever reuse the other's rows.
        self.store = VectorStore(context.data_dir)
        self._typed_store = VectorStore(
            context.data_dir,
            index_file_name=_TYPED_INDEX_FILE_NAME,
        )
        self.logger = context.logger
        self.embeddings: EmbeddingService | None = context.embeddings
        self.model_registry: ModelRegistry | None = context.model_registry
        self._fallback = JsonlSessionRecallBackend(context.sessions)
        # Cached resolved binding for the lifetime of the index — the store
        # itself drops+rebuilds on a binding change, so the cache is always
        # in sync with the on-disk header after the first successful embed.
        self._resolved_headers: dict[str, VectorHeader] = {}
        self._index_lock = asyncio.Lock()

    def describe_search(self) -> str:
        return _SEMANTIC_SEARCH_GUIDANCE

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
                            self._typed_store,
                            _TYPED_INDEX_POLICY,
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
                            store=self._typed_store,
                            index_policy=_TYPED_INDEX_POLICY,
                            usage=usage,
                        )
                        query_vector, query_header = await self._embed_query(
                            binding_header,
                            request.query,
                            index_policy=_TYPED_INDEX_POLICY,
                            usage=usage,
                        )
                        await self._ensure_query_header(
                            request,
                            query_header,
                            store=self._typed_store,
                            index_policy=_TYPED_INDEX_POLICY,
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
                            self._typed_store.knn_search,
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
                            self._typed_store.get_chunks_by_rowids,
                            [rowid for rowid, _ in candidates],
                        )
                        break
                    except (VectorStoreError, OSError, sqlite3.Error) as error:
                        if attempt > 0:
                            raise
                        self._warning("Vector recall index failed; rebuilding once: %s", error)
                        await asyncio.to_thread(self._typed_store.reset_index)
                        self._resolved_headers.pop(_TYPED_INDEX_POLICY, None)
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
            for store in (self.store, self._typed_store):
                await asyncio.to_thread(
                    store.delete_session,
                    agent_id,
                    _project_scope(project_id),
                    session_id,
                )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(self, request: RecallRequest) -> JsonObject:
        summaries = await asyncio.to_thread(self.candidate_session_summaries, request)
        if request.query is None:
            return self.session_summary_result(request, summaries)
        if not request.query.strip():
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)
        if not summaries:
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)

        usage = _EmbeddingOperationUsage()
        try:
            return await self._search_with_vector_store(request, summaries, usage)
        except (VectorStoreError, EmbeddingError, OSError, sqlite3.Error) as error:
            self._warning("Vector recall failed; falling back to JSONL scan: %s", error)
            fallback = await self._fallback.search(request)
            return self._degraded_result(fallback, _SEMANTIC_FAILED_NOTICE)
        finally:
            self._log_embedding_usage("legacy_search", usage)

    async def _search_with_vector_store(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
        usage: _EmbeddingOperationUsage,
    ) -> JsonObject:
        async with self._index_lock:
            for attempt in range(2):
                try:
                    binding_header = await asyncio.to_thread(
                        self._resolve_header,
                        self.store,
                        _LEGACY_INDEX_POLICY,
                    )
                    if binding_header is None:
                        self._warning(
                            "Vector recall has no embedding binding; falling back to JSONL scan"
                        )
                        fallback = await self._fallback.search(request)
                        return self._degraded_result(fallback, _SEMANTIC_UNAVAILABLE_NOTICE)

                    await self._ensure_fresh_index(
                        request,
                        binding_header,
                        store=self.store,
                        index_policy=_LEGACY_INDEX_POLICY,
                        usage=usage,
                    )

                    # ``search()`` has already rejected ``None``/blank queries
                    # before calling us, so the cast only narrows for typing.
                    query = cast(str, request.query)
                    query_vector, query_header = await self._embed_query(
                        binding_header,
                        query,
                        index_policy=_LEGACY_INDEX_POLICY,
                        usage=usage,
                    )
                    await self._ensure_query_header(
                        request,
                        query_header,
                        store=self.store,
                        index_policy=_LEGACY_INDEX_POLICY,
                        usage=usage,
                    )
                    candidates, rowid_to_record = await asyncio.to_thread(
                        self._query_store,
                        self.store,
                        query_header,
                        query_vector,
                        request.limit * _CHUNK_FETCH_MULTIPLIER + _KNN_FETCH_MARGIN,
                    )
                    break
                except (VectorStoreError, OSError, sqlite3.Error) as error:
                    if attempt > 0:
                        raise
                    self._warning("Legacy vector index failed; rebuilding once: %s", error)
                    await asyncio.to_thread(self.store.reset_index)
                    self._resolved_headers.pop(_LEGACY_INDEX_POLICY, None)
        if not candidates:
            return self._message_result(
                request,
                [],
                searched_sessions=len(summaries),
                total_candidates=len(summaries),
            )

        return await asyncio.to_thread(
            self._hydrate_vector_result,
            request,
            summaries,
            candidates,
            rowid_to_record,
        )

    def _query_store(
        self,
        store: VectorStore,
        header: VectorHeader,
        query_vector: list[float],
        limit: int,
    ) -> tuple[list[tuple[int, float]], dict[int, ChunkVectorRecord]]:
        candidates = store.knn_search(
            header=header,
            query_vector=query_vector,
            limit=limit,
        )
        records = store.get_chunks_by_rowids([rowid for rowid, _ in candidates])
        return candidates, records

    def _hydrate_vector_result(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
        candidates: list[tuple[int, float]],
        rowid_to_record: dict[int, ChunkVectorRecord],
    ) -> JsonObject:
        # Walk candidates in distance order; keep the first (nearest) chunk
        # seen for each session so a single session cannot dominate the
        # results with several of its own chunks. Then drop everything
        # past the relevance cutoff and hydrate the survivors.
        # KNN spans the whole vec0 table (all scopes/agents). Keep only chunks
        # whose ``(project_id, agent_id)`` match this request's scope, so a
        # same-UUID session in another scope never collides with this scope's
        # summaries. The store is keyed by ``(project_id, agent_id, session_id)``.
        request_scope = _project_scope(request.project_id)
        nearest_by_session: dict[str, tuple[ChunkVectorRecord, float]] = {}
        for rowid, distance in candidates:
            if distance > _MAX_DISTANCE:
                continue
            record = rowid_to_record.get(rowid)
            if record is None:
                continue
            if record.agent_id != request.agent_id or record.project_id != request_scope:
                continue
            session_id = record.session_id
            if session_id in nearest_by_session:
                continue
            nearest_by_session[session_id] = (record, distance)

        matches: list[JsonObject] = []
        for session_id, (record, distance) in nearest_by_session.items():
            summary = self._summary_by_session_id(summaries, session_id)
            if summary is None:
                continue
            session_match = self._hydrate_chunk(request, summary, record, distance)
            if session_match is None:
                continue
            matches.append(session_match)
            if len(matches) >= request.limit:
                break

        return self._message_result(
            request,
            matches,
            searched_sessions=len(summaries),
            total_candidates=len(summaries),
            truncated=len(nearest_by_session) > request.limit and len(matches) >= request.limit,
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
            self._resolved_headers.pop(index_policy, None)
            stored = None
        if stored is not None:
            self._resolved_headers[index_policy] = stored
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
        self._resolved_headers[index_policy] = resolved
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
        self._resolved_headers[index_policy] = header
        return [list(vector) for vector in result.vectors], header

    async def _ensure_query_header(
        self,
        request: RecallRequest | RecallSearchRequest,
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
        self._resolved_headers[index_policy] = query_header
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
                    f"embedding dimension changed mid-batch: {first.dimension} → {result.dimension}"
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
        request: RecallRequest | RecallSearchRequest,
        header: VectorHeader,
        *,
        store: VectorStore,
        index_policy: str,
        usage: _EmbeddingOperationUsage,
        allow_header_rebuild: bool = True,
    ) -> None:
        """Make sure every JSONL session in this scope has fresh chunk vectors."""

        agent_id = request.agent_id
        scope = _project_scope(request.project_id)
        summaries = await asyncio.to_thread(
            self.sessions.list_with_metadata,
            request.agent_id,
            request.project_id,
        )
        active = {str(summary["id"]): summary for summary in summaries}
        indexed = await asyncio.to_thread(store.list_indexed_sessions, agent_id, scope)

        # Drop JSONL sessions that have been removed since last index.
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

        texts = [chunk.text for _, _, chunk in all_chunks]
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
                    "Embedding dimension changed during backfill (%d → %d); rebuilding full index",
                    header.dimension,
                    resolved_header.dimension,
                )
            await asyncio.to_thread(store.reset_index)
            self._resolved_headers[index_policy] = resolved_header
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
        for (summary, history_revision, chunk), vector in zip(all_chunks, vectors, strict=True):
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
                        history_revision=history_revision,
                        anchor_message_id=chunk.anchor_message_id,
                        snippet=chunk.snippet,
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
        self._resolved_headers[index_policy] = resolved_header

    def _collect_stale_chunks(
        self,
        request: RecallRequest | RecallSearchRequest,
        active: dict[str, JsonObject],
        indexed: dict[str, int],
        store: VectorStore,
    ) -> list[tuple[JsonObject, int, Chunk]]:
        """Load changed Sessions and pack their chunks off the event loop."""

        agent_id = request.agent_id
        scope = _project_scope(request.project_id)
        stale_sessions: list[tuple[JsonObject, int, list[Any]]] = []
        for session_id, summary in active.items():
            session = self.sessions.get(
                SessionAddress(
                    project_id=request.project_id, agent_id=agent_id, session_id=session_id
                )
            )
            history_revision = self.sessions.history_revision(
                SessionAddress(
                    project_id=request.project_id, agent_id=agent_id, session_id=session_id
                )
            )
            cached = indexed.get(session_id)
            if cached is not None and cached == history_revision:
                continue
            stale_sessions.append((summary, history_revision, session.load_active()))

        # A session that yields zero indexable chunks is not covered by
        # ``upsert_many_chunks``. Clear its old rows explicitly.
        all_chunks: list[tuple[JsonObject, int, Chunk]] = []
        for summary, history_revision, messages in stale_sessions:
            if isinstance(request, RecallSearchRequest):
                chunks = [
                    _chunk_from_passage(passage) for passage in build_session_passages(messages)
                ]
            else:
                chunks = build_session_chunks(messages)
            if not chunks:
                store.delete_session(agent_id, scope, str(summary["id"]))
                continue
            all_chunks.extend((summary, history_revision, chunk) for chunk in chunks)
        return all_chunks

    # ------------------------------------------------------------------
    # Hydration
    # ------------------------------------------------------------------

    def _summary_by_session_id(
        self,
        summaries: list[JsonObject],
        session_id: str,
    ) -> JsonObject | None:
        for summary in summaries:
            if str(summary.get("id")) == session_id:
                return summary
        return None

    def _hydrate_chunk(
        self,
        request: RecallRequest,
        summary: JsonObject,
        record: ChunkVectorRecord,
        distance: float,
    ) -> JsonObject | None:
        """Hydrate a per-chunk result anchored at a request-eligible message."""

        messages = self.sessions.get(
            SessionAddress(
                project_id=request.project_id,
                agent_id=request.agent_id,
                session_id=record.session_id,
            )
        ).load_active()
        if not messages:
            return None
        anchor_index = self._resolve_request_anchor(messages, record, request)
        if anchor_index is None:
            return None
        anchor_message = messages[anchor_index]
        text = message_search_text(anchor_message)
        match = message_match_payload(
            request,
            summary,
            messages,
            anchor_index,
            text,
        )
        match["distance"] = distance
        # The snippet stays the anchor message's own search-text snippet from
        # ``message_match_payload``. The chunk's stored ``record.snippet`` is the
        # whole chunk's headline, which mixes in roles the caller did not ask for
        # (a default search excludes ``tool``, but a chunk embeds every role) and
        # would surface raw tool JSON as the result text. Anchoring already moved
        # the result onto a request-eligible message, so its text is the honest,
        # in-scope snippet to show.
        match["chunk_index"] = record.chunk_index
        return match

    @staticmethod
    def _resolve_request_anchor(
        messages: list[Any],
        record: ChunkVectorRecord,
        request: RecallRequest,
    ) -> int | None:
        """Pick a chunk anchor that satisfies the request's structural filters.

        Prefer the chunk's recorded anchor. If it is filtered out — a role the
        caller did not ask for (e.g. ``run_summary``, never a recall role), a
        skill-context note, or a message outside the time window — re-anchor to
        the first message inside the chunk's ``[start, end]`` span that does
        match. Returns ``None`` when no message in the span is eligible, so the
        whole chunk is dropped rather than surfacing a non-requested role.
        """

        anchor_index = message_index_by_id(messages, record.anchor_message_id)
        if anchor_index is not None and message_matches_request(messages[anchor_index], request):
            return anchor_index
        start = message_index_by_id(messages, record.start_message_id)
        end = message_index_by_id(messages, record.end_message_id)
        if start is None:
            start = 0
        if end is None or end < start:
            end = len(messages) - 1
        for index in range(start, end + 1):
            if message_matches_request(messages[index], request):
                return index
        return None

    @staticmethod
    def _message_result(
        request: RecallRequest,
        matches: list[JsonObject],
        *,
        searched_sessions: int,
        total_candidates: int,
        truncated: bool = False,
    ) -> JsonObject:
        return {
            "content": render_vector_matches(request, matches, truncated=truncated),
            "matches": matches,
            "truncated": truncated,
            "searched_sessions": searched_sessions,
            "total_candidate_sessions": total_candidates,
            "request": request_payload(request),
        }

    @staticmethod
    def _degraded_result(base_result: JsonObject, notice: str) -> JsonObject:
        """Wrap a JSONL fallback result with a notice that semantic search did not run.

        The notice is prepended to ``content`` (the model-facing tool output) so the
        agent knows the results are literal-only, and exposed as a structured
        ``notice`` field so a composing backend (hybrid) can detect the degradation
        and re-surface it.
        """

        content = base_result.get("content", "")
        decorated = f"{notice}\n\n{content}" if content else notice
        return {**base_result, "content": decorated, "notice": notice}

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
# Free functions (chunking policy + module-level helpers)
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


def _is_skippable_for_anchor(message: Any) -> bool:
    """True when a message is a poor chunk anchor — not user-facing content.

    A good anchor is a recall-eligible conversation message
    (``is_context_message``: user/assistant/tool/error/compaction_checkpoint,
    minus skill-context notes). Kernel-internal annotations — plain notes and
    ``run_summary`` records — are skipped so a chunk that mixes them with a
    real message anchors on the real message, not the annotation.
    """

    return not is_context_message(message)


def _chunk_from_passage(passage: Passage) -> Chunk:
    return Chunk(
        anchor_message_id=passage.start_message_id,
        start_message_id=passage.start_message_id,
        end_message_id=passage.end_message_id,
        text=passage.text,
        snippet=passage.text,
        passage_id=passage.passage_id,
        start_timestamp=passage.start_timestamp,
        end_timestamp=passage.end_timestamp,
        start_role=passage.start_role,
        end_role=passage.end_role,
    )


def build_session_chunks(messages: Iterable[Any]) -> list[Chunk]:
    """Pack a session's messages into one or more embeddable chunks.

    The chunker walks the messages in order, collecting each message's
    search-text (capped at ``_PER_MESSAGE_CHAR_CAP``) into a running
    buffer. When adding the next message would push the buffer past
    ``_CHUNK_TARGET_CHARS``, the chunk is sealed and a new buffer is
    started with the last ``_CHUNK_OVERLAP_MESSAGES`` messages carried
    over for boundary context. A single message longer than
    ``_CHUNK_TARGET_CHARS`` is hard-capped via
    :meth:`VectorStore.truncate_to_input_limit` so the model never
    receives a request above the input budget.

    The anchor for each chunk is the first non-note, non-skill-context
    message in the chunk (or the chunk's first message if every
    message is a note). The ``start_message_id`` / ``end_message_id``
    bound the chunk's actual message span regardless of which messages
    contributed text.
    """

    chunks: list[Chunk] = []
    current_messages: list[Any] = []
    current_texts: list[str] = []
    current_chars = 0

    def _seal() -> None:
        if not current_messages:
            return
        text = "\n".join(current_texts)
        # Skip chunks with no embeddable text. A window of only run_summary
        # records (which carry no searchable content) joins to an empty
        # string, and an empty string embeds to a constant vector that
        # pollutes every query with identical-distance, empty-snippet noise.
        if not compact_text(text):
            return
        # Anchor: first non-skippable message; fall back to the chunk's
        # first message so we never hand back an empty anchor id.
        anchor_id = ""
        for message in current_messages:
            if not _is_skippable_for_anchor(message):
                anchor_id = getattr(message, "id", "") or anchor_id
                if anchor_id:
                    break
        if not anchor_id:
            anchor_id = getattr(current_messages[0], "id", "")
        start_id = getattr(current_messages[0], "id", "")
        end_id = getattr(current_messages[-1], "id", "")
        chunks.append(
            Chunk(
                anchor_message_id=anchor_id,
                start_message_id=start_id,
                end_message_id=end_id,
                text=text,
                snippet=build_snippet(text),
            )
        )

    for message in messages:
        # A session_search result is the recall tool's own output; embedding it
        # makes future searches match their own prior results. Treat it as
        # empty text so it never contributes to a chunk's embedding (a chunk of
        # only such messages collapses to empty text and is skipped in _seal).
        raw_text = "" if is_recall_artifact_message(message) else message_search_text(message)
        if not raw_text:
            # Empty search-text messages still count toward the chunk's
            # message span (and may be the anchor), so we track them in
            # ``current_messages`` but contribute nothing to the text
            # budget.
            current_messages.append(message)
            continue
        text = raw_text[:_PER_MESSAGE_CHAR_CAP]
        if len(text) > _CHUNK_TARGET_CHARS:
            # Single message would still overflow the chunk budget even
            # after the per-message cap — seal what we have (so the
            # giant message gets its own clean chunk), then write a
            # hard-capped chunk for this message.
            _seal()
            oversized = VectorStore.truncate_to_input_limit(text, context_window=None)
            chunks.append(
                Chunk(
                    anchor_message_id=getattr(message, "id", ""),
                    start_message_id=getattr(message, "id", ""),
                    end_message_id=getattr(message, "id", ""),
                    text=oversized,
                    snippet=build_snippet(oversized),
                )
            )
            current_messages = []
            current_texts = []
            current_chars = 0
            continue
        projected = current_chars + len(text) + (1 if current_texts else 0)
        if projected > _CHUNK_TARGET_CHARS and current_texts:
            # Seal the current chunk and carry the last N messages into
            # the next one for boundary context.
            _seal()
            if _CHUNK_OVERLAP_MESSAGES > 0:
                overlap_messages = current_messages[-_CHUNK_OVERLAP_MESSAGES:]
            else:
                # ``list[:-0]`` returns the full list (because ``-0 == 0``),
                # so a zero overlap must skip the slice entirely.
                overlap_messages = []
            overlap_texts: list[str] = []
            for overlap_message in overlap_messages:
                overlap_text = message_search_text(overlap_message)
                if overlap_text:
                    overlap_texts.append(overlap_text[:_PER_MESSAGE_CHAR_CAP])
            current_messages = list(overlap_messages)
            current_texts = overlap_texts
            current_chars = sum(len(part) for part in current_texts) + max(
                len(current_texts) - 1, 0
            )
        current_messages.append(message)
        current_texts.append(text)
        current_chars += len(text) + (1 if len(current_texts) > 1 else 0)
    _seal()
    return chunks


def build_snippet(text: str, limit: int = 320) -> str:
    """Return a compact headline snippet for the indexed chunk."""

    compact = compact_text(text)
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[: max(limit - 3, 0)] + "..."


def render_vector_matches(
    request: RecallRequest,
    matches: list[JsonObject],
    *,
    truncated: bool,
) -> str:
    """Render a short textual summary of vector matches for the tool UI."""

    if not matches:
        return f"No semantic matches found for query: {request.query}"

    lines = [f"Found {len(matches)} semantic match(es) for query: {request.query}"]
    for index, match in enumerate(matches, start=1):
        distance = match.get("distance")
        distance_str = f"{distance:.4f}" if isinstance(distance, (int, float)) else "n/a"
        chunk_index = match.get("chunk_index")
        chunk_suffix = f" chunk={chunk_index}" if chunk_index is not None else ""
        lines.append(
            f"[{index}] session={match['session_id']} distance={distance_str} "
            f"anchor={match['message_id']}{chunk_suffix}"
        )
        snippet_text = match.get("snippet") or ""
        if snippet_text:
            lines.append(f"  {snippet_text}")
    if truncated:
        lines.append(f"[Results limited to {request.limit} matches.]")
    return "\n".join(lines)


__all__ = [
    "Chunk",
    "VectorRecallBackend",
    "build_session_chunks",
    "build_snippet",
    "render_vector_matches",
]
