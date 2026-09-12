"""Vector: typed search behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.model_tasks import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingUsage,
)
from core.recall import (
    RecallSearchError,
    RecallSearchRequest,
)
from core.recall.vector import _EMBED_BATCH_SIZE
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.recall.vector_helpers import (
    _StubEmbeddings,
    backend,
    timestamp,
)

pytestmark = pytest.mark.asyncio


class _CapturingLogger:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, tuple[object, ...]]] = []
        self.warning_calls: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.info_calls.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warning_calls.append((message, args))


def search_request(
    query: str,
    *,
    limit: int = 10,
    since: datetime | None = None,
    session_id: str | None = None,
    offset: int = 0,
    snapshot_id: str | None = None,
) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        session_id=session_id,
        query=query,
        since=since,
        until=None,
        roles=("user", "assistant", "error", "compaction_checkpoint"),
        match_mode="all_terms",
        order="relevance",
        offset=offset,
        limit=limit,
        snapshot_id=snapshot_id,
    )


async def test_typed_vector_search_returns_ranked_passages_without_session_dedup(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="fruit-heavy").append(
        ChatMessage.user("fruit banana " * 400, timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit"))

    assert page.result_type == "passage"
    assert page.ranking == "cosine_distance"
    assert len(page.hits) > 1
    assert {hit.session_id for hit in page.hits} == {"fruit-heavy"}
    assert len({hit.passage_id for hit in page.hits}) == len(page.hits)
    assert all(hit.sources == ("semantic",) for hit in page.hits)


async def test_vector_search_skips_session_deleted_during_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="deleted-during-index")
    session.append(ChatMessage.user("banana fruit", timestamp=timestamp(1)))
    address = SessionAddress(
        project_id=None,
        agent_id="coder",
        session_id=session.id,
    )
    list_history_versions = sessions.list_history_versions
    deleted = False

    def list_then_delete(addresses):
        nonlocal deleted
        versions = list_history_versions(addresses)
        if not deleted:
            sessions.delete(address)
            deleted = True
        return versions

    monkeypatch.setattr(sessions, "list_history_versions", list_then_delete)
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit"))

    assert page.hits == ()


async def test_typed_vector_search_has_no_literal_fallback_or_distance_cutoff(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vegetable").append(
        ChatMessage.user("carrot vegetable", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit", limit=10))

    assert [hit.session_id for hit in page.hits] == ["fruit", "vegetable"]
    assert page.hits[-1].score > 0.7

    none_dir = tmp_path / "none"
    none_dir.mkdir()
    from core.sessions.format import write_bootstrap_marker

    write_bootstrap_marker(none_dir)
    unavailable = backend(none_dir, ChatSessionManager(none_dir))
    with pytest.raises(RecallSearchError):
        await unavailable.search_page(search_request("literal"))


async def test_typed_vector_search_prefilters_time_inside_knn(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="old").append(
        ChatMessage.user("banana fruit old", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="new").append(
        ChatMessage.user("banana fruit new", timestamp=timestamp(3))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    page = await recall.search_page(search_request("fruit", since=datetime(2026, 5, 2, tzinfo=UTC)))

    assert [hit.session_id for hit in page.hits] == ["new"]


async def test_typed_filtered_search_keeps_other_scope_sessions_indexed(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit one", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="two").append(
        ChatMessage.user("banana fruit two", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    await recall.search_page(search_request("fruit"))
    assert set(recall.store.list_indexed_sessions("coder")) == {"one", "two"}

    page = await recall.search_page(search_request("fruit", session_id="one"))

    assert {hit.session_id for hit in page.hits} == {"one"}
    assert set(recall.store.list_indexed_sessions("coder")) == {"one", "two"}


async def test_typed_search_rebuilds_full_scope_on_native_dimension_change(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    for session_id, day in (("one", 1), ("two", 2)):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(f"banana fruit {session_id}", timestamp=timestamp(day))
        )
    embeddings = _StubEmbeddings(dimension=4)
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(search_request("fruit"))

    embeddings.dimension = 6
    page = await recall.search_page(search_request("fruit"))

    assert {hit.session_id for hit in page.hits} == {"one", "two"}
    header = recall.store.read_header()
    assert header is not None
    assert header.dimension == 6
    assert set(recall.store.list_indexed_sessions("coder")) == {"one", "two"}


async def test_typed_search_rebuilds_when_execution_fingerprint_changes(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(search_request("fruit"))
    calls_before_switch = len(embeddings.embed_calls)

    embeddings.space_fingerprint = "stub-space-b"
    page = await recall.search_page(search_request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert len(embeddings.embed_calls) >= calls_before_switch + 2
    header = recall.store.read_header()
    assert header is not None
    assert header.space_fingerprint == "stub-space-b"


async def test_typed_search_discards_corrupt_index_once_and_rebuilds(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    await recall.search_page(search_request("fruit"))
    recall.store.path.write_bytes(b"not a sqlite database")

    page = await recall.search_page(search_request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert recall.store.read_header() is not None


async def test_typed_search_embeds_documents_and_query_with_explicit_purposes(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = _StubEmbeddings()
    recall = backend(tmp_path, sessions, embeddings=embeddings)

    await recall.search_page(search_request("fruit"))

    assert embeddings.embed_purposes == ["document", "query"]


async def test_typed_search_rebuilds_when_provider_response_model_changes(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = _StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    logger = _CapturingLogger()
    recall = backend(tmp_path, sessions, embeddings=embeddings, logger=logger)
    first_page = await recall.search_page(search_request("fruit"))
    continuation = await recall.search_page(
        search_request(
            "fruit",
            offset=1,
            snapshot_id=first_page.snapshot_id,
        )
    )

    embeddings.response_model_id = "served/embed-b"
    with pytest.raises(RecallSearchError) as error_info:
        await recall.search_page(
            search_request(
                "fruit",
                offset=1,
                snapshot_id=first_page.snapshot_id,
            )
        )
    page = await recall.search_page(search_request("fruit"))

    assert continuation.snapshot_id == first_page.snapshot_id
    assert error_info.value.code == "stale_cursor"
    assert [hit.session_id for hit in page.hits] == ["one"]
    header = recall.store.read_header()
    assert header is not None
    assert header.model_id == "stub-embed"
    assert header.response_model_id == "served/embed-b"
    assert any("response model changed" in message for message, _args in logger.warning_calls)


async def test_typed_search_rebuilds_when_response_model_drifts_during_backfill(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="one")
    session.append(ChatMessage.user("banana fruit", timestamp=timestamp(1)))
    embeddings = _StubEmbeddings()
    embeddings.response_model_id = "served/embed-a"
    recall = backend(tmp_path, sessions, embeddings=embeddings)
    await recall.search_page(search_request("fruit"))

    session.append(ChatMessage.user("another banana", timestamp=timestamp(2)))
    embeddings.response_model_id = "served/embed-b"
    page = await recall.search_page(search_request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    header = recall.store.read_header()
    assert header is not None
    assert header.response_model_id == "served/embed-b"


async def test_run_embed_rejects_actual_model_drift_between_batches(tmp_path: Path) -> None:
    class _DriftingEmbeddings(_StubEmbeddings):
        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            self.response_model_id = f"served/embed-{len(self.embed_calls)}"
            return await super().embed(texts, purpose=purpose)

    sessions = ChatSessionManager(tmp_path)
    recall = backend(tmp_path, sessions, embeddings=_DriftingEmbeddings())
    texts = [f"text-{index}" for index in range(_EMBED_BATCH_SIZE + 1)]

    with pytest.raises(EmbeddingError):
        await recall._run_embed(texts)


async def test_typed_search_logs_usage_aggregated_across_rebuild_batches(
    tmp_path: Path,
) -> None:
    class _UsageEmbeddings(_StubEmbeddings):
        async def embed(
            self,
            texts: list[str],
            *,
            purpose: str | None = None,
        ) -> EmbeddingResult:
            result = await super().embed(texts, purpose=purpose)
            return EmbeddingResult(
                vectors=result.vectors,
                model_id=result.model_id,
                provider_id=result.provider_id,
                dimension=result.dimension,
                space_fingerprint=result.space_fingerprint,
                response_model_id=result.response_model_id,
                usage=EmbeddingUsage(
                    requests=1,
                    token_reports=1,
                    cost_reports=1,
                    input_tokens=len(texts),
                    total_tokens=len(texts),
                    cost=0.01,
                ),
            )

    sessions = ChatSessionManager(tmp_path)
    for index in range(_EMBED_BATCH_SIZE + 1):
        sessions.create("coder", session_id=f"session-{index}").append(
            ChatMessage.user(f"banana fruit {index}", timestamp=timestamp(1))
        )
    logger = _CapturingLogger()
    recall = backend(tmp_path, sessions, embeddings=_UsageEmbeddings(), logger=logger)

    await recall.search_page(search_request("fruit"))

    assert len(logger.info_calls) == 1
    message, args = logger.info_calls[0]
    assert message.startswith("Embedding usage operation=")
    assert args == (
        "typed_search",
        "openrouter",
        "stub-embed",
        3,
        3,
        _EMBED_BATCH_SIZE + 2,
        _EMBED_BATCH_SIZE + 2,
        3,
        pytest.approx(0.03),
        1,
        _EMBED_BATCH_SIZE + 1,
    )
