"""Contract tests for the built-in Session Recall tools."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import (
    JsonlSessionRecallBackend,
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchHit,
    RecallSearchPage,
    SqliteFtsRecallBackend,
)
from core.recall.hybrid import HybridRecallBackend
from core.recall.jsonl import RECALL_TOOL_RESULT_NAMES
from core.recall.vector import VectorRecallBackend
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.tools.session_search import (
    SESSION_DESCRIPTOR_EXCERPT_MAX_CHARS,
    SESSION_READ_TOOL_NAME,
    SESSION_READ_TOOL_PARAMETERS,
    SESSION_SEARCH_EXCERPT_MAX_CHARS,
    SESSION_SEARCH_RESULT_MAX_BYTES,
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
    build_session_search_description,
    build_session_search_parameters,
    register_session_search_tool,
    session_read_handler,
    session_search_handler,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope

pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]


def make_context(
    data_root: Path,
    *,
    agent_id: str = "coder",
    project_id: str | None = None,
    tool_name: str = SESSION_SEARCH_TOOL_NAME,
) -> ToolContext:
    workspace = data_root / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        agent_id=agent_id,
        session_id="current-session",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=data_root.parent,
        data_root=data_root,
        project_id=project_id,
    )


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def success(result: JsonObject) -> JsonObject:
    assert is_tool_result_envelope(result)
    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    return data


def failure(result: JsonObject, code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result)
    assert result["ok"] is False
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    return error  # type: ignore[return-value]


async def test_registration_exposes_two_small_stable_tools(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    registry = ToolRegistry()
    register_session_search_tool(registry, sessions)

    search = registry.get(SESSION_SEARCH_TOOL_NAME)
    read = registry.get(SESSION_READ_TOOL_NAME)

    assert set(search.parameters["properties"]) == {
        "query",
        "period",
        "agent_id",
        "session_id",
    }
    assert set(read.parameters["properties"]) == {
        "session_id",
        "message_id",
        "agent_id",
        "continuation",
        "all_messages",
    }
    assert search.parameters == SESSION_SEARCH_TOOL_PARAMETERS
    assert read.parameters == SESSION_READ_TOOL_PARAMETERS
    assert "oneOf" not in search.parameters
    assert "additionalProperties" not in search.parameters
    assert search.parameters["required"] == []
    assert "oneOf" not in read.parameters
    assert "additionalProperties" not in read.parameters
    assert read.parameters["required"] == ["session_id"]
    assert read.open_input_schema is True
    assert search.open_input_schema is True
    assert search.description.startswith(SESSION_SEARCH_TOOL_DESCRIPTION)
    assert {
        SESSION_SEARCH_TOOL_NAME,
        SESSION_READ_TOOL_NAME,
    } == RECALL_TOOL_RESULT_NAMES
    search_display = registry.display_for_call(
        SESSION_SEARCH_TOOL_NAME,
        {"query": "release"},
        result={
            "ok": True,
            "data": {"items": [{}, {}], "has_more": True},
            "error": None,
            "artifacts": [],
        },
    )
    read_display = registry.display_for_call(
        SESSION_READ_TOOL_NAME,
        {"session_id": "session-one"},
        result={
            "ok": True,
            "data": {"items": [{}], "has_more": False},
            "error": None,
            "artifacts": [],
        },
    )
    assert search_display["facts"] == [
        {"kind": "count", "value": 2, "unit": "results", "at_least": True}
    ]
    assert read_display["facts"] == [
        {"kind": "count", "value": 1, "unit": "results", "at_least": False}
    ]

    context = RecallBackendContext(data_dir=tmp_path, sessions=sessions)
    backend_definitions = {}
    for name, backend in (
        ("jsonl_scan", JsonlSessionRecallBackend(sessions)),
        ("sqlite_fts", SqliteFtsRecallBackend(context)),
        ("vector", VectorRecallBackend(context)),
        ("hybrid", HybridRecallBackend(context)),
    ):
        backend_registry = ToolRegistry()
        register_session_search_tool(backend_registry, backend, sessions)
        definition = backend_registry.get(SESSION_SEARCH_TOOL_NAME)
        backend_definitions[name] = definition
        assert set(definition.parameters["properties"]) == set(search.parameters["properties"])

    assert backend_definitions["jsonl_scan"].parameters == SESSION_SEARCH_TOOL_PARAMETERS
    assert len({definition.description for definition in backend_definitions.values()}) == 4
    assert (
        len(
            {
                definition.parameters["properties"]["query"]["description"]
                for definition in backend_definitions.values()
            }
        )
        == 4
    )
    for field in ("period", "agent_id", "session_id"):
        assert (
            len(
                {
                    definition.parameters["properties"][field]["description"]
                    for definition in backend_definitions.values()
                }
            )
            == 1
        )


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"session_id": "target", "unexpected": True},
        {"session_id": "target", "cursor": "opaque"},
        {"session_id": "target", "start_message_id": "message-id"},
        {"session_id": "target", "last_messages": 2},
        {"session_id": "target", "page_size": 20},
        {"session_id": "target", "offset": 1},
        {"session_id": "target", "continuation": ""},
        {"session_id": "target", "all_messages": "yes"},
        {"session_id": "target", "all_messages": True, "message_id": "message-id"},
    ),
)
async def test_session_read_handler_rejects_invalid_flat_combinations(
    tmp_path: Path, arguments: JsonObject
) -> None:
    sessions = ChatSessionManager(tmp_path)

    result = await session_read_handler(
        make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME),
        arguments,
        sessions,
    )

    failure(result, "invalid_arguments")


async def test_current_session_is_unavailable_but_same_id_for_another_agent_is_allowed(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    current = sessions.create("coder", session_id="current-session")
    current.append(ChatMessage.user("needle current", timestamp=timestamp(3)))
    past = sessions.create("coder", session_id="past-session")
    past_message = ChatMessage.user("needle past", timestamp=timestamp(2))
    past.append(past_message)
    other = sessions.create("reviewer", session_id="current-session")
    other_message = ChatMessage.user("needle other Agent", timestamp=timestamp(1))
    other.append(other_message)
    backend = JsonlSessionRecallBackend(sessions)
    search_context = make_context(tmp_path)
    read_context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    listed = success(await session_search_handler(search_context, {}, backend))
    searched = success(await session_search_handler(search_context, {"query": "needle"}, backend))
    current_read = await session_read_handler(
        read_context,
        {"session_id": "current-session"},
        sessions,
    )
    other_read = success(
        await session_read_handler(
            read_context,
            {"agent_id": "reviewer", "session_id": "current-session"},
            sessions,
        )
    )

    assert [item["session_id"] for item in listed["items"]] == ["past-session"]
    assert [item["session_id"] for item in searched["items"]] == ["past-session"]
    failure(current_read, "current_session_unavailable")
    assert other_read["items"] == [{"message_index": 0, "message": other_message.to_dict()}]


async def test_current_session_writes_never_enter_list_or_search_results(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    current = sessions.create("coder", session_id="current-session")
    current.append(ChatMessage.user("needle current", timestamp=timestamp(3)))
    for index in range(2):
        past = sessions.create("coder", session_id=f"past-{index}")
        past.append(ChatMessage.user(f"needle past {index}", timestamp=timestamp(index + 1)))
    backend = JsonlSessionRecallBackend(sessions)
    context = make_context(tmp_path)

    current.append(ChatMessage.user("needle appended", timestamp=timestamp(4)))
    listed = success(await session_search_handler(context, {}, backend))
    searched = success(await session_search_handler(context, {"query": "needle"}, backend))

    assert all(item["session_id"].startswith("past-") for item in listed["items"])
    assert all(item["session_id"].startswith("past-") for item in searched["items"])


async def test_search_can_restrict_query_to_one_past_session(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    first = sessions.create("coder", session_id="first")
    first_message = ChatMessage.user("shared needle first", timestamp=timestamp(1))
    first.append(first_message)
    second = sessions.create("coder", session_id="second")
    second.append(ChatMessage.user("shared needle second", timestamp=timestamp(2)))
    backend = JsonlSessionRecallBackend(sessions)
    context = make_context(tmp_path)

    scoped = success(
        await session_search_handler(
            context,
            {"query": "shared needle", "session_id": "first"},
            backend,
        )
    )
    current = await session_search_handler(
        context,
        {"query": "needle", "session_id": "current-session"},
        backend,
    )

    assert [item["session_id"] for item in scoped["items"]] == ["first"]
    assert scoped["items"][0]["message_id"] == first_message.id
    failure(current, "current_session_unavailable")


async def test_definition_explains_active_backend(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    context = RecallBackendContext(data_dir=tmp_path, sessions=sessions)

    expected = {
        "jsonl_scan": (
            "Find persisted Sessions and literal matches in past conversations. "
            "The current Session is excluded. Omit query to list recent Sessions. Returns at "
            "most 10 items with no paging; narrow with period or session_id. Search matches "
            "include session_read references.",
            "Literal terms to find. Every whitespace-separated term must occur as a "
            "case-insensitive substring; synonyms and paraphrases do not match. Omit to list "
            "recent Sessions. Matches are newest first.",
        ),
        "sqlite_fts": (
            "Find persisted Sessions and relevance-ranked literal matches in past "
            "conversations. The current Session is excluded. Omit query to list recent Sessions. "
            "Returns at most 10 items with no paging; narrow with period or session_id. Search "
            "matches include session_read references.",
            "Literal terms to find. Every whitespace-separated term must occur as a "
            "case-insensitive substring. Omit to list recent Sessions. Matches are ranked by "
            "text relevance.",
        ),
        "vector": (
            "Find persisted Sessions and semantically related passages from past "
            "conversations. The current Session is excluded. Omit query to list recent Sessions. "
            "Returns at most 10 items with no paging; narrow with period or session_id. Search "
            "matches include session_read references.",
            "Short topic description to find by meaning. Bare keywords anchor poorly and exact "
            "occurrences may be missed. Omit to list recent Sessions. Matches are ranked by "
            "semantic relevance.",
        ),
        "hybrid": (
            "Find persisted Sessions and relevant passages using literal and semantic search. "
            "The current Session is excluded. Omit query to list recent Sessions. Returns at "
            "most 10 items with no paging; narrow with period or session_id. Search matches "
            "include session_read references.",
            "Literal terms or a short topic description. Every whitespace-separated term is "
            "required by literal search; the same query is also searched by meaning. Omit to "
            "list recent Sessions. Matches combine both rankings by relevance.",
        ),
    }
    backends = {
        "jsonl_scan": JsonlSessionRecallBackend(sessions),
        "sqlite_fts": SqliteFtsRecallBackend(context),
        "vector": VectorRecallBackend(context),
        "hybrid": HybridRecallBackend(context),
    }

    for name, backend in backends.items():
        description, query_description = expected[name]
        assert build_session_search_description(backend) == description
        assert (
            build_session_search_parameters(backend)["properties"]["query"]["description"]
            == query_description
        )


@pytest.mark.parametrize(
    "arguments",
    (
        {"request": {"operation": "search", "query": "needle"}},
        {"action": "search", "query": "needle"},
        {"query": "needle", "roles": ["user"]},
        {"query": "needle", "match": "phrase"},
        {"query": "needle", "order": "oldest"},
        {"query": "needle", "since": "2026-05-01"},
        {"query": "needle", "limit": 1},
        {"cursor": "opaque"},
        {"session_id": "past"},
    ),
)
async def test_search_rejects_retired_and_advanced_fields(
    tmp_path: Path,
    arguments: JsonObject,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    result = await session_search_handler(
        make_context(tmp_path),
        arguments,
        JsonlSessionRecallBackend(sessions),
    )

    failure(result, "invalid_arguments")


async def test_list_supports_period_filter(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    weekend = sessions.create("coder", session_id="weekend")
    weekday = sessions.create("coder", session_id="weekday")
    weekend_question = ChatMessage.user("Saturday discussion", timestamp=timestamp(2))
    weekend_answer = ChatMessage.assistant(
        model="test",
        content="Important weekend answer",
        timestamp=timestamp(3),
    )
    weekend.append(weekend_question)
    weekend.append(weekend_answer)
    weekday.append(ChatMessage.user("Monday discussion", timestamp=timestamp(4)))
    backend = JsonlSessionRecallBackend(sessions)

    period = success(
        await session_search_handler(
            make_context(tmp_path),
            {"period": "2026-05-02/2026-05-03"},
            backend,
        )
    )
    assert [item["session_id"] for item in period["items"]] == ["weekend"]
    assert period["result_type"] == "session"
    assert "read_ref" not in period["items"][0]


async def test_list_projects_bounded_session_context_without_internal_metadata(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="context-rich")
    opening = "Opening context " + ("x" * 400)
    session.append(ChatMessage.user(opening, timestamp=timestamp(1)))
    session.append(ChatMessage.assistant(model="test", content="Answer", timestamp=timestamp(2)))
    sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="coder", session_id="context-rich"),
        {
            "title": "  Useful Session  ",
            "run_kinds": ["user", "subagent", "user"],
            "is_subagent_session": False,
            "subagent_parent": {
                "id": "private-work-id",
                "agent_id": "parent-agent",
                "session_id": "parent-session",
                "project_id": "parent-project",
                "run_id": "private-run-id",
                "tool_call_id": "private-tool-call-id",
                "tool_call_index": 7,
            },
            "platform": " telegram ",
            "fork_source": {
                "agent_id": "source-agent",
                "session_id": "source-session",
                "project_id": "source-project",
                "forked_at": "2026-05-01T12:00:00+00:00",
                "message_count": 99,
            },
            "private_cache_key": "private-cache-value",
        },
    )

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {},
            JsonlSessionRecallBackend(sessions),
        )
    )

    item = data["items"][0]
    assert item["title"] == "Useful Session"
    assert item["run_kinds"] == ["user", "subagent"]
    assert item["is_subagent_session"] is True
    assert item["subagent_parent"] == {
        "agent_id": "parent-agent",
        "session_id": "parent-session",
        "project_id": "parent-project",
    }
    assert item["platform"] == "telegram"
    assert item["fork_source"] == {
        "agent_id": "source-agent",
        "session_id": "source-session",
        "project_id": "source-project",
        "forked_at": "2026-05-01T12:00:00+00:00",
    }
    assert item["message_count"] == 2
    assert len(item["first_user_excerpt"]["text"]) == SESSION_DESCRIPTOR_EXCERPT_MAX_CHARS
    assert item["first_user_excerpt"]["trailing_truncated"] is True
    encoded = json.dumps(data)
    for private_value in (
        "private-work-id",
        "private-run-id",
        "private-tool-call-id",
        "private-cache-value",
    ):
        assert private_value not in encoded


async def test_list_preserves_mixed_run_origins_and_marks_legacy_origin_unknown(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    legacy = sessions.create("coder", session_id="legacy")
    legacy.append(ChatMessage.user("Legacy opening", timestamp=timestamp(1)))
    mixed = sessions.create("coder", session_id="mixed")
    mixed.append(ChatMessage.user("Mixed opening", timestamp=timestamp(2)))
    sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="coder", session_id="mixed"),
        {"run_kinds": ["cron", "user"]},
    )
    backend = JsonlSessionRecallBackend(sessions)

    data = success(await session_search_handler(make_context(tmp_path), {}, backend))
    by_id = {item["session_id"]: item for item in data["items"]}
    legacy_item = by_id["legacy"]
    assert legacy_item["run_kinds"] is None
    assert legacy_item["is_subagent_session"] is None
    assert legacy_item["subagent_parent"] is None
    assert legacy_item["platform"] is None
    assert legacy_item["fork_source"] is None
    assert by_id["mixed"]["run_kinds"] == ["cron", "user"]
    assert by_id["mixed"]["is_subagent_session"] is False


@pytest.mark.parametrize(
    "period",
    ("weekend", "/", "2026-05-03/2026-05-02", "2026-05-01/2026-05-02/2026-05-03"),
)
async def test_invalid_period_is_rejected(tmp_path: Path, period: str) -> None:
    sessions = ChatSessionManager(tmp_path)
    result = await session_search_handler(
        make_context(tmp_path),
        {"period": period},
        JsonlSessionRecallBackend(sessions),
    )

    failure(result, "invalid_arguments")


async def test_search_applies_period_and_backend_default_ranking(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="dated")
    outside = ChatMessage.user("needle old", timestamp=timestamp(1))
    first = ChatMessage.user("needle Saturday", timestamp=timestamp(2))
    second = ChatMessage.assistant(model="test", content="needle Sunday", timestamp=timestamp(3))
    for message in (outside, first, second):
        session.append(message)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {
                "query": "needle",
                "period": "2026-05-02/2026-05-03",
            },
            JsonlSessionRecallBackend(sessions),
        )
    )

    assert [item["message_id"] for item in data["items"]] == [second.id, first.id]
    assert data["ranking"] == "message_time_newest"


async def test_search_returns_one_session_descriptor_for_repeated_hits(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="repeated-context")
    first = ChatMessage.user("needle opening context", timestamp=timestamp(1))
    second = ChatMessage.assistant(model="test", content="needle answer", timestamp=timestamp(2))
    session.append(first)
    session.append(second)
    sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="coder", session_id="repeated-context"),
        {"title": "Repeated context", "run_kinds": ["user"]},
    )

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle"},
            JsonlSessionRecallBackend(sessions),
        )
    )

    assert len(data["items"]) == 2
    assert len(data["sessions"]) == 1
    assert data["sessions"][0] == {
        "agent_id": "coder",
        "session_id": "repeated-context",
        "title": "Repeated context",
        "run_kinds": ["user"],
        "is_subagent_session": False,
        "subagent_parent": None,
        "platform": None,
        "fork_source": None,
        "message_count": 2,
        "first_user_excerpt": {
            "text": "needle opening context",
            "trailing_truncated": False,
        },
    }
    assert all("title" not in item and "run_kinds" not in item for item in data["items"])


async def test_search_returns_at_most_ten_results_without_pagination(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="many-hits")
    messages = [
        ChatMessage.user(f"needle {index}", timestamp=timestamp(index + 1)) for index in range(12)
    ]
    for message in messages:
        session.append(message)
    backend = JsonlSessionRecallBackend(sessions)

    data = success(
        await session_search_handler(make_context(tmp_path), {"query": "needle"}, backend)
    )

    assert len(data["items"]) == 10
    assert data["items"][0]["message_id"] == messages[-1].id
    assert data["has_more"] is True
    assert "next_cursor" not in data


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
    backend = JsonlSessionRecallBackend(sessions)

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
            backend_name="passage_test",
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


async def test_read_does_not_report_corrupt_session_as_not_found(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="corrupt")
    session.path.write_text("{invalid-json}\n", encoding="utf-8")
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

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


async def test_project_scope_is_preserved_for_search_and_read(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    global_session = sessions.create("coder", session_id="global")
    project_session = sessions.create("coder", session_id="project", project_id="p1")
    global_session.append(ChatMessage.user("needle global", timestamp=timestamp(1)))
    project_message = ChatMessage.user("needle project", timestamp=timestamp(2))
    project_session.append(project_message)
    backend = JsonlSessionRecallBackend(sessions)
    search_context = make_context(tmp_path, project_id="p1")
    read_context = make_context(
        tmp_path,
        project_id="p1",
        tool_name=SESSION_READ_TOOL_NAME,
    )

    data = success(await session_search_handler(search_context, {"query": "needle"}, backend))
    exact = success(
        await session_read_handler(
            read_context,
            {
                "session_id": "project",
                "message_id": project_message.id,
            },
            sessions,
        )
    )

    assert [item["session_id"] for item in data["items"]] == ["project"]
    assert exact["items"][0]["message"] == project_message.to_dict()


async def test_fts_search_keeps_backend_relevance(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    sparse = sessions.create("coder", session_id="sparse")
    dense = sessions.create("coder", session_id="dense")
    sparse.append(ChatMessage.user("telegram once " + ("filler " * 500), timestamp=timestamp(3)))
    dense.append(
        ChatMessage.user(
            "telegraminstallation telegram twice",
            timestamp=timestamp(1),
        )
    )
    backend = SqliteFtsRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "telegram"},
            backend,
        )
    )

    assert data["backend"] == "sqlite_fts"
    assert data["ranking"] == "bm25"
    assert [item["session_id"] for item in data["items"]] == ["dense", "sparse"]


@pytest.mark.parametrize("tool_name", ["session_search", "session_read"])
async def test_search_excludes_its_own_persisted_results(
    tmp_path: Path,
    tool_name: str,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="artifact-loop")
    session.append(
        ChatMessage.tool(
            tool_call_id="call-1",
            name=tool_name,
            content="needle artifact",
            timestamp=timestamp(2),
        )
    )
    real = ChatMessage.user("needle real", timestamp=timestamp(1))
    session.append(real)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle"},
            JsonlSessionRecallBackend(sessions),
        )
    )

    assert [item["message_id"] for item in data["items"]] == [real.id]


async def test_legacy_extension_search_is_adapted_without_blocking(tmp_path: Path) -> None:
    caller_thread = threading.get_ident()

    class _SimpleLegacyBackend:
        sessions = ChatSessionManager(tmp_path)

        def __init__(self) -> None:
            self.search_thread: int | None = None

        def search(self, request: Any) -> JsonObject:
            self.search_thread = threading.get_ident()
            return {"matches": [{"query": request.query}]}

    legacy = _SimpleLegacyBackend()
    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "legacy"},
            legacy,
        )
    )

    assert data["result_type"] == "backend_defined"
    assert data["items"][0]["backend_result"]["matches"][0]["query"] == "legacy"
    assert legacy.search_thread is not None
    assert legacy.search_thread != caller_thread


async def test_multiple_large_excerpts_stay_within_result_limit(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="large-excerpts")
    for index in range(3):
        session.append(
            ChatMessage.user(
                f"needle-{index} " + (chr(65 + index) * 30_000),
                timestamp=timestamp(index + 1),
            )
        )

    result = await session_search_handler(
        make_context(tmp_path),
        {"query": "needle"},
        JsonlSessionRecallBackend(sessions),
    )
    data = success(result)

    assert len(data["items"]) == 3
    assert all(
        len(item["excerpt"]["text"]) <= SESSION_SEARCH_EXCERPT_MAX_CHARS for item in data["items"]
    )
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES


async def test_large_session_descriptor_list_returns_bounded_first_ten_without_cursor(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    for index in range(100):
        session_id = f"context-{index:03d}"
        session = sessions.create("coder", session_id=session_id)
        session.append(
            ChatMessage.user("opening " + (str(index % 10) * 400), timestamp=timestamp(1))
        )
        sessions.set_metadata(
            SessionAddress(project_id=None, agent_id="coder", session_id=session_id),
            {
                "title": "T" * 200,
                "run_kinds": ["subagent"],
                "subagent_parent": {
                    "agent_id": "parent-agent",
                    "session_id": "parent-" + ("s" * 100),
                    "project_id": "project-" + ("p" * 100),
                },
            },
        )
    backend = JsonlSessionRecallBackend(sessions)

    result = await session_search_handler(make_context(tmp_path), {}, backend)
    data = success(result)

    assert 0 < len(data["items"]) <= 10
    assert data["has_more"] is True
    assert "next_cursor" not in data
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES
