"""Tests for the Session-scoped History tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatMessage, ToolCall
from core.sessions import ChatSession, ChatSessionManager
from core.tools import (
    HISTORY_RESULT_MAX_BYTES,
    HISTORY_TOOL_NAME,
    ToolContext,
    ToolRegistry,
    make_history_handler,
    register_history_tool,
)


def _context(session_id: str, *, agent_id: str = "agent") -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id=session_id,
        run_id="run",
        tool_call_id="call",
        tool_name=HISTORY_TOOL_NAME,
        tool_call_index=0,
        workspace=Path("workspace"),
        app_root=Path("app"),
        data_root=Path("data"),
        session_tool_grants=(HISTORY_TOOL_NAME,),
    )


def _checkpoint(summary: str = "Earlier context") -> ChatMessage:
    return ChatMessage.compaction_checkpoint(
        summary=summary,
        projection=[],
        compacted_token_count=10,
    )


def _call(
    manager: ChatSessionManager,
    session: ChatSession,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return cast(dict[str, Any], make_history_handler(manager)(_context(session.id), arguments))


def _data(result: dict[str, Any]) -> dict[str, Any]:
    assert result["ok"] is True, result
    return cast(dict[str, Any], result["data"])


def _messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [item["message"] for item in data["items"]]


def _serialized_size(result: dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_registration_is_session_scoped_and_schema_is_strict(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    registry = ToolRegistry()

    register_history_tool(registry, manager)

    tool = registry.get(HISTORY_TOOL_NAME)
    assert tool.session_scoped is True
    assert tool.parameters["required"] == ["action"]
    assert tool.parameters["additionalProperties"] is False
    assert set(tool.parameters["properties"]["action"]["enum"]) == {
        "overview",
        "search",
        "read",
        "around",
    }
    display = registry.display_for_call(
        HISTORY_TOOL_NAME,
        {"action": "search", "query": "secret", "cursor": "opaque"},
    )
    assert display["summary"] == "search · all earlier history"
    assert display["hidden_argument_keys"] == ["cursor", "message_id", "query"]


def test_checkpoint_grant_and_cursor_lifecycle_across_restart_move_takeover_and_fork(
    tmp_path: Path,
) -> None:
    manager = ChatSessionManager(tmp_path)
    source = manager.create("alpha", session_id="source")
    source.append(ChatMessage.user("first"))
    source.append(ChatMessage.assistant(model="openai/gpt-5.2", content="second"))
    source.append(_checkpoint())
    first = _data(
        make_history_handler(manager)(
            _context(source.id, agent_id="alpha"),
            {"action": "read", "limit": 1},
        )
    )
    cursor = first["next_cursor"]

    restarted = ChatSessionManager(tmp_path)
    restarted_source = restarted.get("alpha", source.id)
    restarted_page = make_history_handler(restarted)(
        _context(source.id, agent_id="alpha"),
        {"action": "read", "cursor": cursor},
    )
    assert restarted_page["ok"] is True

    moved = asyncio.run(restarted.move("alpha", restarted_source.id, "beta"))
    moved_page = make_history_handler(restarted)(
        _context(moved.id, agent_id="beta"),
        {"action": "read", "cursor": cursor},
    )
    assert moved_page["ok"] is True
    moved.append(ChatMessage.agent_takeover(from_address="alpha", to_address="beta"))
    assert (
        make_history_handler(restarted)(
            _context(moved.id, agent_id="beta"),
            {"action": "overview"},
        )["ok"]
        is True
    )

    fork = asyncio.run(restarted.fork("beta", moved.id, target_agent_id="gamma"))
    assert (
        make_history_handler(restarted)(
            _context(fork.id, agent_id="gamma"),
            {"action": "overview"},
        )["ok"]
        is True
    )
    fork_cursor_result = make_history_handler(restarted)(
        _context(fork.id, agent_id="gamma"),
        {"action": "read", "cursor": cursor},
    )
    assert fork_cursor_result["error"]["code"] == "invalid_cursor"


def test_checkpoint_free_session_returns_history_unavailable(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    session.append(ChatMessage.user("Hello"))

    result = _call(manager, session, {"action": "read"})

    assert result["ok"] is False
    assert result["error"]["code"] == "history_unavailable"


def test_overview_reports_fixed_checkpoint_sections(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    first_user = ChatMessage.user("First question")
    first_assistant = ChatMessage.assistant(model="openai/gpt", content="First answer")
    first_checkpoint = _checkpoint("First summary")
    second_user = ChatMessage.user("Second question")
    second_checkpoint = _checkpoint("Second summary")
    for message in (
        first_user,
        first_assistant,
        first_checkpoint,
        second_user,
        second_checkpoint,
    ):
        session.append(message)

    data = _data(_call(manager, session, {"action": "overview"}))

    assert [item["checkpoint"] for item in data["items"]] == [1, 2]
    assert data["items"][0] == {
        "checkpoint": 1,
        "checkpoint_id": first_checkpoint.id,
        "timestamp": first_checkpoint.timestamp,
        "start_timestamp": first_user.timestamp,
        "end_timestamp": first_assistant.timestamp,
        "eligible_count": 2,
        "summary": "First summary",
    }
    assert data["items"][1]["eligible_count"] == 1
    assert data["snapshot"]["checkpoint_id"] == second_checkpoint.id
    assert data["scope"] == {"checkpoint": None, "checkpoint_id": None}
    assert data["has_more"] is False
    assert "next_cursor" not in data
    assert "content" not in data


def test_read_uses_all_history_or_one_non_overlapping_section(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    first_user = ChatMessage.user("First")
    first_assistant = ChatMessage.assistant(model="openai/gpt", content="Answer one")
    first_checkpoint = _checkpoint("First")
    second_user = ChatMessage.user("Second")
    error = ChatMessage.error("network_error", "Temporary failure")
    second_assistant = ChatMessage.assistant(model="openai/gpt", content="Answer two")
    second_checkpoint = _checkpoint("Second")
    after_latest = ChatMessage.user("Not yet hidden")
    for message in (
        first_user,
        first_assistant,
        first_checkpoint,
        second_user,
        error,
        second_assistant,
        second_checkpoint,
        after_latest,
    ):
        session.append(message)

    all_data = _data(_call(manager, session, {"action": "read"}))
    second_data = _data(_call(manager, session, {"action": "read", "checkpoint": 2}))
    end_data = _data(
        _call(
            manager,
            session,
            {"action": "read", "checkpoint": 2, "direction": "end", "limit": 2},
        )
    )

    assert [message["id"] for message in _messages(all_data)] == [
        first_user.id,
        first_assistant.id,
        second_user.id,
        error.id,
        second_assistant.id,
    ]
    assert [item["checkpoint"] for item in all_data["items"]] == [1, 1, 2, 2, 2]
    assert [message["id"] for message in _messages(second_data)] == [
        second_user.id,
        error.id,
        second_assistant.id,
    ]
    assert [message["id"] for message in _messages(end_data)] == [
        second_assistant.id,
        error.id,
    ]
    assert _messages(second_data)[0] == second_user.to_dict()


@pytest.mark.parametrize(
    ("match", "query", "expected"),
    [
        ("all_terms", "alpha gamma", ["alpha beta gamma", "gamma alpha"]),
        ("phrase", "alpha beta", ["alpha beta gamma"]),
        ("any_term", "beta delta", ["alpha beta gamma", "delta", "beta later"]),
    ],
)
def test_search_is_case_insensitive_chronological_and_deterministic(
    tmp_path: Path,
    match: str,
    query: str,
    expected: list[str],
) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    contents = ["Alpha beta GAMMA", "delta", "gamma alpha", "beta later"]
    for content in contents:
        session.append(ChatMessage.user(content))
    session.append(_checkpoint())

    data = _data(
        _call(
            manager,
            session,
            {"action": "search", "query": query, "match": match},
        )
    )

    assert [item["excerpt"].casefold() for item in data["items"]] == expected
    assert all(len(item["excerpt"]) <= 320 for item in data["items"])
    assert [item["checkpoint"] for item in data["items"]] == [1] * len(expected)


def test_search_excerpt_marks_omitted_edges_within_320_characters(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    message = ChatMessage.user(f"{'a' * 300} NEEDLE {'b' * 500}")
    session.append(message)
    session.append(_checkpoint())

    data = _data(_call(manager, session, {"action": "search", "query": "needle"}))
    excerpt = data["items"][0]["excerpt"]

    assert len(excerpt) <= 320
    assert excerpt.startswith("...")
    assert excerpt.endswith("...")
    assert "NEEDLE" in excerpt


def test_tool_and_note_roles_are_opt_in_and_history_artifacts_are_removed(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    mixed = ChatMessage.assistant(
        model="openai/gpt",
        content="Keep this unrelated answer.",
        reasoning="Keep this reasoning.",
        tool_calls=[
            ToolCall(id="history-call", name="history", arguments={"action": "read"}),
            ToolCall(id="read-call", name="read", arguments={"path": "notes.md"}),
        ],
    )
    history_result = ChatMessage.tool(
        tool_call_id="history-call",
        name="history",
        content='{"ok":true}',
    )
    read_result = ChatMessage.tool(
        tool_call_id="read-call",
        name="read",
        content='{"ok":true,"data":{"content":"evidence"}}',
    )
    pure_history = ChatMessage.assistant(
        model="openai/gpt",
        content=None,
        tool_calls=[ToolCall(id="history-only", name="history", arguments={"action": "read"})],
    )
    pure_history_result = ChatMessage.tool(
        tool_call_id="history-only",
        name="history",
        content='{"ok":true}',
    )
    note = ChatMessage.note("Internal evidence")
    for message in (
        mixed,
        history_result,
        read_result,
        pure_history,
        pure_history_result,
        note,
        _checkpoint(),
    ):
        session.append(message)

    default_data = _data(_call(manager, session, {"action": "read"}))
    expanded = _data(
        _call(
            manager,
            session,
            {"action": "read", "roles": ["assistant", "tool", "note"]},
        )
    )

    assert [message["id"] for message in _messages(default_data)] == [mixed.id]
    expanded_messages = _messages(expanded)
    assert [message["id"] for message in expanded_messages] == [mixed.id, read_result.id, note.id]
    sanitized_mixed = expanded_messages[0]
    assert sanitized_mixed["content"] == "Keep this unrelated answer."
    assert sanitized_mixed["reasoning"] == "Keep this reasoning."
    assert [call["name"] for call in sanitized_mixed["tool_calls"]] == ["read"]


def test_around_returns_complete_local_sequence_and_honors_section_boundary(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    first = ChatMessage.user("first")
    first_checkpoint = _checkpoint("first")
    second = ChatMessage.user("second")
    anchor = ChatMessage.assistant(model="openai/gpt", content="anchor")
    fourth = ChatMessage.error("network_error", "fourth")
    second_checkpoint = _checkpoint("second")
    for message in (first, first_checkpoint, second, anchor, fourth, second_checkpoint):
        session.append(message)

    data = _data(
        _call(
            manager,
            session,
            {
                "action": "around",
                "message_id": anchor.id,
                "checkpoint": 2,
                "before": 10,
                "after": 10,
            },
        )
    )

    assert [message["id"] for message in _messages(data)] == [second.id, anchor.id, fourth.id]
    assert first.id not in {message["id"] for message in _messages(data)}


def test_around_distinguishes_missing_and_out_of_scope_anchor(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    tool = ChatMessage.tool(tool_call_id="read", name="read", content="evidence")
    session.append(tool)
    session.append(_checkpoint())

    missing = _call(manager, session, {"action": "around", "message_id": "missing"})
    outside = _call(manager, session, {"action": "around", "message_id": tool.id})

    assert missing["error"]["code"] == "message_not_found"
    assert outside["error"]["code"] == "anchor_outside_scope"


def test_cursor_freezes_snapshot_across_appends_and_later_compaction(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    first = ChatMessage.user("first")
    second = ChatMessage.user("second")
    first_checkpoint = _checkpoint("first")
    for message in (first, second, first_checkpoint):
        session.append(message)

    first_page = _data(_call(manager, session, {"action": "read", "limit": 1}))
    cursor = first_page["next_cursor"]
    appended = ChatMessage.user("new section")
    second_checkpoint = _checkpoint("second")
    session.append(appended)
    session.append(second_checkpoint)

    second_page = _data(_call(manager, session, {"action": "read", "cursor": cursor}))

    assert [message["id"] for message in _messages(first_page)] == [first.id]
    assert [message["id"] for message in _messages(second_page)] == [second.id]
    assert second_page["snapshot"]["checkpoint_id"] == first_checkpoint.id
    assert appended.id not in {message["id"] for message in _messages(second_page)}


def test_cursor_survives_manager_restart_but_not_action_session_or_corruption(
    tmp_path: Path,
) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    session.append(ChatMessage.user("first"))
    session.append(ChatMessage.user("second"))
    session.append(_checkpoint())
    first_page = _data(_call(manager, session, {"action": "read", "limit": 1}))
    cursor = first_page["next_cursor"]

    restarted = ChatSessionManager(tmp_path)
    restarted_session = restarted.get("agent", "session-one")
    resumed = _call(restarted, restarted_session, {"action": "read", "cursor": cursor})
    wrong_action = _call(manager, session, {"action": "search", "cursor": cursor})
    replacement = "x" if cursor[0] != "x" else "y"
    corrupted = _call(
        manager,
        session,
        {"action": "read", "cursor": f"{replacement}{cursor[1:]}"},
    )

    other = manager.create("agent", session_id="session-two")
    for message in session.load():
        other.append(message)
    wrong_session = _call(manager, other, {"action": "read", "cursor": cursor})

    assert resumed["ok"] is True
    assert wrong_action["error"]["code"] == "invalid_cursor"
    assert corrupted["error"]["code"] == "invalid_cursor"
    assert wrong_session["error"]["code"] == "invalid_cursor"


def test_cursor_continuation_rejects_repeated_scope_arguments(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    session.append(ChatMessage.user("first"))
    session.append(ChatMessage.user("second"))
    session.append(_checkpoint())
    cursor = _data(_call(manager, session, {"action": "read", "limit": 1}))["next_cursor"]

    result = _call(
        manager,
        session,
        {"action": "read", "cursor": cursor, "checkpoint": 1},
    )

    assert result["error"]["code"] == "invalid_arguments"


def test_oversized_unicode_record_continues_losslessly_under_50_kib(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    original = ChatMessage.user("start-" + "🙂" * 40_000 + "-end")
    session.append(original)
    session.append(_checkpoint())

    result = _call(manager, session, {"action": "read", "limit": 1})
    chunks: list[str] = []
    while True:
        assert _serialized_size(result) <= HISTORY_RESULT_MAX_BYTES
        data = _data(result)
        assert len(data["items"]) == 1
        segment = data["items"][0]["segment"]
        chunks.append(segment["record_json"])
        if not data["has_more"]:
            assert segment["complete"] is True
            break
        result = _call(
            manager,
            session,
            {"action": "read", "cursor": data["next_cursor"]},
        )

    assert json.loads("".join(chunks)) == original.to_dict()


def test_explicit_limit_returns_only_requested_logical_amount(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    for index in range(5):
        session.append(ChatMessage.user(f"message {index}"))
    session.append(_checkpoint())

    data = _data(_call(manager, session, {"action": "read", "limit": 2}))

    assert len(data["items"]) == 2
    assert data["has_more"] is True


def test_empty_roles_and_no_search_matches_are_successes(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    session.append(ChatMessage.user("known text"))
    session.append(_checkpoint())

    empty_roles = _data(_call(manager, session, {"action": "read", "roles": []}))
    no_matches = _data(_call(manager, session, {"action": "search", "query": "absent term"}))

    assert empty_roles["items"] == []
    assert empty_roles["has_more"] is False
    assert no_matches["items"] == []
    assert no_matches["has_more"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "overview", "query": "x"},
        {"action": "search", "query": ""},
        {"action": "read", "limit": 0},
        {"action": "read", "roles": ["unknown"]},
        {"action": "around", "message_id": ""},
        {"action": "around", "message_id": "x", "before": 101},
    ],
)
def test_invalid_action_arguments_return_invalid_arguments(
    tmp_path: Path,
    arguments: dict[str, Any],
) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    session.append(_checkpoint())

    result = _call(manager, session, arguments)

    assert result["error"]["code"] == "invalid_arguments"


def test_missing_checkpoint_reports_only_available_range(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    session.append(ChatMessage.user("secret content"))
    session.append(_checkpoint())

    result = _call(manager, session, {"action": "read", "checkpoint": 7})

    assert result["error"]["code"] == "checkpoint_not_found"
    assert "1-1" in result["error"]["message"]
    assert "secret content" not in result["error"]["message"]


def test_corrupt_session_returns_history_session_error(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="session-one")
    session.append(_checkpoint())
    with session.path.open("a", encoding="utf-8") as handle:
        handle.write("{complete invalid json}\n")

    result = _call(manager, session, {"action": "read"})

    assert result["error"]["code"] == "history_session_error"
