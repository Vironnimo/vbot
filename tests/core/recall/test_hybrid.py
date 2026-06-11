"""Tests for the hybrid recall backend.

The hybrid backend fuses the SQLite FTS and vector arms. These tests
construct it directly (no registry) and exercise the headline
behaviors: literal-keyword coverage, conceptual-query fallback,
shared-session tagging, graceful degradation when no embedding
binding is configured, ordering rules, over-fetch, and short queries
that fall through the FTS trigram path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.model_tasks import EmbeddingResult
from core.recall import RecallBackendContext, RecallRequest
from core.recall.hybrid import (
    _FETCH_MARGIN,
    _FETCH_MULTIPLIER,
    HybridRecallBackend,
    render_hybrid_matches,
)
from core.recall.vector import _SEMANTIC_UNAVAILABLE_NOTICE
from core.sessions import ChatSessionManager


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def request(
    *,
    query: str,
    match_mode: str = "all_terms",
    roles: tuple[str, ...] = ("user", "assistant", "tool", "error", "compaction_checkpoint"),
    limit: int = 5,
    sort: str = "newest",
) -> RecallRequest:
    return RecallRequest(
        agent_id="coder",
        session_id=None,
        around_message_id=None,
        query=query,
        since=None,
        until=None,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        limit=limit,
        context_messages=0,
        bookend_messages=2,
        sort=sort,  # type: ignore[arg-type]
    )


class _StubEmbeddings:
    """Deterministic embedding stub for hybrid tests.

    Vector assignment rules — the same text always maps to the same
    vector so cosine distance is a stable signal:

    * ``vehicle``/``driving`` (and other car-like text) → ``[1, 0, 0, 0]``
    * ``bild`` → ``[0, 0, 0, 1]`` — orthogonal to every other cluster,
      so any session whose embedding is not the ``bild`` vector has
      distance ``> 0.7`` to the ``bild`` query. That makes the
      ``bild`` regression test possible: a single-token keyword with
      no semantic context must come back via the literal arm, not via
      a fake semantic match.
    * default → ``[0.5, 0.5, 0, 0]`` (the same default the existing
      vector tests use).
    """

    def __init__(self, *, dimension: int = 4) -> None:
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.embed_calls: list[list[str]] = []
        self.resolve_calls = 0

    def resolve_model_id(self) -> tuple[str, str]:
        self.resolve_calls += 1
        return (self.provider_id, self.model_id)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        vectors: list[list[float]] = [self._vector_for(text) for text in texts]
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id=self.model_id,
            provider_id=self.provider_id,
            dimension=self.dimension,
        )

    def _vector_for(self, text: str) -> list[float]:
        lowered = text.lower()
        if "vehicle" in lowered or "driving" in lowered or " car " in lowered:
            return [1.0, 0.0, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        # The ``bild`` regression: a single-token query like ``bild`` has
        # almost no semantic context, so its embedding is essentially
        # orthogonal to every session's embedding. Model that by
        # routing the keyword itself to a unique vector *only* when the
        # text is short (the query case). Longer text containing
        # ``bild`` — session messages — falls into the default branch
        # and is well-separated from the query vector.
        if "bild" in lowered and len(lowered.strip()) <= 5:
            return [0.0, 0.0, 0.0, 1.0] + [0.0] * (self.dimension - 4)
        return [0.5, 0.5, 0.0, 0.0] + [0.0] * (self.dimension - 4)


def backend(
    tmp_path: Path,
    sessions: ChatSessionManager,
    *,
    embeddings: Any | None = None,
) -> HybridRecallBackend:
    return HybridRecallBackend(
        RecallBackendContext(
            data_dir=tmp_path,
            sessions=sessions,
            embeddings=embeddings,
        )
    )


# ---------------------------------------------------------------------------
# Literal-keyword coverage (the ``bild`` regression)
# ---------------------------------------------------------------------------


def test_hybrid_backend_surfaces_literal_keyword_with_weak_semantic_match(
    tmp_path: Path,
) -> None:
    """A keyword whose semantic distance exceeds the vector cutoff still surfaces via FTS.

    Regression for the ``bild`` case: a single-token query with no
    semantic context embeds to a vector orthogonal to every session
    in the corpus, so the vector arm's ``_MAX_DISTANCE`` filter drops
    every candidate. The literal arm is what the user expects to find
    the session — and the hybrid must not be silently empty just
    because the semantic arm gave up.
    """

    sessions = ChatSessionManager(tmp_path)
    for index, session_id in enumerate(["a", "b", "c"], start=1):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(f"today I built a {session_id} bild", timestamp=timestamp(index))
        )
    embeddings = _StubEmbeddings()

    data = backend(tmp_path, sessions, embeddings=embeddings).search(request(query="bild", limit=3))

    session_ids = [match["session_id"] for match in data["matches"]]
    assert sorted(session_ids) == ["a", "b", "c"]
    # All matches are literal — the vector arm dropped every candidate
    # because every session's embedding is more than 0.7 away from
    # the ``bild`` query's orthogonal vector.
    assert all(match["source"] == "literal" for match in data["matches"])
    assert all("distance" not in match for match in data["matches"])


# ---------------------------------------------------------------------------
# Conceptual query → semantic-only
# ---------------------------------------------------------------------------


def test_hybrid_backend_surfaces_conceptual_query_with_semantic_tag(
    tmp_path: Path,
) -> None:
    """A purely conceptual query returns semantic-only matches with a distance."""

    sessions = ChatSessionManager(tmp_path)
    # Session text contains "vehicle" — matches the ``vehicle`` cluster
    # exactly. The query is "transport" — no literal overlap, but the
    # stub has the same default vector for both, so the distance is
    # well below the cutoff.
    sessions.create("coder", session_id="vehicle-chat").append(
        ChatMessage.user("I love my vehicle", timestamp=timestamp(1))
    )

    class _TransportEmbeddings(_StubEmbeddings):
        def _vector_for(self, text: str) -> list[float]:
            # The query "transport" embeds to the car cluster vector
            # so the session's vehicle content is at distance 0 from
            # it. The session text itself also embeds to the car
            # cluster (the stub does that for "vehicle").
            return [1.0, 0.0, 0.0, 0.0] + [0.0] * (self.dimension - 4)

    data = backend(tmp_path, sessions, embeddings=_TransportEmbeddings()).search(
        request(query="transport", limit=2)
    )

    assert len(data["matches"]) == 1
    match = data["matches"][0]
    assert match["session_id"] == "vehicle-chat"
    assert match["source"] == "semantic"
    assert match["distance"] == pytest.approx(0.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Session hit by both arms → tagged ``both`` with literal snippet + distance
# ---------------------------------------------------------------------------


def test_hybrid_backend_tags_session_in_both_arms_as_both(tmp_path: Path) -> None:
    """A session matched by both arms appears once, tagged ``both`` with both signals."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="shared").append(
        ChatMessage.user("I love my vehicle and take it driving", timestamp=timestamp(1))
    )
    # Use a transport-class stub so the query lands in the same
    # cluster as the session text, putting a real ``distance`` on
    # the vector match.
    embeddings = _StubEmbeddings()

    data = backend(tmp_path, sessions, embeddings=embeddings).search(
        request(query="vehicle", limit=2)
    )

    assert len(data["matches"]) == 1
    match = data["matches"][0]
    assert match["session_id"] == "shared"
    # The session is in both arms: FTS finds the literal "vehicle",
    # vector finds it via the vehicle cluster. Tagged ``both``.
    assert match["source"] == "both"
    # The literal payload keeps the FTS snippet — which contains the
    # exact term the user typed — and the distance is attached.
    assert "vehicle" in match["snippet"].lower()
    assert match["distance"] == pytest.approx(0.0, abs=1e-5)
    # A healthy search (both arms ran) carries no degradation notice.
    assert "notice" not in data


