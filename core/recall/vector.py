"""Vector recall backend — per-chunk semantic search over a sqlite-vec store.

The backend inherits the canonical browse/scroll implementation from
:class:`JsonlSessionRecallBackend` and only overrides search. The
search path:

1. Resolves the embedding binding through the runtime
   :class:`core.model_tasks.EmbeddingService`; if no binding is
   configured the backend logs a warning and falls back to the JSONL
   scanner for that call.
2. Ensures every JSONL session for the requested agent has fresh
   **chunk** vectors in the sqlite-vec store (eager on-search backfill).
   Each session's searchable text is split into one or more
   ``Chunk`` windows of consecutive messages, each capped around
   ``_CHUNK_TARGET_CHARS`` characters with ``_CHUNK_OVERLAP_MESSAGES``
   carried into the next chunk. Chunk anchors pick the first
   non-skill-context, non-note message so the matched region is the
   part of the session the user actually asked about, not the session
   opener.
3. Embeds the query with the same binding, then runs a cosine KNN
   over the chunk vectors.
4. Dedups the KNN hits to the **nearest** chunk per session, drops
   anything beyond ``_MAX_DISTANCE``, and hydrates a representative
   window per surviving session from canonical JSONL, anchored at the
   matching chunk's anchor message. Only **structural** filters apply
   (agent/time/skill-note); ranking stays by vector distance because
   semantic match has no literal query term to re-validate.

The store pins ``(provider_id, model_id, dimension)`` in its header so
switching the embedding binding drops and rebuilds the index on the
next open. Any sqlite-vec/embed failure falls back to JSONL for the
call, mirroring the SQLite FTS backend's safety net.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingService,
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
from core.recall.recall import JsonObject, RecallBackendContext, RecallRequest
from core.recall.vector_store import (
    ChunkVectorRecord,
    VectorHeader,
    VectorStore,
    VectorStoreError,
    format_started_at,
)

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
# Cosine-distance cutoff. Distances run 0 (identical) to 2 (opposite);
# 0.7 keeps anything that the embedding model considered meaningfully
# related to the query and drops the long tail of weak hits.
_MAX_DISTANCE = 0.7
# Over-fetch multiplier for KNN before chunk→session dedup. The
# recall backend requests ``limit * multiplier + KNN margin`` chunks so
# the per-session nearest-chunk selection still leaves ``limit``
# distinct sessions after the cutoff and structural filters.
_CHUNK_FETCH_MULTIPLIER = 8
# Margin to over-fetch from KNN so structural filters still leave ``limit`` hits.
_KNN_FETCH_MARGIN = 4
# How many times to halve an over-long embedding input before giving up. The
# character-budget truncation is a heuristic and cannot guarantee staying under
# the model's *token* cap for dense text (German compounds, code, CJK); when the
# provider rejects the input for context length, we shrink and retry until it
# fits. 6 halvings take any realistic session text down to a few hundred chars.
_EMBED_OVERFLOW_RETRIES = 6

# Guidance appended to the session_search tool description when this backend is
# active (see ``describe_search``). Static: it describes the capability, not the
# current availability — actual availability is surfaced per-call in the result
# (the degradation notices below).
_SEMANTIC_SEARCH_GUIDANCE = (
    "This backend matches by meaning rather than literal words — describe the concept "
    "or topic in a short phrase. A single bare keyword anchors poorly; prefer a "
    "descriptive phrase. It will not reliably surface every literal occurrence of a word."
)
# Notice attached to a degraded result when semantic search could not run. The
# config case is actionable (configure a model); the transient case is operational.
# Prepended to ``content`` (the model-facing tool output) and also exposed as a
# structured ``notice`` field so a composing backend (hybrid) can re-surface it.
_SEMANTIC_UNAVAILABLE_NOTICE = (
    "Semantic search is unavailable: no embedding model is configured. Configure a "
    "text_embedding model in Settings to enable meaning-based recall. Showing literal "
    "keyword matches instead."
)
_SEMANTIC_FAILED_NOTICE = (
    "Semantic search failed for this query and fell back to literal keyword matching. "
    "Results may miss meaning-related sessions; retry, or check the embedding provider."
)
# Sentinel stored for the identity/global scope (``project_id is None``) in the
# chunk-key tuple. An empty string keeps the store's UNIQUE constraint reliable —
# SQLite treats NULLs as distinct, which would break per-scope uniqueness.
_GLOBAL_PROJECT_SCOPE = ""


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


class VectorRecallBackend(JsonlSessionRecallBackend):
    """Recall backend backed by sqlite-vec per-chunk vectors."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.store = VectorStore(context.data_dir)
        self.logger = context.logger
        self.embeddings: EmbeddingService | None = context.embeddings
        self.model_registry: ModelRegistry | None = context.model_registry
        self._fallback = JsonlSessionRecallBackend(context.sessions)
        # Cached resolved binding for the lifetime of the index — the store
        # itself drops+rebuilds on a binding change, so the cache is always
        # in sync with the on-disk header after the first successful embed.
        self._resolved_header: VectorHeader | None = None

    def describe_search(self) -> str:
        return _SEMANTIC_SEARCH_GUIDANCE

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, request: RecallRequest) -> JsonObject:
        summaries = self.candidate_session_summaries(request)
        if request.query is None:
            return self.session_summary_result(request, summaries)
        if not request.query.strip():
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)
        if not summaries:
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)

        try:
            return self._search_with_vector_store(request, summaries)
        except (VectorStoreError, EmbeddingError, OSError, sqlite3.Error) as error:
            self._warning("Vector recall failed; falling back to JSONL scan: %s", error)
            return self._degraded_result(self._fallback.search(request), _SEMANTIC_FAILED_NOTICE)

    def _search_with_vector_store(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> JsonObject:
        binding_header = self._resolve_header()
        if binding_header is None:
            self._warning("Vector recall has no embedding binding; falling back to JSONL scan")
            return self._degraded_result(
                self._fallback.search(request), _SEMANTIC_UNAVAILABLE_NOTICE
            )

        self._ensure_fresh_index(request, summaries, binding_header)

        # After the backfill the cached ``_resolved_header`` holds the real
        # dimension observed from the embedding provider. Use that for the
        # KNN call — the binding-resolution header only knows the (provider,
        # model) pair and is unaware of dimension until the first embed runs.
        # ``search()`` has already rejected ``None``/blank queries before
        # calling us, so the cast is just narrowing for the type-checker.
        query = cast(str, request.query)
        query_vector = self._embed_query(binding_header, query)
        pinned_header = self._resolved_header
        if pinned_header is None or pinned_header.dimension <= 0:
            raise VectorStoreError("vector store header is not pinned after embed")
        candidates = self.store.knn_search(
            header=pinned_header,
            query_vector=query_vector,
            limit=request.limit * _CHUNK_FETCH_MULTIPLIER + _KNN_FETCH_MARGIN,
        )
        if not candidates:
            return self._message_result(
                request,
                [],
                searched_sessions=len(summaries),
                total_candidates=len(summaries),
            )

        rowid_to_record = self.store.get_chunks_by_rowids([rowid for rowid, _ in candidates])

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

    def _resolve_header(self) -> VectorHeader | None:
        """Resolve the binding identity; ``None`` means no usable binding."""

        if self.embeddings is None:
            return None
        try:
            provider_id, model_id = self.embeddings.resolve_model_id()
        except EmbeddingError as error:
            self._warning("Vector recall binding lookup failed: %s", error)
            return None

        stored = self.store.read_header()
        if stored is not None and (
            stored.provider_id != provider_id or stored.model_id != model_id
        ):
            # Bound model changed — drop the now-incompatible index so the
            # next upsert rebuilds from scratch. The spec's "no
            # legacy/compat" decision says a binding switch is a hard reset.
            self._warning(
                "Vector recall binding changed (%s/%s → %s/%s); rebuilding index",
                stored.provider_id,
                stored.model_id,
                provider_id,
                model_id,
            )
            self.store.drop_index()
            self._resolved_header = None
        if self._resolved_header is not None and self._headers_match(
            self._resolved_header,
            VectorHeader(provider_id=provider_id, model_id=model_id, dimension=0),
        ):
            return self._resolved_header
        return VectorHeader(provider_id=provider_id, model_id=model_id, dimension=0)

    @staticmethod
    def _headers_match(left: VectorHeader, right: VectorHeader) -> bool:
        return left.provider_id == right.provider_id and left.model_id == right.model_id

    def _embed_query(self, header: VectorHeader, query: str) -> list[float]:
        """Embed a single query string and pin the dimension from the first response."""

        result = self._run_embed([query])
        if result.dimension <= 0:
            raise VectorStoreError(
                f"embedding provider returned empty dimension for {header.model_id}"
            )
        self._resolved_header = VectorHeader(
            provider_id=result.provider_id,
            model_id=result.model_id,
            dimension=result.dimension,
        )
        return list(result.vectors[0])

    def _embed_chunks(self, texts: list[str]) -> tuple[list[list[float]], VectorHeader]:
        """Embed a batch of chunk texts and return vectors with the resolved header."""

        result = self._run_embed(texts)
        header = VectorHeader(
            provider_id=result.provider_id,
            model_id=result.model_id,
            dimension=result.dimension,
        )
        self._resolved_header = header
        return [list(vector) for vector in result.vectors], header

    def _run_embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed *texts*, batching into ``_EMBED_BATCH_SIZE`` groups.

        Each batch is sent through the shrink-retry path independently so
        a context-overflow on one batch does not affect the others, and
        single-text callers naturally fall through the ``len == 1`` path
        without any splitting. Vectors are concatenated in input order
        and the response's ``provider_id``/``model_id``/``dimension`` is
        asserted consistent across batches — a binding switch mid-batch
        is an error, not a silent mix.
        """

        if self.embeddings is None:
            raise EmbeddingError("embedding service is not configured")
        if not texts:
            raise EmbeddingError("embedding input is empty")
        if len(texts) == 1:
            # Fast path: a single query is one batch with no concatenation.
            return self._run_embed_batch(texts[0])

        combined_vectors: list[list[float]] = []
        combined_provider: str | None = None
        combined_model: str | None = None
        combined_dimension: int | None = None
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            result = self._run_embed_batch(batch)
            if combined_provider is None:
                combined_provider = result.provider_id
                combined_model = result.model_id
                combined_dimension = result.dimension
            else:
                if result.provider_id != combined_provider:
                    raise EmbeddingError(
                        f"embedding provider changed mid-batch: "
                        f"{combined_provider} → {result.provider_id}"
                    )
                if result.model_id != combined_model:
                    raise EmbeddingError(
                        f"embedding model changed mid-batch: {combined_model} → {result.model_id}"
                    )
                if result.dimension != combined_dimension:
                    raise EmbeddingError(
                        f"embedding dimension changed mid-batch: "
                        f"{combined_dimension} → {result.dimension}"
                    )
            combined_vectors.extend(list(vector) for vector in result.vectors)
        assert combined_provider is not None
        assert combined_model is not None
        assert combined_dimension is not None
        return EmbeddingResult(
            vectors=tuple(combined_vectors),
            model_id=combined_model,
            provider_id=combined_provider,
            dimension=combined_dimension,
        )

    def _run_embed_batch(self, batch: str | list[str]) -> EmbeddingResult:
        """Embed one batch (single string or list) with the shrink-retry loop."""

        # ``_run_embed`` is the only caller and it raises when the
        # embedding service is missing; the cast keeps mypy happy
        # without re-checking the same condition on every retry.
        embeddings = cast(EmbeddingService, self.embeddings)
        if isinstance(batch, str):
            current: list[str] = [batch]
        else:
            current = list(batch)
        for attempt in range(_EMBED_OVERFLOW_RETRIES + 1):
            try:
                result = self._run_async(embeddings.embed, current)
                return cast(EmbeddingResult, result)
            except EmbeddingError as error:
                # Only the provider's context-length rejection is recoverable by
                # shrinking — every other embedding error (auth, network, no
                # binding) re-raises immediately and the caller falls back.
                if attempt >= _EMBED_OVERFLOW_RETRIES or not _is_context_overflow(error):
                    raise
                cap = max((len(text) for text in current), default=0) // 2
                if cap <= 0:
                    raise
                current = [text[:cap] if len(text) > cap else text for text in current]
                self._warning(
                    "Embedding input exceeded the model context window; "
                    "shrinking to <=%d chars and retrying (attempt %d/%d)",
                    cap,
                    attempt + 1,
                    _EMBED_OVERFLOW_RETRIES,
                )
        # The loop either returns a result or re-raises inside the body; this is
        # only reached if the retry budget is exhausted by repeated overflows.
        raise EmbeddingError("embedding input still exceeded the context window after retries")

    def _run_async(self, awaitable_func: Any, *args: Any) -> Any:
        """Drive an async coroutine from a sync backend call.

        The recall backend is invoked from sync tool code; the embed
        service is async. When no event loop is running we run it
        inline via :func:`asyncio.run`. When a loop is already running
        (production — FastAPI handlers) we cannot block on
        ``run_coroutine_threadsafe(...).result()`` because the call
        thread IS the loop thread, so the future can never make
        progress. Instead we hand the coroutine to a worker thread
        that runs its own loop via :func:`asyncio.run`.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable_func(*args))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, awaitable_func(*args))
            return future.result()

    # ------------------------------------------------------------------
    # Freshness + backfill
    # ------------------------------------------------------------------

    def _ensure_fresh_index(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
        header: VectorHeader,
    ) -> None:
        """Make sure every JSONL session in this scope has fresh chunk vectors."""

        agent_id = request.agent_id
        scope = _project_scope(request.project_id)
        active = {str(summary["id"]): summary for summary in summaries}
        indexed = self.store.list_indexed_sessions(agent_id, scope)

        # Drop JSONL sessions that have been removed since last index.
        stale_to_remove = sorted(set(indexed) - set(active))
        if stale_to_remove:
            self.store.drop_indexed_sessions(agent_id, scope, stale_to_remove)

        # Collect every (session summary, mtime, size) that's missing or
        # whose JSONL changed since the last backfill. Sessions whose
        # mtime/size already match are skipped.
        stale_sessions: list[tuple[JsonObject, int, int, list[Any]]] = []
        for session_id, summary in active.items():
            session = self.sessions.get(agent_id, session_id, request.project_id)
            stat = session.path.stat()
            cached = indexed.get(session_id)
            if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                continue
            messages = session.load()
            stale_sessions.append((summary, stat.st_mtime_ns, stat.st_size, messages))

        if not stale_sessions:
            return

        # Pack all stale sessions into chunks up front. A session that
        # yields zero indexable chunks is *not* covered by
        # ``upsert_many_chunks`` (it only wipes sessions present in its
        # ``records``), so its prior rows would silently survive a JSONL
        # change to all-empty content. ``delete_session`` clears them.
        all_chunks: list[tuple[JsonObject, int, int, Chunk]] = []
        for summary, mtime_ns, size_bytes, messages in stale_sessions:
            chunks = build_session_chunks(messages)
            if not chunks:
                self.store.delete_session(agent_id, scope, str(summary["id"]))
                continue
            for chunk in chunks:
                all_chunks.append((summary, mtime_ns, size_bytes, chunk))
        if not all_chunks:
            return

        texts = [chunk.text for _, _, _, chunk in all_chunks]
        vectors, resolved_header = self._embed_chunks(texts)
        if resolved_header.dimension <= 0:
            raise VectorStoreError("embedding provider returned no vectors")

        # Per-session running counter: chunk_index must be unique within
        # ``(project_id, agent_id, session_id)`` and start at 0 — the store's
        # ``UNIQUE(project_id, agent_id, session_id, chunk_index)`` constraint
        # will reject duplicates, so a stable, ordered counter is required.
        records: list[tuple[ChunkVectorRecord, list[float]]] = []
        per_session_index: dict[str, int] = {}
        for (summary, mtime_ns, size_bytes, chunk), vector in zip(all_chunks, vectors, strict=True):
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
                        mtime_ns=mtime_ns,
                        size_bytes=size_bytes,
                        anchor_message_id=chunk.anchor_message_id,
                        snippet=chunk.snippet,
                        chunk_index=index,
                        start_message_id=chunk.start_message_id,
                        end_message_id=chunk.end_message_id,
                    ),
                    vector,
                )
            )

        self.store.upsert_many_chunks(header=resolved_header, records=records)
        self._resolved_header = resolved_header

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

        messages = self.sessions.get(request.agent_id, record.session_id, request.project_id).load()
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
