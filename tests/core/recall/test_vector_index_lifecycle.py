"""The ``vector`` backend's derived index: lazy refresh, reuse, pruning and recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import _passage_catalog
from core.recall.passages import build_session_passages
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.recall.recall_test_support import (
    StubEmbeddings,
    passage_rows,
    request,
    timestamp,
    vector_backend,
)

pytestmark = pytest.mark.asyncio


async def test_first_search_embeds_the_scope_and_later_searches_reuse_it(
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

    first = await recall.search_page(request("carrot"))
    # The query embeds first: it pins the live dimension and served model every
    # document vector of the search must match.
    assert embeddings.embed_purposes == ["query", "document"]
    assert sorted(embeddings.document_inputs) == [
        "Bananas and other fruit are tasty",
        "I bought some carrots",
    ]
    assert first.hits[0].session_id == "carrots"

    second = await recall.search_page(request("fruit"))
    assert embeddings.embed_purposes == ["query", "document", "query"]
    assert second.hits[0].session_id == "fruit"


async def test_append_embeds_only_new_passages_and_surfaces_them(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="growing")
    for day in range(1, 8):
        session.append(ChatMessage.user(f"turn {day} lorem ipsum " * 150, timestamp=timestamp(day)))
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    first = await recall.search_page(request("lorem", limit=2))
    old_passages = build_session_passages(session.load_active())
    before = passage_rows(recall.store.path, "coder", "growing")
    assert sorted(before.values()) == sorted(passage.text for passage in old_passages)

    session.append(ChatMessage.user("brand new banana content " * 20, timestamp=timestamp(8)))
    documents_before = len(embeddings.document_inputs)
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
    after = passage_rows(recall.store.path, "coder", "growing")
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
    await recall.search_page(request("car"))
    old_passages = build_session_passages(session.load_active())

    session.apply_edit(
        target.id, [ChatMessage.user("I bought some carrots", timestamp=timestamp(4))]
    )
    documents_before = len(embeddings.document_inputs)
    page = await recall.search_page(request("car"))

    new_passages = build_session_passages(session.load_active())
    rows = passage_rows(recall.store.path, "coder", "edited")
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
    recall = vector_backend(tmp_path, sessions, embeddings=StubEmbeddings())
    await recall.search_page(request("fruit"))

    filtered = await recall.search_page(request("fruit", session_id="fruit"))
    assert {hit.session_id for hit in filtered.hits} == {"fruit"}
    assert set(await recall.store.list_indexed_sessions("coder")) == {"carrots", "fruit"}

    # Pruning follows the complete live scope, not the request's Session filter.
    sessions.delete(SessionAddress(project_id=None, agent_id="coder", session_id="carrots"))
    await recall.search_page(request("fruit", session_id="fruit"))

    assert set(await recall.store.list_indexed_sessions("coder")) == {"fruit"}
    assert passage_rows(recall.store.path, "coder", "carrots") == {}
    page = await recall.search_page(request("carrot"))
    assert {hit.session_id for hit in page.hits} == {"fruit"}


async def test_fork_reuses_its_origins_stored_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    source = sessions.create("coder", session_id="source")
    for day in range(1, 4):
        source.append(ChatMessage.user(f"fruit story {day} " * 120, timestamp=timestamp(day)))
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    first = await recall.search_page(request("fruit", limit=20))
    documents_before = len(embeddings.document_inputs)

    fork = await sessions.fork(source.address)
    page = await recall.search_page(request("fruit", limit=20))

    assert embeddings.document_inputs[documents_before:] == []
    assert passage_rows(recall.store.path, "coder", fork.id) == passage_rows(
        recall.store.path, "coder", "source"
    )
    # The origin owns the shared history and is eligible, so it keeps every hit.
    assert [(hit.session_id, hit.passage_id) for hit in page.hits] == [
        (hit.session_id, hit.passage_id) for hit in first.hits
    ]


async def test_excluded_session_is_not_embedded_until_it_is_included(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    current = sessions.create("coder", session_id="current")
    current.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))
    sessions.create("coder", session_id="other").append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(2))
    )
    embeddings = StubEmbeddings()
    recall = vector_backend(tmp_path, sessions, embeddings=embeddings)
    excluding = request("fruit", excluded_session_ids=("current",))

    first = await recall.search_page(excluding)
    current.append(ChatMessage.user("more fruit talk", timestamp=timestamp(3)))
    documents_before = len(embeddings.document_inputs)
    second = await recall.search_page(excluding)

    assert set(await recall.store.list_indexed_sessions("coder")) == {"other"}
    assert embeddings.document_inputs[documents_before:] == []
    assert "current" not in {hit.session_id for hit in (*first.hits, *second.hits)}

    included = await recall.search_page(request("fruit"))

    assert set(await recall.store.list_indexed_sessions("coder")) == {"current", "other"}
    assert included.hits[0].session_id == "current"
    assert "more fruit talk" in included.hits[0].text


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
    assert builds == [1]
    assert set(await recall.store.list_indexed_sessions("coder")) == {"inert"}


async def test_session_that_stops_yielding_passages_loses_its_rows(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = sessions.create("coder", session_id="becomes-empty")
    session.append(ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1)))
    recall = vector_backend(tmp_path, sessions, embeddings=StubEmbeddings())
    first = await recall.search_page(request("fruit"))
    assert [hit.session_id for hit in first.hits] == ["becomes-empty"]

    # A changed history that no longer yields Passages (as under a stricter policy).
    session.append(ChatMessage.user("still here, but inert", timestamp=timestamp(2)))
    monkeypatch.setattr(_passage_catalog, "build_session_passages", lambda _messages: [])
    second = await recall.search_page(request("fruit"))

    assert second.hits == ()
    assert passage_rows(recall.store.path, "coder", "becomes-empty") == {}


async def test_corrupt_index_is_discarded_once_and_rebuilt(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("banana fruit", timestamp=timestamp(1))
    )
    recall = vector_backend(tmp_path, sessions, embeddings=StubEmbeddings())
    await recall.search_page(request("fruit"))
    recall.store.path.write_bytes(b"not a sqlite database")

    page = await recall.search_page(request("fruit"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert await recall.store.read_header() is not None
