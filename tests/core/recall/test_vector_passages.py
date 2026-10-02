"""The ``vector`` backend's hits are canonical conversation Passages."""

from __future__ import annotations

import pytest

from core.chat import ChatMessage
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
