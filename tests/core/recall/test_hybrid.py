"""The ``hybrid`` backend: Reciprocal Rank Fusion of the literal and semantic Passage arms."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import (
    RecallBackendContext,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
    hybrid,
)
from core.recall.canonical import RecallScope
from core.recall.hybrid import HybridRecallBackend
from core.sessions import ChatSessionManager
from tests.core.recall.recall_test_support import (
    StubEmbeddings,
    forbid_database_calls_on_loop,
    forbid_event_loop_calls,
    request,
    timestamp,
)

pytestmark = pytest.mark.asyncio


def backend(
    tmp_path: Path, sessions: ChatSessionManager, *, embeddings: Any | None = None
) -> HybridRecallBackend:
    return HybridRecallBackend(
        RecallBackendContext(data_dir=tmp_path, sessions=sessions, embeddings=embeddings)
    )


async def test_typed_hybrid_search_uses_passage_rrf_and_source_membership(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="both").append(
        ChatMessage.user("I was driving today", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="semantic").append(
        ChatMessage.user("vehicle maintenance", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=StubEmbeddings())

    page = await recall.search_page(request("driving"))

    assert page.result_type == "passage"
    assert page.ranking == "reciprocal_rank_fusion"
    assert page.hits[0].session_id == "both"
    assert page.hits[0].sources == ("literal", "semantic")
    assert any(hit.session_id == "semantic" and hit.sources == ("semantic",) for hit in page.hits)


async def test_typed_hybrid_degrades_explicitly_to_literal_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="literal").append(
        ChatMessage.user("telegraminstallation", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)

    page = await recall.search_page(request("telegram"))

    assert [hit.session_id for hit in page.hits] == ["literal"]
    assert page.hits[0].sources == ("literal",)
    assert page.degraded is True
    assert page.degradation_reason is not None


async def test_typed_hybrid_keeps_multiple_passages_from_one_session(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="repeated").append(
        ChatMessage.user("needle context " * 400, timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=StubEmbeddings())

    page = await recall.search_page(request("needle"))

    repeated = [hit for hit in page.hits if hit.session_id == "repeated"]
    assert len(repeated) > 1
    assert len({hit.passage_id for hit in repeated}) == len(repeated)


class _WaitingArm:
    """Prepared arm whose ranking waits until the other arm ranks too."""

    def __init__(self, ranking: str, started: asyncio.Event, other: asyncio.Event) -> None:
        self._ranking = ranking
        self._started = started
        self._other = other

    async def page(self, offset: int, limit: int) -> RecallSearchPage:
        self._started.set()
        await self._other.wait()
        return RecallSearchPage((), "passage", self._ranking, f"{self._ranking}-snapshot", False, 0)


async def test_hybrid_arms_run_concurrently(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    recall = backend(tmp_path, sessions)
    literal_prepared = asyncio.Event()
    semantic_prepared = asyncio.Event()
    literal_ranked = asyncio.Event()
    semantic_ranked = asyncio.Event()

    async def literal(request: RecallSearchRequest, scope: RecallScope) -> _WaitingArm:
        literal_prepared.set()
        await semantic_prepared.wait()
        return _WaitingArm("literal", literal_ranked, semantic_ranked)

    async def semantic(request: RecallSearchRequest, scope: RecallScope) -> _WaitingArm:
        semantic_prepared.set()
        await literal_prepared.wait()
        return _WaitingArm("semantic", semantic_ranked, literal_ranked)

    monkeypatch.setattr(recall._fts, "prepare_passage_search", literal)
    monkeypatch.setattr(recall._vector, "prepare_search", semantic)
    page = await asyncio.wait_for(recall.search_page(request("query")), timeout=2)
    assert page.degraded is False
    assert page.hits == ()


async def test_hybrid_depth_growth_prepares_each_arm_once(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deeper fusion pass reruns only the rankings, never freshness or query embedding."""

    for index in range(3):
        sessions.create("coder", session_id=f"drive-{index}").append(
            ChatMessage.user(f"I was driving today {index}", timestamp=timestamp(index + 1))
        )
    embeddings = StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    stability_checks = 0

    def unstable_once(*args: Any) -> bool:
        nonlocal stability_checks
        stability_checks += 1
        return stability_checks > 1

    revisions = 0
    list_history_revisions = sessions.list_history_revisions

    def counting_revisions(agent_id: str, project_id: str | None = None) -> Any:
        nonlocal revisions
        revisions += 1
        return list_history_revisions(agent_id, project_id)

    syncs = 0
    refresh_passage_index = recall._fts._catalog.refresh

    async def counting_sync(*args: Any) -> None:
        nonlocal syncs
        syncs += 1
        await refresh_passage_index(*args)

    def reject_listing(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the scope read is the only Session listing of a search")

    monkeypatch.setattr(hybrid, "_rrf_page_is_stable", unstable_once)
    monkeypatch.setattr(sessions, "list_history_revisions", counting_revisions)
    monkeypatch.setattr(sessions, "list_summaries", reject_listing)
    monkeypatch.setattr(sessions, "list_history_versions", reject_listing)
    monkeypatch.setattr(recall._fts._catalog, "refresh", counting_sync)

    page = await recall.search_page(request("driving"))

    assert stability_checks == 2
    assert page.degraded is False
    assert {hit.session_id for hit in page.hits} == {"drive-0", "drive-1", "drive-2"}
    assert embeddings.embed_calls.count(["driving"]) == 1
    assert revisions == 1
    assert syncs == 1


async def test_hybrid_search_keeps_session_and_index_work_off_the_event_loop(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.create("coder", session_id="both").append(
        ChatMessage.user("I was driving today", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=StubEmbeddings())
    calls = forbid_event_loop_calls(monkeypatch, sessions._store)
    database_calls = forbid_database_calls_on_loop(monkeypatch)

    page = await recall.search_page(request("driving"))

    # An arm failure would only degrade the page, so both arms must have succeeded.
    assert page.degraded is False
    assert page.hits[0].sources == ("literal", "semantic")
    assert "list_history_revisions" in calls
    # Both arms refreshed and read their Passage indexes on the database worker pools.
    assert {
        "recall_index.read",
        "recall_index.write",
        "recall_vectors.read",
        "recall_vectors.write",
    } <= set(database_calls)


async def test_hybrid_short_query_retains_literal_and_semantic_sources(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="short").append(
        ChatMessage.user("Go fast", timestamp=timestamp(1))
    )
    page = await backend(tmp_path, sessions, embeddings=StubEmbeddings()).search_page(request("go"))
    assert page.hits[0].session_id == "short"
    assert page.hits[0].sources == ("literal", "semantic")


@pytest.mark.parametrize(("offset", "limit"), [(0, 10), (8, 2), (9, 1)])
async def test_hybrid_resolves_late_contributions_before_returning_selected_hits(
    tmp_path: Path,
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
    limit: int,
) -> None:
    literal = ["a", "b", *[f"shared-{index}" for index in range(1, 9)]]
    semantic = [
        *[f"shared-{index}" for index in range(1, 9)],
        *[f"filler-{index}" for index in range(9, 21)],
        "b",
        *[f"filler-{index}" for index in range(22, 40)],
        "a",
    ]

    class Ranking:
        def __init__(self, names: list[str]) -> None:
            self.names = names

        async def prepare(self, *_args: Any) -> Ranking:
            return self

        async def page(self, start: int, count: int) -> RecallSearchPage:
            return RecallSearchPage(
                hits=tuple(
                    RecallSearchHit(
                        "passage", "session", name, "user", "", name, 0, passage_id=name
                    )
                    for name in self.names[start : start + count]
                ),
                result_type="passage",
                ranking="fixture",
                snapshot_id="fixed",
                has_more=start + count < len(self.names),
                total_candidate_sessions=1,
            )

    recall = backend(tmp_path, sessions)
    monkeypatch.setattr(recall._fts, "prepare_passage_search", Ranking(literal).prepare)
    monkeypatch.setattr(recall._vector, "prepare_search", Ranking(semantic).prepare)
    try:
        page = await recall.search_page(request("query", limit=limit, offset=offset))
    finally:
        await recall.aclose()

    # The top ten members are already known at depth 20, but b's semantic
    # contribution at rank 21 must put it before a (whose other rank is 40).
    expected = [f"shared-{index}" for index in range(1, 9)] + ["b", "a"]
    assert [hit.message_id for hit in page.hits] == expected[offset : offset + limit]
    for hit in page.hits:
        assert hit.sources == ("literal", "semantic")
        assert hit.score == pytest.approx(
            1 / (61 + literal.index(hit.message_id)) + 1 / (61 + semantic.index(hit.message_id))
        )