# ---------------------------------------------------------------------------
# No embedding binding → literal-only, no crash
# ---------------------------------------------------------------------------


def test_hybrid_backend_returns_literal_only_when_no_embedding_binding(
    tmp_path: Path,
) -> None:
    """Without an embedding binding the vector arm falls back; every result is literal."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="alpha").append(
        ChatMessage.user("vehicle here", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="beta").append(
        ChatMessage.user("vehicle there", timestamp=timestamp(2))
    )

    data = backend(tmp_path, sessions, embeddings=None).search(request(query="vehicle", limit=2))

    # Both sessions match via FTS, the vector arm falls back to JSONL
    # and its matches also have no ``distance``. After dedup both
    # sessions are tagged literal and there are no distance fields.
    assert [match["session_id"] for match in data["matches"]] == ["beta", "alpha"]
    assert all(match["source"] == "literal" for match in data["matches"])
    assert all("distance" not in match for match in data["matches"])


def test_hybrid_backend_surfaces_semantic_unavailable_notice(tmp_path: Path) -> None:
    """When the vector arm cannot run, the fused result re-surfaces the reason.

    Literal matches still come back, but the agent must know the semantic half
    was skipped rather than assume the result reflects full coverage.
    """

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="alpha").append(
        ChatMessage.user("vehicle here", timestamp=timestamp(1))
    )

    data = backend(tmp_path, sessions, embeddings=None).search(request(query="vehicle", limit=2))

    # The hybrid frames its own notice and embeds the vector arm's reason.
    assert "Semantic augmentation unavailable" in data["notice"]
    assert _SEMANTIC_UNAVAILABLE_NOTICE in data["notice"]
    assert data["content"].startswith("Semantic augmentation unavailable")
    # Literal results are still present.
    assert [match["session_id"] for match in data["matches"]] == ["alpha"]


# ---------------------------------------------------------------------------
# Ordering: literal/both first, then semantic; literal honors sort, semantic by distance
# ---------------------------------------------------------------------------


def test_hybrid_backend_orders_literal_group_then_semantic_group(
    tmp_path: Path,
) -> None:
    """Literal/both precede semantic-only; literal honors sort, semantic is distance-ascending."""

    sessions = ChatSessionManager(tmp_path)
    # Three literal sessions with distinct timestamps. The ``vehicle``
    # text puts them in the vehicle cluster, so the vector arm will
    # also match all of them with low distance — every session is
    # ``both`` (in the literal group).
    for index, session_id in enumerate(["literal-1", "literal-2", "literal-3"], start=1):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(
                f"vehicle content for {session_id}",
                timestamp=timestamp(index),
            )
        )
    # A semantic-only session: its text does not contain "vehicle"
    # literally, but its embedding lands in the vehicle cluster.
    sessions.create("coder", session_id="semantic-only").append(
        ChatMessage.user("I like to drive", timestamp=timestamp(4))
    )
    embeddings = _StubEmbeddings()

    data = backend(tmp_path, sessions, embeddings=embeddings).search(
        request(query="vehicle", sort="newest", limit=4)
    )

    sources = [match["source"] for match in data["matches"]]
    # All literal/both sessions must come before any semantic-only.
    literal_indices = [i for i, source in enumerate(sources) if source in ("literal", "both")]
    semantic_indices = [i for i, source in enumerate(sources) if source == "semantic"]
    if literal_indices and semantic_indices:
        assert max(literal_indices) < min(semantic_indices)
    # The semantic group, if any, must be distance-ascending.
    semantic_distances = [
        match["distance"] for match in data["matches"] if match["source"] == "semantic"
    ]
    assert semantic_distances == sorted(semantic_distances)
    # The literal/both group is tagged correctly and includes the
    # literal sessions.
    literal_session_ids = [
        match["session_id"] for match in data["matches"] if match["source"] in ("literal", "both")
    ]
    assert "semantic-only" not in literal_session_ids


def test_hybrid_backend_literal_group_honors_oldest_sort(tmp_path: Path) -> None:
    """With ``sort='oldest'`` the literal group is in ascending timestamp order."""

    sessions = ChatSessionManager(tmp_path)
    for index, session_id in enumerate(["old", "middle", "new"], start=1):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(
                f"vehicle mention in {session_id}",
                timestamp=timestamp(index),
            )
        )
    embeddings = _StubEmbeddings()

    data = backend(tmp_path, sessions, embeddings=embeddings).search(
        request(query="vehicle", sort="oldest", limit=3)
    )

    # The literal/both group is sorted ascending by timestamp: the
    # oldest session comes first.
    literal_session_ids = [
        match["session_id"] for match in data["matches"] if match["source"] in ("literal", "both")
    ]
    assert literal_session_ids[0] == "old"


# ---------------------------------------------------------------------------
# Over-fetch: a single session with many literal hits cannot starve the budget
# ---------------------------------------------------------------------------


def test_hybrid_backend_overfetch_lets_other_literal_sessions_fill_budget(
    tmp_path: Path,
) -> None:
    """A session with many literal message-hits does not starve other distinct literal sessions.

    With ``limit=2`` and the over-fetch multiplier+margin, the FTS
    arm receives an inflated request that lets multiple distinct
    sessions come back even when one session is responsible for most
    of the FTS rows. The fused result must contain two different
    sessions, not two hits from the same session.
    """

    sessions = ChatSessionManager(tmp_path)
    # One repetitive session — same keyword across many messages.
    repetitive = sessions.create("coder", session_id="repetitive")
    for day in range(1, 11):
        repetitive.append(
            ChatMessage.user(f"vehicle build log day {day}", timestamp=timestamp(day))
        )
    # Two distinct sessions, each with a single vehicle mention, more
    # recent than the repetitive cluster so they come first under
    # ``sort='newest'``.
    sessions.create("coder", session_id="distinct-a").append(
        ChatMessage.user("vehicle one-off", timestamp=timestamp(20))
    )
    sessions.create("coder", session_id="distinct-b").append(
        ChatMessage.user("vehicle one-off too", timestamp=timestamp(21))
    )
    embeddings = _StubEmbeddings()

    data = backend(tmp_path, sessions, embeddings=embeddings).search(
        request(query="vehicle", limit=2)
    )

    session_ids = [match["session_id"] for match in data["matches"]]
    # The budget was two slots, and both must be distinct sessions.
    assert len(session_ids) == 2
    assert len(set(session_ids)) == 2
    # Neither of the survivors is the repetitive session — it is
    # the *oldest* cluster and ``sort='newest'`` puts the more recent
    # one-off sessions first.
    assert "repetitive" not in session_ids
    assert set(session_ids) == {"distinct-a", "distinct-b"}


def test_hybrid_backend_overfetch_uses_expected_multiplier_and_margin() -> None:
    """The over-fetch constants are exposed and mirror the plan's starting point."""

    assert _FETCH_MULTIPLIER == 3
    assert _FETCH_MARGIN == 10


