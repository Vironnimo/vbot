"""Tests for the built-in session_search tool."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import RecallBackendContext
from core.recall.hybrid import _HYBRID_SEARCH_GUIDANCE, HybridRecallBackend
from core.recall.jsonl import SESSION_RECALL_LITERAL_SEARCH_GUIDANCE, JsonlSessionRecallBackend
from core.recall.vector import _SEMANTIC_SEARCH_GUIDANCE, VectorRecallBackend
from core.sessions import ChatSessionManager
from core.tools.session_search import (
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
    build_session_search_description,
    register_session_search_tool,
    session_search_handler,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope

JsonObject = dict[str, Any]


def make_context(data_root: Path, *, agent_id: str = "coder") -> ToolContext:
    workspace = data_root / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        agent_id=agent_id,
        session_id="current-session",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=SESSION_SEARCH_TOOL_NAME,
        tool_call_index=0,
        workspace=workspace,
        app_root=data_root.parent,
        data_root=data_root,
    )


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def assert_success_envelope(result: JsonObject) -> JsonObject:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    return data


def assert_failure_envelope(result: JsonObject, code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    return error  # type: ignore[return-value]


def test_register_session_search_tool_exposes_provider_schema(tmp_path: Path) -> None:
    registry = ToolRegistry()
    sessions = ChatSessionManager(tmp_path)

    register_session_search_tool(registry, sessions)

    tool = registry.get("session_search")
    assert tool.name == SESSION_SEARCH_TOOL_NAME == "session_search"
    # A bare ChatSessionManager is wrapped in the JSONL backend, whose
    # describe_search fragment (literal-substring guidance) is appended.
    assert tool.description.startswith(SESSION_SEARCH_TOOL_DESCRIPTION)
    assert SESSION_RECALL_LITERAL_SEARCH_GUIDANCE in tool.description
    assert tool.parameters == SESSION_SEARCH_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["session_search"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["name"] == "session_search"
    assert definition["description"].startswith(SESSION_SEARCH_TOOL_DESCRIPTION)

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {
        "agent_id",
        "around_message_id",
        "bookends",
        "context",
        "limit",
        "match",
        "query",
        "roles",
        "session_id",
        "since",
        "sort",
        "until",
    }


def test_build_session_search_description_appends_backend_guidance(tmp_path: Path) -> None:
    """Each backend contributes its own how-to-query guidance to the description."""

    sessions = ChatSessionManager(tmp_path)
    context = RecallBackendContext(data_dir=tmp_path, sessions=sessions)

    jsonl_description = build_session_search_description(JsonlSessionRecallBackend(sessions))
    vector_description = build_session_search_description(VectorRecallBackend(context))
    hybrid_description = build_session_search_description(HybridRecallBackend(context))

    # JSONL/literal default.
    assert jsonl_description == (
        f"{SESSION_SEARCH_TOOL_DESCRIPTION} {SESSION_RECALL_LITERAL_SEARCH_GUIDANCE}"
    )
    # Vector and hybrid override with their own capability text.
    assert vector_description.startswith(SESSION_SEARCH_TOOL_DESCRIPTION)
    assert _SEMANTIC_SEARCH_GUIDANCE in vector_description
    assert SESSION_RECALL_LITERAL_SEARCH_GUIDANCE not in vector_description
    assert hybrid_description.startswith(SESSION_SEARCH_TOOL_DESCRIPTION)
    assert _HYBRID_SEARCH_GUIDANCE in hybrid_description


def test_build_session_search_description_falls_back_without_describe_search() -> None:
    """A backend without describe_search (e.g. an extension) gets the generic base."""

    class _BareBackend:
        def browse(self, request: Any) -> Any: ...

        def overview(self, request: Any) -> Any: ...

        def search(self, request: Any) -> Any: ...

        def scroll(self, request: Any) -> Any: ...

    assert build_session_search_description(_BareBackend()) == SESSION_SEARCH_TOOL_DESCRIPTION


def test_session_search_finds_all_query_terms_in_newest_sessions(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    old_session = sessions.create("coder", session_id="old-session")
    new_session = sessions.create("coder", session_id="new-session")
    old_session.append(ChatMessage.user("Release notes mention migration", timestamp=timestamp(1)))
    new_session.append(
        ChatMessage.user("Release deploy plan for session search", timestamp=timestamp(3))
    )

    result = session_search_handler(
        make_context(tmp_path),
        {"query": "release deploy"},
        sessions,
    )

    data = assert_success_envelope(result)
    matches = data["matches"]
    assert isinstance(matches, list)
    assert len(matches) == 1
    assert matches[0]["session_id"] == "new-session"
    assert matches[0]["role"] == "user"
    assert "Release deploy" in matches[0]["snippet"]
    assert "Found 1 match" in data["content"]


def test_session_search_filters_by_time_range_and_role(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    old_session = sessions.create("coder", session_id="old-session")
    new_session = sessions.create("coder", session_id="new-session")
    old_session.append(
        ChatMessage.assistant(model="openai/gpt-5", content="needle", timestamp=timestamp(1))
    )
    new_session.append(ChatMessage.user("needle from user", timestamp=timestamp(3, 9)))
    new_session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="needle from assistant",
            timestamp=timestamp(3, 10),
        )
    )

    result = session_search_handler(
        make_context(tmp_path),
        {"query": "needle", "since": "2026-05-02", "roles": ["assistant"]},
        sessions,
    )

    data = assert_success_envelope(result)
    matches = data["matches"]
    assert isinstance(matches, list)
    assert [match["session_id"] for match in matches] == ["new-session"]
    assert matches[0]["snippet"] == "needle from assistant"


def test_session_search_lists_recent_sessions_without_query(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    old_session = sessions.create("coder", session_id="old-session")
    new_session = sessions.create("coder", session_id="new-session")
    old_session.append(ChatMessage.user("older work", timestamp=timestamp(1)))
    new_session.append(ChatMessage.user("newer work", timestamp=timestamp(4)))
    sessions.set_metadata("coder", "new-session", {"platform": "telegram"})

    result = session_search_handler(make_context(tmp_path), {"limit": 1}, sessions)

    data = assert_success_envelope(result)
    session_summaries = data["sessions"]
    assert isinstance(session_summaries, list)
    assert len(session_summaries) == 1
    assert session_summaries[0]["session_id"] == "new-session"
    assert session_summaries[0]["metadata"] == {"platform": "telegram"}
    assert data["truncated"] is True
    assert "Results limited to 1 sessions" in data["content"]


def test_session_search_blank_query_lists_sessions(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="blank-query-session")
    session.append(ChatMessage.user("newer work", timestamp=timestamp(4)))

    result = session_search_handler(make_context(tmp_path), {"query": "   "}, sessions)

    data = assert_success_envelope(result)
    session_summaries = data["sessions"]
    assert isinstance(session_summaries, list)
    assert [session_summary["session_id"] for session_summary in session_summaries] == [
        "blank-query-session"
    ]


def test_session_search_includes_neighbor_context_when_requested(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="thread-session")
    session.append(ChatMessage.user("Can you inspect checkout bug?", timestamp=timestamp(2, 9)))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="I will inspect the payment flow.",
            timestamp=timestamp(2, 10),
        )
    )
    session.append(ChatMessage.user("Checkout bug reproduces again", timestamp=timestamp(2, 11)))

    result = session_search_handler(
        make_context(tmp_path),
        {"query": "checkout bug", "limit": 1, "context": 1},
        sessions,
    )

    data = assert_success_envelope(result)
    matches = data["matches"]
    assert isinstance(matches, list)
    context = matches[0]["context"]
    assert context["before"] == []
    assert context["after"][0]["role"] == "assistant"
    assert context["after"][0]["snippet"] == "I will inspect the payment flow."
    assert matches[0]["window"][0]["role"] == "user"
    assert matches[0]["bookend_end"][-1]["role"] == "user"


def test_session_search_returns_anchored_view_around_message(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="anchored-session")
    session.append(ChatMessage.user("Session goal is memory planning", timestamp=timestamp(2, 8)))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="We can keep JSONL canonical.",
            timestamp=timestamp(2, 9),
        )
    )
    anchor = ChatMessage.user("Now inspect SQLite FTS options", timestamp=timestamp(2, 10))
    session.append(anchor)
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="SQLite FTS should be an optional index.",
            timestamp=timestamp(2, 11),
        )
    )

    result = session_search_handler(
        make_context(tmp_path),
        {"session_id": "anchored-session", "around_message_id": anchor.id, "context": 1},
        sessions,
    )

    data = assert_success_envelope(result)
    assert data["around_message_id"] == anchor.id
    assert [item["role"] for item in data["window"]] == ["assistant", "user", "assistant"]
    assert data["bookend_start"][0]["snippet"] == "Session goal is memory planning"
    assert data["bookend_end"] == []
    assert "Anchored view" in data["content"]


def test_session_search_returns_session_overview_for_session_id_alone(tmp_path: Path) -> None:
    """session_id without a query returns that session's overview, not just metadata."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="overview-session")
    session.append(ChatMessage.user("first request about caching", timestamp=timestamp(2, 8)))
    session.append(
        ChatMessage.assistant(model="openai/gpt-5", content="middle one", timestamp=timestamp(2, 9))
    )
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5", content="middle two", timestamp=timestamp(2, 10)
        )
    )
    session.append(ChatMessage.user("final wrap-up question", timestamp=timestamp(2, 11)))

    result = session_search_handler(
        make_context(tmp_path),
        {"session_id": "overview-session", "bookends": 1},
        sessions,
    )

    data = assert_success_envelope(result)
    assert data["session"]["session_id"] == "overview-session"
    assert data["total_messages"] == 4
    assert [item["snippet"] for item in data["bookend_start"]] == ["first request about caching"]
    assert [item["snippet"] for item in data["bookend_end"]] == ["final wrap-up question"]
    assert data["truncated"] is True
    assert "2 message(s) omitted" in data["content"]


