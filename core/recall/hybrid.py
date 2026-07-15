"""Hybrid Recall backend combining Passage FTS and Vector rankings.

Typed first-party search runs both arms concurrently and applies true Reciprocal
Rank Fusion with adaptive candidate depth. It preserves multiple Passages per
Session and reports one-arm degradation explicitly. The older
``RecallBackend.search`` method retains its session-grouped payload solely for
legacy callers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib

from core.recall.jsonl import (
    JsonlSessionRecallBackend,
    request_payload,
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
from core.recall.sqlite_fts import SqliteFtsRecallBackend
from core.recall.vector import VectorRecallBackend, render_vector_matches

# Over-fetch multiplier applied to each arm before session-level dedup.
# The vector backend uses 8x+4 for chunk→session dedup; for hybrid a
# smaller multiplier with a larger margin keeps the literal group full
# of distinct sessions even when one session is unusually repetitive.
_FETCH_MULTIPLIER = 3
# Over-fetch margin on top of ``limit * _FETCH_MULTIPLIER`` so a
# session with many FTS hits (FTS is per-message) does not starve the
# literal group of other distinct sessions.
_FETCH_MARGIN = 10
_RRF_RANK_CONSTANT = 60
_RRF_INITIAL_DEPTH = 20

# Guidance appended to the session_search tool description when this backend is
# active (see ``describe_search``). Static capability text — actual semantic
# availability is surfaced per-call via the notice propagated from the vector arm.
_HYBRID_SEARCH_GUIDANCE = (
    "This backend combines literal keyword matching with semantic meaning-based "
    "matching: a single keyword surfaces every exact occurrence, and a short "
    "descriptive phrase additionally finds conceptually related sessions that share "
    "no words. Use plain keywords to find exact mentions, or a phrase to search by topic."
)


class HybridRecallBackend(JsonlSessionRecallBackend):
    """Recall backend that fuses FTS literal matches with vector semantic matches."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.logger = context.logger
        self._fts = SqliteFtsRecallBackend(context)
        self._vector = VectorRecallBackend(context)

    def describe_search(self) -> str:
        return _HYBRID_SEARCH_GUIDANCE

    def search_capabilities(self) -> RecallSearchCapabilities:
        return RecallSearchCapabilities(
            result_type="passage",
            guidance=_HYBRID_SEARCH_GUIDANCE,
            match_argument="literal_match",
            match_modes=("all_terms", "any_term", "phrase"),
            order_modes=("relevance",),
            default_order="relevance",
        )

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        depth = max(_RRF_INITIAL_DEPTH, request.offset + request.limit + 1)
        literal_page: RecallSearchPage | None = None
        semantic_page: RecallSearchPage | None = None
        literal_error: Exception | None = None
        semantic_error: Exception | None = None
        fused: list[RecallSearchHit] = []

        while True:
            arm_request = dataclasses.replace(
                request,
                offset=0,
                limit=depth,
                snapshot_id=None,
            )
            literal_result, semantic_result = await asyncio.gather(
                self._fts.search_passages(arm_request),
                self._vector.search_page(arm_request),
                return_exceptions=True,
            )
            literal_page = literal_result if isinstance(literal_result, RecallSearchPage) else None
            semantic_page = (
                semantic_result if isinstance(semantic_result, RecallSearchPage) else None
            )
            literal_error = literal_result if isinstance(literal_result, Exception) else None
            semantic_error = semantic_result if isinstance(semantic_result, Exception) else None
            if literal_page is None and semantic_page is None:
                raise RecallSearchError(
                    "hybrid_unavailable",
                    "Both literal and semantic retrieval are unavailable.",
                )
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
        degraded = literal_page is None or semantic_page is None
        reason: str | None = None
        if degraded:
            failed_arm = "literal" if literal_page is None else "semantic"
            error = literal_error if literal_page is None else semantic_error
            reason = f"{failed_arm} retrieval unavailable"
            if isinstance(error, RecallSearchError):
                reason = str(error)
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
            degraded=degraded,
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

    async def search(self, request: RecallRequest) -> JsonObject:
        # ``browse`` and ``scroll`` keep the canonical JSONL behavior
        # (nothing to fuse). Only ``search`` is hybrid.
        summaries = await asyncio.to_thread(self.candidate_session_summaries, request)
        if request.query is None:
            return self.session_summary_result(request, summaries)
        if not request.query.strip():
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)
        if not summaries:
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)

        over_fetched = dataclasses.replace(
            request,
            limit=request.limit * _FETCH_MULTIPLIER + _FETCH_MARGIN,
        )

        # Run both arms; each one is a self-contained backend with its
        # own fallback policy, so any per-arm failure surfaces only as
        # a missing contribution to the fused result.
        fts_result, vector_result = await asyncio.gather(
            self._fts.search(over_fetched),
            self._vector.search(over_fetched),
        )

        fts_matches = list(fts_result.get("matches", []))
        vector_matches = list(vector_result.get("matches", []))

        literal_group, semantic_only_group = self._group_matches(fts_matches, vector_matches)
        ordered = self._order_matches(
            request,
            literal_group,
            semantic_only_group,
        )
        truncated = len(ordered) > request.limit
        limited = ordered[: request.limit]
        result = self._message_result(
            request,
            limited,
            searched_sessions=max(
                int(fts_result.get("searched_sessions", 0)),
                int(vector_result.get("searched_sessions", 0)),
                len(summaries),
            ),
            total_candidates=max(
                int(fts_result.get("total_candidate_sessions", 0)),
                int(vector_result.get("total_candidate_sessions", 0)),
                len(summaries),
            ),
            truncated=truncated,
        )
        # The vector arm only sets ``notice`` when its semantic search could not
        # run (no embedding model, or a transient embed failure) and it fell back
        # to literal scanning. In that case the fused result is literal-only, so
        # re-surface the reason — otherwise the agent assumes full coverage.
        vector_notice = vector_result.get("notice")
        if vector_notice:
            result = _with_semantic_notice(result, str(vector_notice))
        return result

    # ------------------------------------------------------------------
    # Match grouping
    # ------------------------------------------------------------------

    @staticmethod
    def _group_matches(
        fts_matches: list[JsonObject],
        vector_matches: list[JsonObject],
    ) -> tuple[list[JsonObject], list[JsonObject]]:
        """Group matches into (literal+both, semantic-only) per session.

        FTS is per-message and can yield several matches in the same
        session; collapse to the first match in FTS order per session
        (its snippet contains the exact term the user typed). The
        vector arm is already per-session. The first-seen FTS match
        for a session is the literal payload; the distance — if any —
        is taken from the vector match for that session.
        """

        literal_group: list[JsonObject] = []
        literal_session_ids: set[str] = set()
        # Track the best (smallest) distance for each session hit by
        # the vector arm; the FTS group is the user-facing payload
        # regardless of how many vector chunks the session had.
        vector_by_session: dict[str, JsonObject] = {}
        # Iterate in FTS order so the kept FTS match is the *first*
        # FTS hit per session (the FTS arm orders by request.sort).
        for match in fts_matches:
            session_id = str(match.get("session_id", ""))
            if not session_id or session_id in literal_session_ids:
                continue
            literal_session_ids.add(session_id)
            literal_group.append(match)
        for match in vector_matches:
            session_id = str(match.get("session_id", ""))
            if not session_id:
                continue
            vector_by_session[session_id] = match

        literal_final: list[JsonObject] = []
        for match in literal_group:
            session_id = str(match.get("session_id", ""))
            vector_match = vector_by_session.get(session_id)
            if vector_match is not None and "distance" in vector_match:
                payload = dict(match)
                payload["distance"] = vector_match["distance"]
                if "chunk_index" in vector_match:
                    payload["chunk_index"] = vector_match["chunk_index"]
                payload["source"] = "both"
                literal_final.append(payload)
            else:
                payload = dict(match)
                payload["source"] = "literal"
                literal_final.append(payload)

        semantic_only_group: list[JsonObject] = []
        for session_id, match in vector_by_session.items():
            if session_id in literal_session_ids:
                continue
            if "distance" not in match:
                # Distance-less vector match = vector arm's JSONL
                # fallback. Treat as literal-only to avoid double-
                # surfacing the same session from both arms' fallbacks.
                continue
            payload = dict(match)
            payload["source"] = "semantic"
            semantic_only_group.append(payload)
        return literal_final, semantic_only_group

    # ------------------------------------------------------------------
    # Match ordering
    # ------------------------------------------------------------------

    @staticmethod
    def _order_matches(
        request: RecallRequest,
        literal_group: list[JsonObject],
        semantic_group: list[JsonObject],
    ) -> list[JsonObject]:
        """Order the fused match list: literal/both by ``sort``, then semantic by distance.

        FTS already orders candidates by ``request.sort``; the literal
        group keeps that order. The semantic group is *always* ordered
        by ascending ``distance`` regardless of ``sort`` — recency
        would scramble the only meaningful relevance signal semantic
        hits have.
        """

        literal_ordered = sorted(
            literal_group,
            key=lambda match: _literal_sort_key(match, request.sort),
            reverse=request.sort == "newest",
        )
        semantic_ordered = sorted(
            semantic_group,
            key=lambda match: float(match.get("distance", float("inf"))),
        )
        return [*literal_ordered, *semantic_ordered]

    # ------------------------------------------------------------------
    # Result shape
    # ------------------------------------------------------------------

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
            "content": render_hybrid_matches(request, matches, truncated=truncated),
            "matches": matches,
            "truncated": truncated,
            "searched_sessions": searched_sessions,
            "total_candidate_sessions": total_candidates,
            "request": request_payload(request),
        }


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