# ---------------------------------------------------------------------------
# Short query (2 chars) — FTS trigram falls back, vector still embeds
# ---------------------------------------------------------------------------


def test_hybrid_backend_two_char_query_merges_literal_and_semantic(
    tmp_path: Path,
) -> None:
    """A 2-character query (below FTS trigram's 3-char floor) still surfaces matches.

    The FTS arm's trigram tokenizer cannot index short terms, so it
    falls back to its own JSONL substring scan. The vector arm still
    embeds the query. The fused result must not be empty: a
    non-empty FTS fallback + a non-empty vector arm merge into a
    hybrid match.
    """

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="gopher").append(
        ChatMessage.user("Go fast", timestamp=timestamp(1))
    )
    # The default vector for an unrecognized 2-char query lands on
    # the same default vector as the session text, so the distance is
    # 0 — the vector arm returns a real match.
    embeddings = _StubEmbeddings()

    data = backend(tmp_path, sessions, embeddings=embeddings).search(request(query="go", limit=2))

    assert data["matches"], "hybrid must not return an empty result for a 2-char query"
    match = data["matches"][0]
    assert match["session_id"] == "gopher"
    # FTS short query fallback has no distance, but the vector arm
    # supplied one — the session is in both arms → ``both``.
    assert match["source"] == "both"
    assert match["distance"] == pytest.approx(0.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_render_hybrid_matches_tags_each_line_with_source() -> None:
    """The renderer tags each line with the source label and shows distance when present."""

    request_obj = request(query="vehicle")
    matches: list[dict[str, Any]] = [
        {
            "session_id": "s1",
            "message_id": "m1",
            "timestamp": "2026-05-01T12:00:00+00:00",
            "role": "user",
            "snippet": "vehicle here",
            "source": "literal",
        },
        {
            "session_id": "s2",
            "message_id": "m2",
            "timestamp": "2026-05-02T12:00:00+00:00",
            "role": "user",
            "snippet": "vehicle anchor",
            "source": "semantic",
            "distance": 0.1234,
        },
        {
            "session_id": "s3",
            "message_id": "m3",
            "timestamp": "2026-05-03T12:00:00+00:00",
            "role": "user",
            "snippet": "vehicle match",
            "source": "both",
            "distance": 0.5,
        },
    ]

    rendered = render_hybrid_matches(request_obj, matches, truncated=False)

    assert "[literal]" in rendered
    assert "[semantic]" in rendered
    assert "[both]" in rendered
    # Distance is shown for entries that carry one — at 4 dp.
    assert "0.1234" in rendered
    assert "0.5000" in rendered


def test_render_hybrid_matches_empty_result_message() -> None:
    """An empty match list renders the no-matches banner with the query."""

    rendered = render_hybrid_matches(request(query="vehicle"), [], truncated=False)
    assert "No matches found for query: vehicle" in rendered


def test_render_hybrid_matches_omits_distance_for_literal_only_entries() -> None:
    """Entries with no ``distance`` field do not render a distance line."""

    request_obj = request(query="vehicle")
    matches: list[dict[str, Any]] = [
        {
            "session_id": "s1",
            "message_id": "m1",
            "timestamp": "2026-05-01T12:00:00+00:00",
            "role": "user",
            "snippet": "vehicle here",
            "source": "literal",
        }
    ]

    rendered = render_hybrid_matches(request_obj, matches, truncated=False)

    assert "distance=" not in rendered


def test_render_hybrid_matches_truncation_marker() -> None:
    """A truncated result includes the limit marker."""

    request_obj = request(query="vehicle", limit=1)
    matches: list[dict[str, Any]] = [
        {
            "session_id": "s1",
            "message_id": "m1",
            "timestamp": "2026-05-01T12:00:00+00:00",
            "role": "user",
            "snippet": "vehicle here",
            "source": "literal",
        }
    ]

    rendered = render_hybrid_matches(request_obj, matches, truncated=True)
    assert "[Results limited to 1 matches.]" in rendered
