"""Session search: conversation-only hits, bounded context and visibility."""

from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.recall import (
    RecallBackendContext,
    RecallSearchError,
    RecallSearchPage,
    SqliteFtsRecallBackend,
    VectorRecallBackend,
)
from core.runs import RunKind
from core.sessions import ChatSession, ChatSessionManager
from scripts.provider_probe.recall_cases import FixtureEmbeddings
from tests.core.recall.recall_test_support import embed_documents
from tests.core.sessions.history_fixtures import admit_run, append_tool_fixture
from tests.core.tools.session_search_test_support import search, success

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


async def test_fts_hit_includes_question_and_final_answer_without_loading_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    session.append_many([question, interim, tool, answer, ChatMessage.user("Unrelated question")])
    sessions.create("coder", session_id="target").append(ChatMessage.user("Retention wrong scope"))

    def reject_load(*_args: object) -> None:
        raise AssertionError("Search must not load an entire transcript")

    monkeypatch.setattr(ChatSession, "load", reject_load)
    monkeypatch.setattr(ChatSession, "load_active", reject_load)
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    data = success(await search(tmp_path, {"query": "Retention"}, backend, project_id="p1"))
    missing = success(await search(tmp_path, {"query": "absent"}, backend, project_id="p1"))

    hit = data["items"][0]
    assert hit["message_id"] == interim.id
    assert [item["message_id"] for item in hit["context"]] == [question.id, answer.id]
    assert [item["text"] for item in hit["context"]] == [question.content, answer.content]
    assert "read_ref" not in hit
    assert hit["context_is_partial"] is True
    assert data["sessions"] == [{"agent_id": "coder", "session_id": "target"}]
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


async def test_multi_message_passage_does_not_attribute_all_text_to_first_speaker(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="conversation")
    question = ChatMessage.user("Aurora Aufbewahrung?")
    answer = ChatMessage.assistant(model="fixture", content="30 Tage.")
    session.append_many([question, answer])
    embeddings = FixtureEmbeddings()
    backend = VectorRecallBackend(RecallBackendContext(tmp_path, sessions, embeddings=embeddings))
    await embed_documents(backend.index, sessions, embeddings)
    data = success(await search(tmp_path, {"query": "Aurora Aufbewahrung"}, backend))
    hit = data["items"][0]
    assert hit["content_kind"] == "conversation_excerpt"
    assert "role" not in hit
    assert hit["message_id"] == question.id
    assert hit["end_message_id"] == answer.id
    assert "30 Tage." in hit["excerpt"]["text"]
    assert not {"passage_id", "sources"} & hit.keys()


async def test_empty_filtered_page_keeps_more_matches_signal(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)

    class EmptyBackend(SqliteFtsRecallBackend):
        async def search_page(self, request):
            return RecallSearchPage((), "message", "relevance", "snapshot", True, 12)

    data = success(
        await search(
            tmp_path, {"query": "Aurora"}, EmptyBackend(RecallBackendContext(tmp_path, sessions))
        )
    )
    assert data["items"] == []
    assert data["has_more"] is True


@pytest.mark.parametrize(
    "code", ["semantic_unavailable", "hybrid_unavailable", "stale_cursor", "extension_failed"]
)
async def test_backend_failures_keep_code_without_leaking_internal_diagnostics(
    tmp_path: Path, code: str
) -> None:
    sessions = ChatSessionManager(tmp_path)

    class BrokenBackend(SqliteFtsRecallBackend):
        async def search_page(self, request):
            raise RecallSearchError(code, "private-database-path-and-provider-detail")

    result = await search(
        tmp_path, {"query": "Aurora"}, BrokenBackend(RecallBackendContext(tmp_path, sessions))
    )
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert "private-database-path-and-provider-detail" not in str(result)