def _with_semantic_notice(result: JsonObject, vector_notice: str) -> JsonObject:
    """Re-surface the vector arm's degradation notice on the fused result.

    When the semantic arm could not run, the hybrid output is literal-only; the
    agent must know the semantic half was skipped rather than assume full
    coverage. The notice is prepended to ``content`` and exposed as ``notice``.
    """

    notice = f"Semantic augmentation unavailable for this search. {vector_notice}"
    content = result.get("content", "")
    decorated = f"{notice}\n\n{content}" if content else notice
    return {**result, "content": decorated, "notice": notice}


def _literal_sort_key(match: JsonObject, sort: str) -> str:
    """Pick the sort key for a literal/both match.

    The literal group's FTS hits are already in timestamp order; the
    vector contribution only adds ``distance``/``chunk_index`` and
    never changes the user-facing sort dimension.
    """

    timestamp = match.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        return timestamp
    # Fall back to a stable key so the sort never raises on a malformed
    # payload; an empty string sorts oldest-first in either direction.
    return ""


def render_hybrid_matches(
    request: RecallRequest,
    matches: list[JsonObject],
    *,
    truncated: bool,
) -> str:
    """Render fused hybrid matches for the tool UI.

    Each match is tagged ``[literal]`` / ``[semantic]`` / ``[both]``
    and entries that carry a ``distance`` show it to four decimal
    places. Reuses the existing match-line formatting style from
    :func:`render_vector_matches`.
    """

    if not matches:
        return f"No matches found for query: {request.query}"

    lines = [f"Found {len(matches)} match(es) for query: {request.query}"]
    for index, match in enumerate(matches, start=1):
        source = match.get("source", "literal")
        tag = f"[{source}]"
        distance = match.get("distance")
        distance_str = f"{distance:.4f}" if isinstance(distance, (int, float)) else "n/a"
        chunk_index = match.get("chunk_index")
        chunk_suffix = f" chunk={chunk_index}" if chunk_index is not None else ""
        if source == "semantic":
            lines.append(
                f"[{index}] {tag} session={match['session_id']} distance={distance_str} "
                f"anchor={match['message_id']}{chunk_suffix}"
            )
        else:
            lines.append(
                f"[{index}] {tag} {match['session_id']} {match['timestamp']} "
                f"{match['role']} {match['message_id']}"
            )
            if isinstance(distance, (int, float)):
                lines.append(f"  distance={distance_str}")
        snippet_text = match.get("snippet") or ""
        if snippet_text:
            lines.append(f"  {snippet_text}")
        context = match.get("context")
        if isinstance(context, dict):
            for side in ("before", "after"):
                for item in context.get(side, []):
                    lines.append(f"  {side}: {item['timestamp']} {item['role']} {item['snippet']}")
    if truncated:
        lines.append(f"[Results limited to {request.limit} matches.]")
    return "\n".join(lines)


# Re-export the vector renderer so callers can still reach it
# through the hybrid module if they want to mirror its visual
# style elsewhere.
__all__ = [
    "HybridRecallBackend",
    "render_hybrid_matches",
    "render_vector_matches",
]
