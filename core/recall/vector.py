"""Vector recall backend — per-session semantic search over a sqlite-vec store.

The backend inherits the canonical browse/scroll implementation from
:class:`JsonlSessionRecallBackend` and only overrides search. The
search path:

1. Resolves the embedding binding through the runtime
   :class:`core.embeddings.EmbeddingService`; if no binding is
   configured the backend logs a warning and falls back to the JSONL
   scanner for that call.
2. Ensures every JSONL session for the requested agent has a fresh
   vector in the sqlite-vec store (eager on-search backfill — see
   the plan's Decisions).
3. Embeds the query with the same binding, then runs a cosine KNN
   over the session vectors.
4. Hydrates a representative window per nearest session from
   canonical JSONL and applies **structural** filters only
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
from collections.abc import Iterable
from typing import Any, cast

from core.embeddings import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingService,
)
from core.models.models import ModelRegistry
from core.recall.jsonl import (
    JsonlSessionRecallBackend,
    compact_text,
    message_index_by_id,
    message_match_payload,
    message_search_text,
    request_payload,
)
from core.recall.recall import JsonObject, RecallBackendContext, RecallRequest
from core.recall.vector_store import (
    SessionVectorRecord,
    VectorHeader,
    VectorStore,
    VectorStoreError,
    format_started_at,
)

# Margin to over-fetch from KNN so structural filters still leave ``limit`` hits.
_KNN_FETCH_MARGIN = 4
# How many times to halve an over-long embedding input before giving up. The
# character-budget truncation is a heuristic and cannot guarantee staying under
# the model's *token* cap for dense text (German compounds, code, CJK); when the
# provider rejects the input for context length, we shrink and retry until it
# fits. 6 halvings take any realistic session text down to a few hundred chars.
_EMBED_OVERFLOW_RETRIES = 6


class VectorRecallBackend(JsonlSessionRecallBackend):
    """Recall backend backed by sqlite-vec per-session vectors."""

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
        except (VectorStoreError, EmbeddingError, OSError) as error:
            self._warning("Vector recall failed; falling back to JSONL scan: %s", error)
            return self._fallback.search(request)

    def _search_with_vector_store(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> JsonObject:
        binding_header = self._resolve_header()
        if binding_header is None:
            self._warning("Vector recall has no embedding binding; falling back to JSONL scan")
            return self._fallback.search(request)

        self._ensure_fresh_index(request.agent_id, summaries, binding_header)

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
            limit=request.limit + _KNN_FETCH_MARGIN,
        )
        if not candidates:
            return self._message_result(
                request,
                [],
                searched_sessions=len(summaries),
                total_candidates=len(summaries),
            )

        rowid_to_record = self.store.get_sessions_by_rowids([rowid for rowid, _ in candidates])

        matches: list[JsonObject] = []
        for rowid, distance in candidates:
            record = rowid_to_record.get(rowid)
            if record is None:
                continue
            summary = self._summary_by_session_id(summaries, record.session_id)
            if summary is None:
                continue
            session_match = self._hydrate_session(request, summary, record, distance)
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
            truncated=len(matches) >= request.limit,
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

    def _embed_sessions(self, texts: list[str]) -> tuple[list[list[float]], VectorHeader]:
        """Embed a batch of session texts and return vectors with the resolved header."""

        result = self._run_embed(texts)
        header = VectorHeader(
            provider_id=result.provider_id,
            model_id=result.model_id,
            dimension=result.dimension,
        )
        self._resolved_header = header
        return [list(vector) for vector in result.vectors], header

    def _run_embed(self, texts: list[str]) -> EmbeddingResult:
        if self.embeddings is None:
            raise EmbeddingError("embedding service is not configured")
        current = list(texts)
        for attempt in range(_EMBED_OVERFLOW_RETRIES + 1):
            try:
                result = self._run_async(self.embeddings.embed, current)
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
        agent_id: str,
        summaries: list[JsonObject],
        header: VectorHeader,
    ) -> None:
        """Make sure every JSONL session for this agent has a fresh vector."""

        active = {str(summary["id"]): summary for summary in summaries}
        indexed = self.store.list_indexed_sessions(agent_id)

        # Drop JSONL sessions that have been removed since last index.
        stale_to_remove = sorted(set(indexed) - set(active))
        if stale_to_remove:
            self.store.drop_indexed_sessions(agent_id, stale_to_remove)

        stale_or_missing: list[tuple[JsonObject, int, int]] = []
        for session_id, summary in active.items():
            session = self.sessions.get(agent_id, session_id)
            stat = session.path.stat()
            cached = indexed.get(session_id)
            if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                continue
            stale_or_missing.append((summary, stat.st_mtime_ns, stat.st_size))

        if not stale_or_missing:
            return

        text_inputs: list[tuple[JsonObject, int, int, str, str, str]] = []
        for summary, mtime_ns, size_bytes in stale_or_missing:
            session_id = str(summary["id"])
            messages = self.sessions.get(agent_id, session_id).load()
            text = build_session_search_text(messages)
            if not text:
                continue
            text = self._truncate_to_input_limit(text, header)
            anchor_id, snippet = representative_window(messages, text)
            text_inputs.append((summary, mtime_ns, size_bytes, text, anchor_id, snippet))
        if not text_inputs:
            return

        texts = [text for *_, text, _, _ in text_inputs]
        vectors, resolved_header = self._embed_sessions(texts)
        if resolved_header.dimension <= 0:
            raise VectorStoreError("embedding provider returned no vectors")

        records: list[tuple[SessionVectorRecord, list[float]]] = []
        for (summary, mtime_ns, size_bytes, _text, anchor_id, snippet), vector in zip(
            text_inputs, vectors, strict=True
        ):
            session_id = str(summary["id"])
            records.append(
                (
                    SessionVectorRecord(
                        session_id=session_id,
                        agent_id=agent_id,
                        started_at=format_started_at(summary.get("created_at")),
                        mtime_ns=mtime_ns,
                        size_bytes=size_bytes,
                        anchor_message_id=anchor_id,
                        snippet=snippet,
                    ),
                    vector,
                )
            )

        self.store.upsert_many_sessions(header=resolved_header, records=records)
        self._resolved_header = resolved_header

    def _truncate_to_input_limit(self, text: str, header: VectorHeader | None = None) -> str:
        # On the first backfill ``_resolved_header`` is still unset — the
        # dimension is only observed after the first embed — so fall back to the
        # binding *header*, which already carries the (provider, model) pair we
        # need to look up the model's context window before any embed runs.
        # Without this, the first run truncated against the unknown-window
        # default, which overflowed bge-m3's 8192-token cap on German sessions.
        resolved = header or self._resolved_header
        if self.model_registry is None or resolved is None:
            return VectorStore.truncate_to_input_limit(text, context_window=None)
        try:
            model = self.model_registry.get(resolved.provider_id, resolved.model_id)
        except KeyError:
            return VectorStore.truncate_to_input_limit(text, context_window=None)
        return VectorStore.truncate_to_input_limit(text, context_window=model.context_window)

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

    def _hydrate_session(
        self,
        request: RecallRequest,
        summary: JsonObject,
        record: SessionVectorRecord,
        distance: float,
    ) -> JsonObject | None:
        messages = self.sessions.get(request.agent_id, record.session_id).load()
        if not messages:
            return None
        anchor_index = message_index_by_id(messages, record.anchor_message_id)
        if anchor_index is None:
            anchor_index = _first_indexable_message(messages)
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
        match["snippet"] = record.snippet
        return match

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

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


# ---------------------------------------------------------------------------
# Free functions (mirror the JSONL backend's module-level helpers)
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


def build_session_search_text(messages: Iterable[Any]) -> str:
    """Concatenate the per-message search text from a session's messages."""

    parts: list[str] = []
    for message in messages:
        text = message_search_text(message)
        if text:
            parts.append(text)
    return compact_text("\n".join(parts))


