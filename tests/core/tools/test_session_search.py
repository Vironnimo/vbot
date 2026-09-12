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
from core.sessions import ChatSessionManager
from core.tools.session_search import (
    SESSION_SEARCH_TOOL_NAME,
    build_session_search_description,
    build_session_search_parameters,
    register_session_search_tool,
    session_search_handler,
)
from core.tools.tools import ToolRegistry
from scripts.provider_probe.choices import SESSION_SEARCH_CASES
from scripts.provider_probe.scenario_history import _session_search_scenario
from tests.core.tools.session_search_helpers import (
    JsonObject,
    failure,
    make_context,
    success,
    timestamp,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


@pytest.mark.parametrize("case", SESSION_SEARCH_CASES)
async def test_luna_matrix_arguments_execute_against_canonical_fixture(
    tmp_path: Path, case: str
) -> None:
    sessions = ChatSessionManager(tmp_path)
    for agent in ("coder", "tester"):
        sessions.create(agent, session_id="session-123").append(
            ChatMessage.user("Tool schema defaults", timestamp=timestamp(1).replace(month=7))
        )
    backend = SqliteFtsRecallBackend(RecallBackendContext(tmp_path, sessions))
    arguments = _session_search_scenario(case).expected_arguments
    assert arguments is not None
    data = success(await session_search_handler(make_context(tmp_path), arguments, backend))
    assert len(data["items"]) == 1
    assert data["items"][0]["agent_id"] == arguments.get("agent_id", "coder")


async def test_registration_exposes_only_required_query_search(tmp_path: Path) -> None:
    from core.recall import RecallBackendRegistry

    sessions = ChatSessionManager(tmp_path)
    backends = RecallBackendRegistry.with_builtins()
    assert set(backends.names()) == {"sqlite_fts", "vector", "hybrid"}
    with pytest.raises(KeyError):
        backends.create("canonical_scan", RecallBackendContext(tmp_path, sessions))
    for name in backends.names():
        registry = ToolRegistry()
        backend = backends.create(name, RecallBackendContext(tmp_path, sessions))
        register_session_search_tool(registry, backend, sessions)
        search = registry.get(SESSION_SEARCH_TOOL_NAME)
        assert "session_read" not in [tool.name for tool in registry.list_tools()]
        assert search.parameters["required"] == ["query"]
        assert set(search.parameters["properties"]) == {
            "query",
            "period",
            "agent_id",
            "session_id",
            "include_subagents",
        }
        assert "additionalProperties" not in search.parameters
        assert search.description == build_session_search_description(backend)
        assert search.parameters == build_session_search_parameters(backend)


async def test_other_agent_same_session_id_remains_searchable(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    for agent in ("coder", "reviewer"):
        sessions.create(agent, session_id="current-session").append(ChatMessage.user("needle"))
    backend = CanonicalSessionRecallBackend(sessions)
    own = success(
        await session_search_handler(make_context(tmp_path), {"query": "needle"}, backend)
    )
    other = success(
        await session_search_handler(
            make_context(tmp_path), {"query": "needle", "agent_id": "reviewer"}, backend
        )
    )
    assert own["items"] == []
    assert other["items"][0]["agent_id"] == "reviewer"


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


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"query": ""},
        {"query": " "},
        {"query": "needle", "period": "/"},
        {"query": "needle", "period": "2026-07-02/2026-07-01"},
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
