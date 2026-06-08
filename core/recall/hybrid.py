"""Hybrid recall backend — fuses SQLite-FTS literal matches with vector semantic matches.

The backend inherits from :class:`JsonlSessionRecallBackend` so ``browse``
and ``scroll`` reuse the canonical JSONL implementation; only
``search`` is overridden. The search path runs both arms in parallel
and fuses their results:

1. Over-fetch from each arm with an inflated limit so a single noisy
   session cannot starve the literal group of distinct sessions.
2. Group matches by session (FTS is per-message, collapse to the first
   FTS hit per session).
3. Classify the session's ``source`` by the presence of a ``distance``
   field on any match from the vector arm — ``distance`` present
   means a real semantic KNN hit. A session in both arms gets the
   literal (FTS) payload with the vector's ``distance`` attached and
   is tagged ``"both"``; a session in FTS only is tagged
   ``"literal"``; a session in the vector arm only is tagged
   ``"semantic"``.
4. Order literal/both group by ``request.sort`` (FTS already produces
   in-order candidates), then the semantic-only group by ascending
   ``distance``; truncate to ``request.limit`` and set ``truncated``.

The classification keying on ``distance`` presence is what makes
graceful degradation fall out for free: when the vector arm has no
embedding binding (or the embed call fails) it falls back to its
own JSONL scanner, whose matches have no ``distance`` field, so the
hybrid output is effectively literal-only with no special-casing.
"""

from __future__ import annotations

import dataclasses

from core.recall.jsonl import (
    JsonlSessionRecallBackend,
    request_payload,
)
from core.recall.recall import JsonObject, RecallBackendContext, RecallRequest
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

    def search(self, request: RecallRequest) -> JsonObject:
        # ``browse`` and ``scroll`` keep the canonical JSONL behavior
        # (nothing to fuse). Only ``search`` is hybrid.
        summaries = self.candidate_session_summaries(request)
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
        fts_result = self._fts.search(over_fetched)
        vector_result = self._vector.search(over_fetched)

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
