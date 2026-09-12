"""Session search: reading behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import (
    CanonicalSessionRecallBackend,
    RecallSearchCapabilities,
    RecallSearchHit,
    RecallSearchPage,
)
from core.sessions import ChatSession, ChatSessionManager
from core.tools.session_search import (
    SESSION_READ_TOOL_NAME,
    SESSION_SEARCH_RESULT_MAX_BYTES,
    session_read_handler,
    session_search_handler,
)
from tests.core.tools.session_search_helpers import (
    JsonObject,
    failure,
    make_context,
    success,
    timestamp,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


async def test_search_read_ref_covers_complete_conversation_block(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="multi-message-answer")
    question = ChatMessage.user("What was the result?", timestamp=timestamp(1))
    first = ChatMessage.assistant(
        model="test",
        content="The sapphire answer starts here.",
        timestamp=timestamp(2),
    )
    second = ChatMessage.assistant(
        model="test",
        content="The answer finishes in this separate Message.",
        timestamp=timestamp(3),
    )
    next_question = ChatMessage.user("Next topic", timestamp=timestamp(4))
    for message in (question, first, second, next_question):
        session.append(message)
    backend = CanonicalSessionRecallBackend(sessions)

    search = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "sapphire"},
            backend,
        )
    )
    read_ref = search["items"][0]["read_ref"]
    read = success(
        await session_read_handler(
            make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME),
            read_ref,
            sessions,
        )
    )

    assert read_ref == {
        "agent_id": "coder",
        "session_id": "multi-message-answer",
        "message_id": first.id,
    }
    assert [item["message"] for item in read["items"]] == [
        question.to_dict(),
        first.to_dict(),
        second.to_dict(),
    ]


async def test_passage_read_ref_expands_to_complete_conversation_block(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="passage-answer")
    question = ChatMessage.user("Question", timestamp=timestamp(1))
    first = ChatMessage.assistant(model="test", content="needle first", timestamp=timestamp(2))
    second = ChatMessage.assistant(model="test", content="answer continued", timestamp=timestamp(3))
    next_question = ChatMessage.user("Next topic", timestamp=timestamp(4))
    for message in (question, first, second, next_question):
        session.append(message)

    class _PassageBackend:
        def search_capabilities(self) -> RecallSearchCapabilities:
            return RecallSearchCapabilities(
                result_type="passage",
                guidance="Test passage search.",
            )

        async def search_page(self, _request: Any) -> RecallSearchPage:
            return RecallSearchPage(
                hits=(
                    RecallSearchHit(
                        result_type="passage",
                        session_id="passage-answer",
                        message_id=first.id,
                        role="assistant",
                        timestamp=str(first.timestamp),
                        text="needle first",
                        score=1.0,
                        passage_id="passage-1",
                        start_message_id=first.id,
                        end_message_id=first.id,
                    ),
                ),
                result_type="passage",
                ranking="test",
                snapshot_id="snapshot",
                has_more=False,
                total_candidate_sessions=1,
            )

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle"},
            _PassageBackend(),
            sessions=sessions,
        )
    )

    assert data["items"][0]["read_ref"] == {
        "agent_id": "coder",
        "session_id": "passage-answer",
        "message_id": first.id,
    }


async def test_read_selects_latest_or_anchored_conversation_block_and_exact_tool_result(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="blocks")
    first_user = ChatMessage.user("first question", timestamp=timestamp(1))
    first_answer = ChatMessage.assistant(
        model="test", content="first answer", timestamp=timestamp(2)
    )
    second_user = ChatMessage.user("second question", timestamp=timestamp(3))
    tool_result = ChatMessage.tool(
        tool_call_id="call-1", name="read", content="small result", timestamp=timestamp(4)
    )
    second_answer = ChatMessage.assistant(
        model="test", content="second answer", timestamp=timestamp(5)
    )
    messages = [first_user, first_answer, second_user, tool_result, second_answer]
    for message in messages:
        session.append(message)
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    latest = success(await session_read_handler(context, {"session_id": "blocks"}, sessions))
    anchored = success(
        await session_read_handler(
            context,
            {"session_id": "blocks", "message_id": first_answer.id},
            sessions,
        )
    )
    exact_tool_result = success(
        await session_read_handler(
            context,
            {"session_id": "blocks", "message_id": tool_result.id},
            sessions,
        )
    )
    assert [item["message"] for item in latest["items"]] == [
        message.to_dict() for message in messages[2:]
    ]
    assert [item["message"] for item in anchored["items"]] == [
        first_user.to_dict(),
        first_answer.to_dict(),
    ]
    assert exact_tool_result["items"] == [{"message_index": 3, "message": tool_result.to_dict()}]
    assert latest["selection"] == {
        "kind": "latest_block",
        "first_message_index": 2,
        "last_message_index": 4,
        "message_count": 3,
    }
    assert anchored["selection"]["kind"] == "conversation_block"
    assert exact_tool_result["selection"] == {
        "kind": "tool_result",
        "first_message_index": 3,
        "last_message_index": 3,
        "message_count": 1,
    }
    assert latest["user_anchors"] == [
        {
            "message_index": 0,
            "message_id": first_user.id,
            "timestamp": str(first_user.timestamp),
            "excerpt": {"text": "first question", "trailing_truncated": False},
        },
        {
            "message_index": 2,
            "message_id": second_user.id,
            "timestamp": str(second_user.timestamp),
            "excerpt": {"text": "second question", "trailing_truncated": False},
        },
    ]
    assert "user_anchors" not in anchored
    assert "user_anchors" not in exact_tool_result
    assert latest["session"]["message_count"] == 5
    assert latest["session"]["first_message"]["message_id"] == first_user.id
    assert latest["session"]["last_message"]["message_id"] == second_answer.id


async def test_read_projects_large_internal_metadata_to_a_bounded_descriptor(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="large-internal-metadata")
    message = ChatMessage.user("small history", timestamp=timestamp(1))
    session.append(message)
    sessions.set_metadata(
        session.address,
        {
            "auto_title": "Useful title",
            "run_kinds": ["chat"],
            "pinned_working_project_context": {"content": "x" * 70_000},
            "pinned_skill_catalog": {"content": "y" * 20_000},
        },
    )

    result = await session_read_handler(
        make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME),
        {"session_id": "large-internal-metadata"},
        sessions,
    )
    data = success(result)

    assert data["items"] == [{"message_index": 0, "message": message.to_dict()}]
    assert data["session"]["title"] == "Useful title"
    assert data["session"]["message_count"] == 1
    assert "metadata" not in data["session"]
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= (
        SESSION_SEARCH_RESULT_MAX_BYTES
    )


async def test_read_all_messages_returns_every_block_without_anchor_index(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="all-blocks")
    messages = [
        ChatMessage.user("first", timestamp=timestamp(1)),
        ChatMessage.assistant(model="test", content="one", timestamp=timestamp(2)),
        ChatMessage.user("second", timestamp=timestamp(3)),
        ChatMessage.assistant(model="test", content="two", timestamp=timestamp(4)),
    ]
    for message in messages:
        session.append(message)

    data = success(
        await session_read_handler(
            make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME),
            {"session_id": "all-blocks", "all_messages": True},
            sessions,
        )
    )

    assert [item["message"] for item in data["items"]] == [
        message.to_dict() for message in messages
    ]
    assert data["selection"] == {
        "kind": "all_messages",
        "first_message_index": 0,
        "last_message_index": 3,
        "message_count": 4,
    }
    assert "user_anchors" not in data


async def test_read_rejects_missing_message_and_invalid_continuation(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="read-errors")
    message = ChatMessage.user("only Message", timestamp=timestamp(1))
    session.append(message)
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    missing = await session_read_handler(
        context,
        {"session_id": "read-errors", "message_id": "missing"},
        sessions,
    )
    invalid = await session_read_handler(
        context,
        {
            "session_id": "read-errors",
            "message_id": message.id,
            "continuation": "r1:999999:not-the-selection",
        },
        sessions,
    )

    failure(missing, "message_not_found")
    failure(invalid, "invalid_continuation")


async def test_read_reports_only_a_missing_session_as_not_found(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    result = await session_read_handler(context, {"session_id": "missing"}, sessions)

    failure(result, "session_not_found")


async def test_read_does_not_report_session_read_failure_as_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="corrupt")
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    def fail_load(self: ChatSession) -> list[ChatMessage]:
        raise OSError("database read failed")

    monkeypatch.setattr(ChatSession, "load", fail_load)

    result = await session_read_handler(context, {"session_id": "corrupt"}, sessions)

    failure(result, "session_read_error")


async def test_read_does_not_report_permission_error_as_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="denied")
    session.append(ChatMessage.user("private", timestamp=timestamp(1)))
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    def deny_load(self: ChatSession) -> list[ChatMessage]:
        raise PermissionError("access denied")

    monkeypatch.setattr(ChatSession, "load", deny_load)
    result = await session_read_handler(context, {"session_id": "denied"}, sessions)

    failure(result, "session_read_error")


async def test_oversized_read_record_is_losslessly_segmented(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="segmented-read")
    message = ChatMessage.user("Ü\n" * 70_000, timestamp=timestamp(1))
    session.append(message)
    arguments: JsonObject = {
        "session_id": "segmented-read",
        "message_id": message.id,
    }
    segments: list[str] = []

    while True:
        result = await session_read_handler(
            make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME),
            arguments,
            sessions,
        )
        data = success(result)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES
        segments.append(data["items"][0]["segment"]["selection_json"])
        if not data["has_more"]:
            break
        arguments["continuation"] = data["next_continuation"]

    assert json.loads("".join(segments)) == [{"message_index": 0, "message": message.to_dict()}]


async def test_oversized_unanchored_read_segments_user_anchor_index_with_selection(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="segmented-index")
    message = ChatMessage.user("Ü\n" * 70_000, timestamp=timestamp(1))
    session.append(message)
    arguments: JsonObject = {"session_id": "segmented-index"}
    segments: list[str] = []

    while True:
        result = await session_read_handler(
            make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME),
            arguments,
            sessions,
        )
        data = success(result)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES
        segments.append(data["items"][0]["segment"]["selection_json"])
        if not data["has_more"]:
            break
        arguments["continuation"] = data["next_continuation"]

    selection = json.loads("".join(segments))
    assert selection["items"] == [{"message_index": 0, "message": message.to_dict()}]
    assert selection["user_anchors"] == [
        {
            "message_index": 0,
            "message_id": message.id,
            "timestamp": str(message.timestamp),
            "excerpt": {
                "text": "Ü " * 80,
                "trailing_truncated": True,
            },
        }
    ]


async def test_read_continuation_is_bound_to_the_original_selection(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="bound-continuation")
    message = ChatMessage.user("large\n" * 20_000, timestamp=timestamp(1))
    session.append(message)
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    first = success(
        await session_read_handler(
            context,
            {"session_id": "bound-continuation", "message_id": message.id},
            sessions,
        )
    )
    changed_selection = await session_read_handler(
        context,
        {
            "session_id": "bound-continuation",
            "all_messages": True,
            "continuation": first["next_continuation"],
        },
        sessions,
    )

    failure(changed_selection, "invalid_continuation")


async def test_large_tool_result_is_referenced_in_block_and_exactly_dereferenced(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="tool-result-ref")
    user = ChatMessage.user("inspect data", timestamp=timestamp(1))
    content = json.dumps({"rows": ["secret-value", "Ü" * 8_000]}, ensure_ascii=False)
    tool_result = ChatMessage.tool(
        tool_call_id="call-large",
        name="database",
        content=content,
        timestamp=timestamp(2),
    )
    answer = ChatMessage.assistant(model="test", content="done", timestamp=timestamp(3))
    for message in (user, tool_result, answer):
        session.append(message)
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    block = success(
        await session_read_handler(
            context,
            {"session_id": "tool-result-ref", "message_id": answer.id},
            sessions,
        )
    )
    result_item = block["items"][1]
    projected_content = json.loads(result_item["message"]["content"])

    assert result_item["message_index"] == 1
    assert result_item["message"]["tool_call_id"] == "call-large"
    assert result_item["read_ref"] == {
        "session_id": "tool-result-ref",
        "message_id": tool_result.id,
    }
    assert projected_content["_vbot_referenced_tool_result"] is True
    assert projected_content["original_bytes"] == len(content.encode("utf-8"))
    assert len(projected_content["preview"]) <= 800

    exact = success(await session_read_handler(context, result_item["read_ref"], sessions))
    assert exact["items"] == [{"message_index": 1, "message": tool_result.to_dict()}]