def test_session_overview_returns_all_messages_when_bookends_cover_session(tmp_path: Path) -> None:
    """A short session shows every message once, with no overlap and nothing omitted."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="small-session")
    session.append(ChatMessage.user("only question", timestamp=timestamp(2, 8)))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5", content="only answer", timestamp=timestamp(2, 9)
        )
    )

    result = session_search_handler(
        make_context(tmp_path),
        {"session_id": "small-session", "bookends": 3},
        sessions,
    )

    data = assert_success_envelope(result)
    assert data["total_messages"] == 2
    assert [item["snippet"] for item in data["bookend_start"]] == ["only question", "only answer"]
    assert data["bookend_end"] == []
    assert data["truncated"] is False


def test_session_overview_reports_missing_session(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)

    result = session_search_handler(
        make_context(tmp_path),
        {"session_id": "does-not-exist"},
        sessions,
    )

    data = assert_success_envelope(result)
    assert data["session"] is None
    assert data["total_messages"] == 0
    assert "No session found" in data["content"]


def test_session_search_anchors_on_message_outside_default_roles(tmp_path: Path) -> None:
    """An explicit anchor id surfaces the message even when its role is filtered out."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="tool-anchor-session")
    session.append(ChatMessage.user("run the build", timestamp=timestamp(2, 8)))
    tool_message = ChatMessage.tool(
        tool_call_id="c1",
        name="bash",
        content="build output dump",
        timestamp=timestamp(2, 9),
    )
    session.append(tool_message)

    result = session_search_handler(
        make_context(tmp_path),
        {"session_id": "tool-anchor-session", "around_message_id": tool_message.id},
        sessions,
    )

    data = assert_success_envelope(result)
    assert data["around_message_id"] == tool_message.id
    assert any(item["message_id"] == tool_message.id for item in data["window"])