def representative_window(messages: list[Any], full_text: str) -> tuple[str, str]:
    """Pick a representative anchor message and a session-level snippet.

    For now we pick the first non-skill-context, non-note message as the
    anchor — the agent receives the window around the anchor plus
    bookends, and the snippet is a compact headline of the concatenated
    text. Future iterations could pick the message closest to the
    centroid or the most recent user message; the spec keeps this minimal.
    """

    anchor_id = ""
    for message in messages:
        role = getattr(message, "role", "")
        if role in {"skill_context_note", "note"}:
            continue
        anchor_id = getattr(message, "id", "") or anchor_id
        if anchor_id:
            break
    if not anchor_id and messages:
        anchor_id = getattr(messages[0], "id", "")
    return anchor_id, build_snippet(full_text)


def build_snippet(text: str, limit: int = 320) -> str:
    """Return a compact headline snippet for the indexed session."""

    compact = compact_text(text)
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[: max(limit - 3, 0)] + "..."


def _first_indexable_message(messages: list[Any]) -> int | None:
    for index, message in enumerate(messages):
        if getattr(message, "role", "") in {"user", "assistant", "tool", "error"}:
            return index
    return None


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
        lines.append(
            f"[{index}] session={match['session_id']} distance={distance_str} "
            f"anchor={match['message_id']}"
        )
        snippet_text = match.get("snippet") or ""
        if snippet_text:
            lines.append(f"  {snippet_text}")
    if truncated:
        lines.append(f"[Results limited to {request.limit} matches.]")
    return "\n".join(lines)


__all__ = [
    "VectorRecallBackend",
    "build_session_search_text",
    "build_snippet",
    "representative_window",
    "render_vector_matches",
]
