"""Session search: conversation-only hits, bounded context and visibility."""

from dataclasses import replace
from pathlib import Path
from typing import override

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.recall import (
    HybridRecallBackend,
    RecallBackendContext,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    SqliteFtsRecallBackend,
    VectorRecallBackend,
)
from core.runs import RunKind
from core.sessions import ChatSession, ChatSessionManager
from scripts.provider_probe.recall_cases import FixtureEmbeddings
from tests.core.recall.recall_test_support import StubEmbeddings, embed_documents
from tests.core.sessions.history_fixtures import admit_run, append_tool_fixture
from tests.core.tools.session_search_test_support import search, success

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


@pytest.mark.parametrize("passage_backend", [False, True], ids=["messages", "passages"])
@pytest.mark.parametrize("inherited", [False, True], ids=["own", "inherited"])
async def test_hit_includes_question_and_final_answer_without_loading_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passage_backend: bool, inherited: bool
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="target", project_id="p1")
    question = ChatMessage.user("Which backup policy did we choose?")
    interim = ChatMessage.assistant(
        model="test",
        content="Retention initially: seven days.",
        tool_calls=[ToolCall(id="call", name="bash")],
    )
    tool = ChatMessage.tool(
        tool_call_id="call", name="bash", content="private machine output" * 10000
    )
    answer = ChatMessage.assistant(model="test", content="Correction: keep thirty days.")
    session.append_many([question, interim, tool, answer])
    if inherited:
        session = await sessions.fork(session.address, target_project_id="p1")
    session.append(ChatMessage.user("Unrelated question"))
    sessions.create("coder", session_id="target").append(ChatMessage.user("Retention wrong scope"))

    def reject_load(*_args: object) -> None:
        raise AssertionError("Search must not load an entire transcript")

    monkeypatch.setattr(ChatSession, "load", reject_load)
    monkeypatch.setattr(ChatSession, "load_active", reject_load)
    context = RecallBackendContext(tmp_path, sessions)
    backend = HybridRecallBackend(context) if passage_backend else SqliteFtsRecallBackend(context)
    try:
        data = success(
            await search(
                tmp_path, {"query": "Retention", "session_id": session.id}, backend, project_id="p1"
            )
        )
        missing = success(await search(tmp_path, {"query": "absent"}, backend, project_id="p1"))
    finally:
        if isinstance(backend, HybridRecallBackend):
            backend.close()

    hit = data["items"][0]
    assert hit["message_id"] == interim.id
    assert [item["message_id"] for item in hit["context"]] == [question.id, answer.id]
    assert [item["text"] for item in hit["context"]] == [question.content, answer.content]
    assert "read_ref" not in hit
    assert hit["context_is_partial"] is True
    assert data["sessions"] == [{"agent_id": "coder", "session_id": session.id}]
    assert missing["items"] == []


async def test_only_conversation_text_matches_before_candidate_limit(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="sources")
    # Enough metadata-only matches to starve the visible hit if filtered after LIMIT.
    for _ in range(20):
        session.append(
            ChatMessage.assistant(
                model="test",
                content="ordinary answer",
                reasoning="needle metadata",
                tool_calls=[ToolCall(id="call", name="needle", arguments={"needle": "metadata"})],
            )
        )
    append_tool_fixture(
        session, ChatMessage.tool(tool_call_id="call", name="bash", content="needle result")
    )
    visible = ChatMessage.user("needle actual conversation")
    session.append(visible)
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    data = success(await search(tmp_path, {"query": "needle"}, backend))
    assert [hit["message_id"] for hit in data["items"]] == [visible.id]


async def test_substring_matches_are_not_hidden_by_whole_word_hits(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="words")
    messages = [ChatMessage.user("Auto"), ChatMessage.user("Autobahn")]
    session.append_many(messages)
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    data = success(await search(tmp_path, {"query": "Auto"}, backend))
    assert {hit["message_id"] for hit in data["items"]} == {message.id for message in messages}