def test_session_search_requires_session_for_anchored_view(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)

    result = session_search_handler(
        make_context(tmp_path),
        {"around_message_id": "message-1"},
        sessions,
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "requires session_id" in error["message"]


def test_session_search_excludes_notes_until_requested(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="note-session")
    session.add_note("hidden needle")

    default_result = session_search_handler(
        make_context(tmp_path), {"query": "hidden needle"}, sessions
    )
    note_result = session_search_handler(
        make_context(tmp_path),
        {"query": "hidden needle", "roles": ["note"]},
        sessions,
    )

    default_data = assert_success_envelope(default_result)
    note_data = assert_success_envelope(note_result)
    assert default_data["matches"] == []
    assert len(note_data["matches"]) == 1
    assert note_data["matches"][0]["role"] == "note"


def test_session_search_excludes_tool_results_until_requested(tmp_path: Path) -> None:
    """Tool results are opt-in: a default search skips them; ``roles: ["tool"]`` finds them."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="tool-session")
    session.append(
        ChatMessage.tool(
            tool_call_id="c1",
            name="bash",
            content="checkout bug stack trace from the shell",
            timestamp=timestamp(1),
        )
    )

    default_result = session_search_handler(
        make_context(tmp_path), {"query": "checkout bug"}, sessions
    )
    tool_result = session_search_handler(
        make_context(tmp_path),
        {"query": "checkout bug", "roles": ["tool"]},
        sessions,
    )

    default_data = assert_success_envelope(default_result)
    tool_data = assert_success_envelope(tool_result)
    assert default_data["matches"] == []
    assert len(tool_data["matches"]) == 1
    assert tool_data["matches"][0]["role"] == "tool"


def test_session_search_excludes_tool_results_from_context_until_requested(
    tmp_path: Path,
) -> None:
    """A tool result next to a match is hidden from context by default, shown when requested."""

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="ctx-session")
    session.append(ChatMessage.user("inspect the checkout bug", timestamp=timestamp(1)))
    session.append(
        ChatMessage.tool(
            tool_call_id="c1",
            name="bash",
            content="raw ansi terminal dump",
            timestamp=timestamp(2),
        )
    )

    default_result = session_search_handler(
        make_context(tmp_path),
        {"query": "checkout bug", "context": 1, "bookends": 0},
        sessions,
    )
    tool_result = session_search_handler(
        make_context(tmp_path),
        {"query": "checkout bug", "roles": ["user", "tool"], "context": 1, "bookends": 0},
        sessions,
    )

    default_data = assert_success_envelope(default_result)
    tool_data = assert_success_envelope(tool_result)
    # The adjacent tool result is not a neighbor of the match by default.
    assert default_data["matches"][0]["context"]["after"] == []
    # ...but it shows as context once the caller opts into tool results.
    assert tool_data["matches"][0]["context"]["after"][0]["role"] == "tool"


def test_session_search_excludes_its_own_prior_results(tmp_path: Path) -> None:
    """A persisted session_search result is never returned as a match.

    Its output is the recall tool's own derived content; returning it makes a
    search match its previous results (a feedback loop). The real user message
    in the same session still matches.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="loopy")
    session.append(
        ChatMessage.tool(
            tool_call_id="c1",
            name="session_search",
            content="Found 3 match(es) for query: needle",
            timestamp=timestamp(1),
        )
    )
    session.append(ChatMessage.user("the real needle is here", timestamp=timestamp(2)))

    result = session_search_handler(make_context(tmp_path), {"query": "needle"}, sessions)

    data = assert_success_envelope(result)
    matches = data["matches"]
    assert len(matches) == 1
    assert matches[0]["role"] == "user"
    assert matches[0]["snippet"] == "the real needle is here"


def test_recall_tool_result_name_matches_session_search_tool_name() -> None:
    """Drift guard: the recall layer's excluded-tool name must equal the tool's name.

    ``core.recall`` cannot import ``core.tools`` (lower layer), so it keeps a
    private copy of the tool name; this asserts the copy stays in sync.
    """

    from core.recall.jsonl import RECALL_TOOL_RESULT_NAME

    assert RECALL_TOOL_RESULT_NAME == SESSION_SEARCH_TOOL_NAME


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"context": 3}, "context"),
        ({"roles": ["system"]}, "roles"),
        ({"since": "soon"}, "since"),
        ({"unknown": True}, "Unknown argument"),
    ],
)
def test_session_search_rejects_invalid_arguments(
    tmp_path: Path,
    arguments: JsonObject,
    message: str,
) -> None:
    sessions = ChatSessionManager(tmp_path)

    result = session_search_handler(make_context(tmp_path), arguments, sessions)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert message in error["message"]
