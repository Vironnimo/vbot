"""The semantic index lifecycle: reuse, pruning and recovery across indexing passes and searches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import _passage_catalog
from core.recall.passages import build_session_passages
from core.recall.vector import SEMANTIC_PARTIAL_REASON
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.recall.recall_test_support import (
    StubEmbeddings,
    embed_documents,
    passage_rows,
    request,
    timestamp,
    vector_backend,
)

pytestmark = pytest.mark.asyncio


async def test_indexing_embeds_documents_once_and_searches_embed_only_queries(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="carrots").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="fruit").append(
        ChatMessage.user("Bananas and other fruit are tasty", timestamp=timestamp(2))
    )
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)

    await embed_documents(recall.index, sessions, embeddings)
    first = await recall.search_page(request("carrot"))
    await embed_documents(recall.index, sessions, embeddings)
    second = await recall.search_page(request("fruit"))

    # Newest first: the most recent conversation is searchable soonest.
    assert embeddings.document_inputs == [
        "Bananas and other fruit are tasty",
        "I bought some carrots",
    ]
    assert embeddings.embed_purposes == ["document", "query", "query"]
    assert first.hits[0].session_id == "carrots"
    assert second.hits[0].session_id == "fruit"


async def test_append_embeds_only_new_passages_and_surfaces_them(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="growing")
    for day in range(1, 8):
        session.append(ChatMessage.user(f"turn {day} lorem ipsum " * 150, timestamp=timestamp(day)))
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    first = await recall.search_page(request("lorem", limit=2))
    old_passages = build_session_passages(session.load_active())
    before = passage_rows(recall.index.path, "coder", "growing")
    assert sorted(before.values()) == sorted(passage.text for passage in old_passages)

    session.append(ChatMessage.user("brand new banana content " * 20, timestamp=timestamp(8)))
    documents_before = len(embeddings.document_inputs)
    await embed_documents(recall.index, sessions, embeddings)
    page = await recall.search_page(request("banana", limit=2))

    # The former tail Passage may grow and new Passages cover the appended turn;
    # only texts without a stored vector reach the provider.
    new_passages = build_session_passages(session.load_active())
    added = [passage for passage in new_passages if passage not in old_passages]
    vanished = [passage for passage in old_passages if passage not in new_passages]
    embedded = embeddings.document_inputs[documents_before:]
    assert sorted(embedded) == sorted(
        {passage.text for passage in added} - {passage.text for passage in old_passages}
    )
    assert 0 < len(embedded) < len(new_passages)
    after = passage_rows(recall.index.path, "coder", "growing")
    assert sorted(after.values()) == sorted(passage.text for passage in new_passages)
    # Every unchanged Passage keeps its row; only vanished Passages lose theirs.
    assert len(set(before.items()) & set(after.items())) == len(old_passages) - len(vanished)
    assert "brand new banana" in page.hits[0].text
    assert page.snapshot_id != first.snapshot_id


async def test_history_edit_replaces_changed_and_vanished_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="edited")
    session.append(ChatMessage.user("I love bananas and fruit " * 80, timestamp=timestamp(1)))
    target = ChatMessage.user("My car broke down " * 80, timestamp=timestamp(2))
    session.append(target)
    session.append(ChatMessage.user("car repair advice " * 150, timestamp=timestamp(3)))
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    old_passages = build_session_passages(session.load_active())

    session.apply_edit(
        target.id, [ChatMessage.user("I bought some carrots", timestamp=timestamp(4))]
    )
    documents_before = len(embeddings.document_inputs)
    await embed_documents(recall.index, sessions, embeddings)
    page = await recall.search_page(request("car"))

    new_passages = build_session_passages(session.load_active())
    rows = passage_rows(recall.index.path, "coder", "edited")
    assert sorted(rows.values()) == sorted(passage.text for passage in new_passages)
    assert not any("broke down" in text or "repair" in text for text in rows.values())
    assert all("broke down" not in hit.text and "repair" not in hit.text for hit in page.hits)
    embedded = embeddings.document_inputs[documents_before:]
    assert sorted(embedded) == sorted(
        {passage.text for passage in new_passages if passage not in old_passages}
        - {passage.text for passage in old_passages}
    )
    assert len(embedded) < len(new_passages)


async def test_filtered_search_keeps_other_sessions_and_prunes_the_whole_scope(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    for session_id, text in (("carrots", "I bought some carrots"), ("fruit", "Fruit is tasty")):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(text, timestamp=timestamp(1))
        )
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)

    filtered = await recall.search_page(request("fruit", session_id="fruit"))
    assert {hit.session_id for hit in filtered.hits} == {"fruit"}
    assert set(await recall.index.list_indexed_sessions("coder")) == {"carrots", "fruit"}

    # Pruning follows the complete live scope, not the request's Session filter.
    sessions.delete(SessionAddress(project_id=None, agent_id="coder", session_id="carrots"))
    await recall.search_page(request("fruit", session_id="fruit"))

    assert set(await recall.index.list_indexed_sessions("coder")) == {"fruit"}
    assert passage_rows(recall.index.path, "coder", "carrots") == {}
    page = await recall.search_page(request("carrot"))
    assert {hit.session_id for hit in page.hits} == {"fruit"}


async def test_fork_reuses_its_origins_stored_vectors(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    source = sessions.create("coder", session_id="source")
    for day in range(1, 4):
        source.append(ChatMessage.user(f"fruit story {day} " * 120, timestamp=timestamp(day)))
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    first = await recall.search_page(request("fruit", limit=20))
    documents_before = len(embeddings.document_inputs)

    fork = await sessions.fork(source.address)
    await embed_documents(recall.index, sessions, embeddings)
    page = await recall.search_page(request("fruit", limit=20))

    assert embeddings.document_inputs[documents_before:] == []
    assert passage_rows(recall.index.path, "coder", fork.id) == passage_rows(
        recall.index.path, "coder", "source"
    )
    # The origin owns the shared history and is eligible, so it keeps every hit.
    assert [(hit.session_id, hit.passage_id) for hit in page.hits] == [
        (hit.session_id, hit.passage_id) for hit in first.hits
    ]


async def test_session_without_passages_is_stamped_and_not_reread(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Notes are not conversation text, so this Session yields no Passages; on a
    # brand-new index the search must still answer.
    sessions.create("coder", session_id="inert").append(
        ChatMessage.note("I bought some carrots", timestamp=timestamp(1))
    )
    builds: list[int] = []

    def counting_build(messages: list[ChatMessage]) -> Any:
        builds.append(len(messages))
        return build_session_passages(messages)

    monkeypatch.setattr(_passage_catalog, "build_session_passages", counting_build)
    recall = vector_backend(tmp_path, sessions, embeddings=StubEmbeddings())

    first = await recall.search_page(request("carrot"))
    await recall.search_page(request("carrot"))

    assert first.hits == ()
    assert first.degraded is False
    assert builds == [1]
    assert set(await recall.index.list_indexed_sessions("coder")) == {"inert"}


async def test_session_that_stops_yielding_passages_loses_its_rows(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = sessions.create("coder", session_id="becomes-empty")
    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    first = await recall.search_page(request("fruit"))
    assert [hit.session_id for hit in first.hits] == ["becomes-empty"]

    # A changed history that no longer yields Passages (as under a stricter policy).
    session.append(ChatMessage.user("still here, but inert", timestamp=timestamp(2)))
    monkeypatch.setattr(_passage_catalog, "build_session_passages", lambda _messages: [])
    second = await recall.search_page(request("fruit"))

    assert second.hits == ()
    assert passage_rows(recall.index.path, "coder", "becomes-empty") == {}


async def test_corrupt_index_is_discarded_once_and_rebuilt(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    await embed_documents(recall.index, sessions, embeddings)
    await recall.search_page(request("fruit"))
    recall.index.path.write_bytes(b"not a sqlite database")

    rebuilt = await recall.search_page(request("fruit"))

    # The search rebuilt the catalog; its vectors wait for the next indexing pass.
    assert (rebuilt.hits, rebuilt.degradation_reason) == ((), SEMANTIC_PARTIAL_REASON)
    assert await recall.index.read_header() is not None
    await embed_documents(recall.index, sessions, embeddings)
    page = await recall.search_page(request("fruit"))
    assert [hit.session_id for hit in page.hits] == ["one"]