async def test_visibility_filters_still_apply_to_search_and_context(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    for name, kinds in [
        ("user", [RunKind.USER]),
        ("calendar", [RunKind.CALENDAR]),
        ("sub", [RunKind.SUBAGENT]),
        ("reflection", [RunKind.USER, RunKind.SKILL_REFLECTION]),
        ("system", [RunKind.SYSTEM]),
    ]:
        session = sessions.create("coder", session_id=name)
        session.append_many(
            [
                ChatMessage.user("needle question"),
                ChatMessage.assistant(model="test", content="answer"),
            ]
        )
        for kind in kinds:
            await admit_run(sessions, session.address, kind)
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    normal = success(await search(tmp_path, {"query": "needle"}, backend))
    delegated = success(
        await search(tmp_path, {"query": "needle", "include_subagents": True}, backend)
    )
    assert {hit["session_id"] for hit in normal["items"]} == {"user", "calendar"}
    assert {hit["session_id"] for hit in delegated["items"]} == {"user", "calendar", "sub"}
    assert (
        next(hit for hit in delegated["items"] if hit["session_id"] == "sub")["include_subagents"]
        is True
    )


async def test_summary_hits_are_labeled_and_carry_no_context(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="summary")
    question = ChatMessage.user("Original question")
    summary = ChatMessage.compaction_checkpoint(
        summary="needle summary", projection=[], compacted_token_count=10
    )
    answer = ChatMessage.assistant(model="test", content="Final answer")
    session.append_many([question, summary, answer])
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    data = success(await search(tmp_path, {"query": "needle"}, backend))
    assert data["items"][0]["content_kind"] == "compaction_summary"
    assert data["items"][0]["context"] == []


async def test_passage_keeps_its_speaker_and_adds_other_turns_as_context(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="conversation")
    question = ChatMessage.user("Aurora Aufbewahrung?")
    answer = ChatMessage.assistant(model="fixture", content="30 Tage.")
    session.append_many([question, answer])
    embeddings = FixtureEmbeddings()
    backend = VectorRecallBackend(RecallBackendContext(tmp_path, sessions, embeddings=embeddings))
    try:
        await embed_documents(backend.index, sessions, embeddings)
        data = success(await search(tmp_path, {"query": "Aurora Aufbewahrung"}, backend))
    finally:
        backend.close()
    hit = next(item for item in data["items"] if item["message_id"] == question.id)
    assert hit["role"] == "user"
    assert hit["end_message_id"] == question.id
    assert hit["excerpt"]["text"] == question.content
    assert any(
        item["message_id"] == answer.id and item["text"] == answer.content
        for item in hit["context"]
    )
    assert not {"passage_id", "sources"} & hit.keys()


async def test_empty_filtered_page_keeps_more_matches_signal(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)

    class EmptyBackend(SqliteFtsRecallBackend):
        @override
        async def search_page(self, request):
            return RecallSearchPage((), "message", "relevance", "snapshot", True, 12)

    data = success(
        await search(
            tmp_path, {"query": "Aurora"}, EmptyBackend(RecallBackendContext(tmp_path, sessions))
        )
    )
    assert data["items"] == []
    assert data["has_more"] is True


@pytest.mark.parametrize("backend_type", [HybridRecallBackend, VectorRecallBackend])
@pytest.mark.parametrize("semantic_available", [True, False])
async def test_passage_excerpts_keep_source_positions_and_truncation(
    tmp_path: Path,
    backend_type: type[HybridRecallBackend | VectorRecallBackend],
    semantic_available: bool,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="long-message")
    source = "  needle ä😀\n" * 340 + "last words"
    message = ChatMessage.user(source)
    session.append(message)
    embeddings = StubEmbeddings()
    backend = backend_type(
        RecallBackendContext(
            tmp_path, sessions, embeddings=embeddings if semantic_available else None
        )
    )
    try:
        if semantic_available:
            await embed_documents(backend.index, sessions, embeddings)
        data = success(await search(tmp_path, {"query": "needle"}, backend))
    finally:
        backend.close()
    excerpts = [item["excerpt"] for item in data["items"]]
    assert len(excerpts) > 2
    assert len({item["start_char"] for item in excerpts}) == len(excerpts)
    assert all(item["message_id"] == message.id for item in data["items"])
    assert all(item["message_index"] == 0 for item in data["items"])
    for excerpt in excerpts:
        start, end = excerpt["start_char"], excerpt["end_char"]
        assert excerpt["text"] == source[start:end]
        assert excerpt["leading_truncated"] is (start > 0)
        assert excerpt["trailing_truncated"] is (end < len(source))
    assert any(not item["leading_truncated"] for item in excerpts)
    assert any(not item["trailing_truncated"] for item in excerpts)
    assert any(item["leading_truncated"] and item["trailing_truncated"] for item in excerpts)


async def test_hybrid_query_keeps_partial_keyword_evidence_ahead_of_semantic_only_hits(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="evidence")
    keyword = ChatMessage.user("The vehicle maintenance plan covers inspections.")
    semantic = ChatMessage.user("Driving on the motorway.")
    session.append_many([keyword, semantic])
    embeddings = StubEmbeddings()
    backend = HybridRecallBackend(RecallBackendContext(tmp_path, sessions, embeddings=embeddings))
    try:
        await embed_documents(backend.index, sessions, embeddings)
        data = success(await search(tmp_path, {"query": "vehicle maintenance budget"}, backend))
        exact = success(await search(tmp_path, {"query": "vehicle"}, backend))
    finally:
        backend.close()
    assert data["items"][0]["message_id"] == keyword.id
    assert data["items"][0]["match_kind"] == "keyword_match"
    assert (
        next(item for item in data["items"] if item["message_id"] == semantic.id)["match_kind"]
        == "semantic_candidate"
    )
    assert exact["items"][0]["message_id"] == keyword.id
    literal = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    assert (
        success(await search(tmp_path, {"query": "vehicle maintenance budget"}, literal))["items"]
        == []
    )


@pytest.mark.parametrize(
    "code", ["semantic_unavailable", "hybrid_unavailable", "stale_cursor", "extension_failed"]
)
async def test_backend_failures_keep_code_without_leaking_internal_diagnostics(
    tmp_path: Path, code: str
) -> None:
    sessions = ChatSessionManager(tmp_path)

    class BrokenBackend(SqliteFtsRecallBackend):
        @override
        async def search_page(self, request):
            raise RecallSearchError(code, "private-database-path-and-provider-detail")

    result = await search(
        tmp_path, {"query": "Aurora"}, BrokenBackend(RecallBackendContext(tmp_path, sessions))
    )
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert "private-database-path-and-provider-detail" not in str(result)


@pytest.mark.parametrize(
    "backend_type", [SqliteFtsRecallBackend, HybridRecallBackend, VectorRecallBackend]
)
@pytest.mark.parametrize("lineage", ["own", "inherited", "materialized"])
async def test_repeated_ids_keep_each_hit_and_its_own_context(
    tmp_path: Path, backend_type, lineage: str
) -> None:
    sessions = ChatSessionManager(tmp_path)
    source = sessions.create("coder", session_id="repeated")
    messages = []
    for topic in ("apples", "banana"):
        messages.append(ChatMessage.user(f"Question about {topic}"))
        answer = ChatMessage.assistant(model="fixture", content=f"anchor {topic} 123")
        answer = replace(answer, id="repeated", timestamp="2026-11-01T12:00:00.000000Z")
        messages.append(answer)
    source.append_many(messages)
    session = source
    if lineage != "own":
        session = await sessions.fork(source.address)
    if lineage == "materialized":
        sessions.delete(source.address)
    embeddings = StubEmbeddings()
    backend = backend_type(RecallBackendContext(tmp_path, sessions, embeddings=embeddings))
    try:
        if isinstance(backend, (HybridRecallBackend, VectorRecallBackend)):
            await embed_documents(backend.index, sessions, embeddings)
        data = success(
            await search(tmp_path, {"query": "anchor", "session_id": session.id}, backend)
        )
    finally:
        if isinstance(backend, (HybridRecallBackend, VectorRecallBackend)):
            backend.close()
    hits = sorted(
        (hit for hit in data["items"] if hit["message_id"] == "repeated"),
        key=lambda hit: hit["message_index"],
    )
    assert len(hits) == 2
    assert [hit["message_index"] for hit in hits] == [1, 3]
    for hit, topic in zip(hits, ("apples", "banana"), strict=True):
        assert hit["excerpt"]["text"] == f"anchor {topic} 123"
        assert [item["text"] for item in hit["context"]] == [f"Question about {topic}"]
    assert sessions.recall_context(session.address, "repeated") == []


async def test_shared_passage_keeps_each_sources_truncation(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    prefix = "needle " + "x" * 1493
    for name, text in (("short", prefix), ("long", prefix + "y" * 500)):
        session = sessions.create("coder", session_id=name)
        message = ChatMessage.user(text)
        message = replace(message, id="shared", timestamp="2026-11-01T12:00:00.000000Z")
        session.append(message)
    backend = HybridRecallBackend(RecallBackendContext(tmp_path, sessions))
    try:
        for name in ("short", "long", "short"):
            data = success(await search(tmp_path, {"query": "needle", "session_id": name}, backend))
            excerpt = data["items"][0]["excerpt"]
            assert excerpt["text"] == prefix
            assert excerpt["trailing_truncated"] is (name == "long")
    finally:
        backend.close()


@pytest.mark.parametrize("multi_message", [False, True])
async def test_extension_passage_without_source_bounds_does_not_claim_completeness(
    tmp_path: Path, multi_message: bool
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="extension")
    message = ChatMessage.user("before needle after")
    session.append(message)

    class Backend(SqliteFtsRecallBackend):
        @override
        async def search_page(self, request):
            hit = RecallSearchHit(
                "passage",
                session.id,
                message.id,
                "user",
                message.timestamp,
                "needle",
                1.0,
                end_message_id="another-message" if multi_message else message.id,
                source_length=50 if multi_message else None,
                sources=("extension",),
            )
            return RecallSearchPage((hit,), "passage", "relevance", "snapshot", False, 1)

    data = success(
        await search(
            tmp_path, {"query": "needle"}, Backend(RecallBackendContext(tmp_path, sessions))
        )
    )
    hit = data["items"][0]
    assert hit["excerpt"] == {"text": "needle", "source_boundaries_unknown": True}
    assert "match_kind" not in hit
