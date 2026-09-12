"""Session search: contract behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.recall import (
    CanonicalSessionRecallBackend,
    RecallBackendContext,
    SqliteFtsRecallBackend,
)
from core.recall.canonical import RECALL_TOOL_RESULT_NAMES
from core.recall.hybrid import HybridRecallBackend
from core.recall.vector import VectorRecallBackend
from core.sessions import ChatSessionManager
from core.tools.session_search import (
    SESSION_READ_TOOL_NAME,
    SESSION_READ_TOOL_PARAMETERS,
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
    build_session_search_description,
    build_session_search_parameters,
    register_session_search_tool,
    session_read_handler,
    session_search_handler,
)
from core.tools.tools import ToolRegistry
from tests.core.tools.session_search_helpers import (
    JsonObject,
    failure,
    make_context,
    success,
    timestamp,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


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
        "include_subagents",
    }
    assert set(read.parameters["properties"]) == {
        "session_id",
        "message_id",
        "agent_id",
        "continuation",
        "all_messages",
        "include_subagents",
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
        ("canonical_scan", CanonicalSessionRecallBackend(sessions)),
        ("sqlite_fts", SqliteFtsRecallBackend(context)),
        ("vector", VectorRecallBackend(context)),
        ("hybrid", HybridRecallBackend(context)),
    ):
        backend_registry = ToolRegistry()
        register_session_search_tool(backend_registry, backend, sessions)
        definition = backend_registry.get(SESSION_SEARCH_TOOL_NAME)
        backend_definitions[name] = definition
        assert set(definition.parameters["properties"]) == set(search.parameters["properties"])

    assert backend_definitions["canonical_scan"].parameters == SESSION_SEARCH_TOOL_PARAMETERS
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
    for field in ("period", "agent_id", "session_id", "include_subagents"):
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
        {"session_id": "target", "include_subagents": "yes"},
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
    backend = CanonicalSessionRecallBackend(sessions)
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
    backend = CanonicalSessionRecallBackend(sessions)
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
    backend = CanonicalSessionRecallBackend(sessions)
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
    suffix = (
        "Delegated Sub-Agent work is excluded unless include_subagents is true. Omit query to "
        "list recent Sessions. Returns up to 10 excerpts with no paging; narrow with period or "
        "session_id. Use a returned read_ref with session_read when exact context matters. The "
        "current Session is unavailable."
    )

    expected = {
        "canonical_scan": (
            f"Find persisted Sessions and literal matches in past conversations. {suffix}",
            "Literal terms to find. Every whitespace-separated term must occur as a "
            "case-insensitive substring; synonyms and paraphrases do not match. Omit to list "
            "recent Sessions. Matches are newest first.",
        ),
        "sqlite_fts": (
            "Find persisted Sessions and relevance-ranked literal matches in past conversations. "
            f"{suffix}",
            "Literal terms to find. Every whitespace-separated term must occur. One- or "
            "two-character terms match whole tokens; longer terms also match inside words. "
            "Omit to list recent Sessions. Matches are ranked by text relevance.",
        ),
        "vector": (
            "Find persisted Sessions and semantically related passages from past conversations. "
            f"{suffix}",
            "Short topic description to find by meaning. Bare keywords anchor poorly and exact "
            "occurrences may be missed. Omit to list recent Sessions. Matches are ranked by "
            "semantic relevance.",
        ),
        "hybrid": (
            "Find persisted Sessions and relevant passages using literal and semantic search. "
            f"{suffix}",
            "Literal terms or a short topic description. Every whitespace-separated term is "
            "required by literal search; the same query is also searched by meaning. Omit to "
            "list recent Sessions. Matches combine both rankings by relevance.",
        ),
    }
    backends = {
        "canonical_scan": CanonicalSessionRecallBackend(sessions),
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
        {"query": "needle", "include_subagents": "yes"},
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
        CanonicalSessionRecallBackend(sessions),
    )

    failure(result, "invalid_arguments")
