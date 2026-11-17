"""The ``vector`` backend's hits are canonical conversation Passages."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.chat import ChatMessage
from core.recall import RecallSearchError
from core.sessions import ChatSessionManager
from tests.core.recall.recall_test_support import (
    ALL_ROLES,
    StubEmbeddings,
    VectorBackendFactory,
    embed_documents,
    request,
    timestamp,
)
from tests.core.sessions.history_fixtures import append_tool_fixture

pytestmark = pytest.mark.asyncio


async def test_match_in_the_middle_of_a_long_session_anchors_at_its_passage(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    session = sessions.create("coder", session_id="mixed")
    session.append(ChatMessage.user("My car broke down on the highway", timestamp=timestamp(1)))
    # Filler long enough to push the fruit message into its own Passage.
    for day in range(2, 5):
        session.append(
            ChatMessage.user("unrelated filler content " * 200, timestamp=timestamp(day))
        )
    last = ChatMessage.user("I love bananas and fruit", timestamp=timestamp(5))
    session.append(last)
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)

    page = await recall.search_page(request("fruit", limit=2))

    assert page.hits[0].session_id == "mixed"
    assert page.hits[0].end_message_id == last.id
    assert "fruit" in page.hits[0].text.lower()


async def test_equal_distances_keep_the_same_order_across_page_sizes(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    session = sessions.create("coder", session_id="repeated-text")
    for index in range(10):
        message = ChatMessage.user("car", timestamp=timestamp(1))
        session.append(replace(message, id=f"msg_{index:03d}"))
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    search = request("car", limit=10)
    whole = await recall.search_page(search)
    paged: list[str] = []
    for offset in range(0, 10, 2):
        page = await recall.search_page(
            replace(search, offset=offset, limit=2, snapshot_id=whole.snapshot_id)
        )
        paged.extend(hit.message_id for hit in page.hits)
        assert page.has_more is (offset < 8)
    assert len(set(paged)) == 10
    assert paged == [hit.message_id for hit in whole.hits]


@pytest.mark.parametrize("change", ["coverage", "rebuild"])
async def test_continuation_rejects_changes_in_vector_coverage_or_generation(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory, change: str
) -> None:
    session = sessions.create("coder", session_id="mixed")
    for day, text in enumerate(("banana", "vehicle", "car"), start=1):
        session.append(ChatMessage.user(text, timestamp=timestamp(day)))
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    search = request("car", limit=1)
    await recall.search_page(search)
    header = await recall.index.read_header()
    assert header is not None
    batch = await recall.index.pending_texts(limit=3)
    initial = {key: embeddings.vector_for(text) for key, text in batch if text != "car"}
    await recall.index.store_vectors(header, initial)
    first = await recall.search_page(search)
    assert first.hits[0].text == "vehicle"
    assert first.has_more and first.degraded

    if change == "coverage":
        await recall.index.store_vectors(
            header, {key: embeddings.vector_for(text) for key, text in batch if text == "car"}
        )
    else:
        await recall.index.reset_vectors()
        await recall.index.use_space(header)
        await recall.index.store_vectors(header, initial)

    with pytest.raises(RecallSearchError) as error:
        await recall.search_page(replace(search, offset=1, snapshot_id=first.snapshot_id))
    assert error.value.code == "stale_cursor"
    new_first = await recall.search_page(search)
    assert new_first.hits[0].text == ("car" if change == "coverage" else "vehicle")


async def test_tool_output_and_recall_results_never_become_hits(
    sessions: ChatSessionManager, vector_backend: VectorBackendFactory
) -> None:
    session = sessions.create("coder", session_id="mixed")
    # Tool text carries the query's meaning; only the conversation may surface.
    for call_id, name, content, day in (
        ("c1", "session_search", "I love bananas and fruit", 1),
        ("c2", "bash", "banana fruit raw ansi terminal dump", 2),
    ):
        append_tool_fixture(
            session,
            ChatMessage.tool(
                tool_call_id=call_id, name=name, content=content, timestamp=timestamp(day)
            ),
        )
    user = ChatMessage.user("I bought some carrots", timestamp=timestamp(3))
    session.append(user)
    embeddings = StubEmbeddings()
    recall = vector_backend(embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)

    page = await recall.search_page(request("fruit", roles=ALL_ROLES))

    assert [(hit.role, hit.end_message_id, hit.text) for hit in page.hits] == [
        ("user", user.id, "I bought some carrots")
    ]
