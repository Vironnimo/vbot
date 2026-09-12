"""Conversation-only search, bounded context and visibility regressions."""

from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.recall import RecallBackendContext, SqliteFtsRecallBackend
from core.recall.canonical import SESSION_RECALL_DEFAULT_ROLES
from core.recall.passages import build_session_passages
from core.sessions import ChatSession, ChatSessionManager
from core.tools.session_search import session_search_handler
from tests.core.tools.session_search_helpers import make_context, success

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


async def test_hit_includes_question_and_final_answer_without_tool_payload_or_full_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="target", project_id="p1")
    question = ChatMessage.user("Which backup policy did we choose?")
    interim = ChatMessage.assistant(model="test", content="Retention initially: seven days.")
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
    data = success(
        await session_search_handler(
            make_context(tmp_path, project_id="p1"), {"query": "Retention"}, backend
        )
    )
    hit = data["items"][0]
    assert hit["message_id"] == interim.id
    assert [item["message_id"] for item in hit["context"]] == [question.id, answer.id]
    assert [item["text"] for item in hit["context"]] == [question.content, answer.content]
    assert "read_ref" not in hit
    assert hit["context_is_partial"] is True


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
    session.append(ChatMessage.tool(tool_call_id="call", name="bash", content="needle result"))
    visible = ChatMessage.user("needle actual conversation")
    session.append(visible)
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    data = success(
        await session_search_handler(make_context(tmp_path), {"query": "needle"}, backend)
    )
    assert [hit["message_id"] for hit in data["items"]] == [visible.id]
    passages = build_session_passages(session.load_active())
    assert all(
        "metadata" not in passage.text and "needle result" not in passage.text
        for passage in passages
    )
    assert "error" not in SESSION_RECALL_DEFAULT_ROLES


async def test_substring_matches_are_not_hidden_by_whole_word_hits(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="words")
    messages = [ChatMessage.user("Auto"), ChatMessage.user("Autobahn")]
    session.append_many(messages)
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    data = success(await session_search_handler(make_context(tmp_path), {"query": "Auto"}, backend))
    assert {hit["message_id"] for hit in data["items"]} == {message.id for message in messages}


async def test_visibility_filters_still_apply_to_search_and_context(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    for name, kinds in [
        ("user", ["user"]),
        ("sub", ["subagent"]),
        ("reflection", ["user", "skill_reflection"]),
        ("system", ["system"]),
    ]:
        session = sessions.create("coder", session_id=name)
        session.append_many(
            [
                ChatMessage.user("needle question"),
                ChatMessage.assistant(model="test", content="answer"),
            ]
        )
        sessions.set_metadata(session.address, {"run_kinds": kinds})
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    normal = success(
        await session_search_handler(make_context(tmp_path), {"query": "needle"}, backend)
    )
    delegated = success(
        await session_search_handler(
            make_context(tmp_path), {"query": "needle", "include_subagents": True}, backend
        )
    )
    assert {hit["session_id"] for hit in normal["items"]} == {"user"}
    assert {hit["session_id"] for hit in delegated["items"]} == {"user", "sub"}
    assert (
        next(hit for hit in delegated["items"] if hit["session_id"] == "sub")["include_subagents"]
        is True
    )


async def test_context_is_bounded_and_missing_anchor_does_not_borrow_a_block(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="long")
    question = ChatMessage.user("Q" * 10000)
    answer = ChatMessage.assistant(model="test", content="answer")
    session.append_many([question, answer])
    context = sessions.recall_context(session.address, answer.id)
    assert len(context[0]["text"]) == 800
    assert context[0]["truncated"] is True
    assert sessions.recall_context(session.address, "missing") == []


async def test_summaries_are_searchable_and_separate_from_verbatim_passages(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="summary")
    question = ChatMessage.user("Original question")
    summary = ChatMessage.compaction_checkpoint(
        summary="needle summary", projection=[], compacted_token_count=10
    )
    answer = ChatMessage.assistant(model="test", content="Final answer")
    session.append_many([question, summary, answer])
    passages = build_session_passages(session.load_active())
    summary_passages = [passage for passage in passages if "needle" in passage.text]
    assert summary_passages
    assert all(
        passage.start_role == passage.end_role == "compaction_checkpoint"
        and passage.start_message_id == passage.end_message_id == summary.id
        for passage in summary_passages
    )
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    data = success(
        await session_search_handler(make_context(tmp_path), {"query": "needle"}, backend)
    )
    assert data["items"][0]["content_kind"] == "compaction_summary"
    assert data["items"][0]["context"] == []


async def test_context_excludes_superseded_history(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="edited")
    old_question = ChatMessage.user("Old question")
    obsolete = ChatMessage.assistant(model="test", content="Obsolete answer")
    question = ChatMessage.user("Question")
    replacement = ChatMessage.assistant(model="test", content="New answer")
    session.append_many(
        [old_question, obsolete, ChatMessage.history_edit(old_question.id), question, replacement]
    )
    assert sessions.recall_context(session.address, obsolete.id) == []
    assert [
        item["message_id"] for item in sessions.recall_context(session.address, question.id)
    ] == [replacement.id]
