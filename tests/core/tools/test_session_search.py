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
from core.runs import RunKind
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
from tests.core.sessions.history_fixtures import admit_run
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
        {"query": "needle", "period": "2026-07-unknown/2026-07-01"},
        {"query": "needle", "roles": ["user"]},
        {"query": "needle", "match": "phrase"},
        {"query": "needle", "order": "oldest"},
        {"query": "needle", "limit": 0},
        {"query": "needle", "limit": "some"},
        {"query": "needle", "page": 2},
        {"query": "needle", "period": "2026-07-01T09:00"},
        {"cursor": "opaque"},
        {"session_id": "past"},
        {"query": "needle", "include_subagents": "perhaps"},
        {"query": "needle", "q": "different request"},
        {"operation": "list"},
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


@pytest.mark.parametrize(
    "value, included",
    [
        (True, True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        (1, True),
        (False, False),
        ("false", False),
        ("no", False),
        (0, False),
    ],
)
async def test_dispatch_accepts_unambiguous_boolean_encodings(
    tmp_path: Path, value: object, included: bool
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="delegated")
    session.append(ChatMessage.user("needle"))
    await admit_run(sessions, session.address, RunKind.SUBAGENT)
    registry = ToolRegistry()
    register_session_search_tool(registry, CanonicalSessionRecallBackend(sessions))
    arguments = {"query": "needle", "include_subagents": value}
    data = success(await registry.dispatch(make_context(tmp_path), arguments))
    assert bool(data["items"]) is included
    assert arguments["include_subagents"] is value


@pytest.mark.parametrize(
    "arguments",
    [
        {"qurey": "needle", "sessionId": "past"},
        {"query": "needle", "q": " needle ", "session_id": " past ", "session": "past"},
        {"search_query": "needle", "agent": "coder", "session": "past"},
        {"request": {"operation": "SEARCH", "query": "needle", "session_id": "past"}},
        {"action": "search", "query": "needle", "session_id": "past"},
        {"query": "needle", "session_id": "past", "since": "2026-07-01", "until": "2026-07-31"},
        {"query": "needle", "session_id": "past", "period": "2026-07-31/2026-07-01"},
    ],
)
async def test_recognizable_search_intent_reaches_same_session(
    tmp_path: Path, arguments: JsonObject
) -> None:
    import copy

    sessions = ChatSessionManager(tmp_path)
    for name in ("past", "other"):
        sessions.create("coder", session_id=name).append(
            ChatMessage.user("needle", timestamp=timestamp(1).replace(month=7))
        )
    original = copy.deepcopy(arguments)
    registry = ToolRegistry()
    register_session_search_tool(registry, CanonicalSessionRecallBackend(sessions))
    data = success(await registry.dispatch(make_context(tmp_path), arguments))
    assert [hit["session_id"] for hit in data["items"]] == ["past"]
    assert arguments == original


@pytest.mark.parametrize(
    "extra",
    [{"periods": "2026-07-01/2026-07-31"}, {"session": "other"}],
)
async def test_repair_does_not_discard_unknown_effects_or_conflicting_selection(
    tmp_path: Path, extra: JsonObject
) -> None:
    from unittest.mock import AsyncMock

    sessions = ChatSessionManager(tmp_path)
    backend = CanonicalSessionRecallBackend(sessions)
    backend.search_page = AsyncMock()  # type: ignore[method-assign]
    registry = ToolRegistry()
    register_session_search_tool(registry, backend)
    result = await registry.dispatch(
        make_context(tmp_path), {"query": "needle", "session_id": "past", **extra}
    )
    failure(result, "invalid_arguments")
    backend.search_page.assert_not_awaited()


@pytest.mark.parametrize("extra", [{"limit": None}, {"roles": ""}, {"session": None}])
async def test_empty_unknown_arguments_are_dropped_and_the_search_runs(
    tmp_path: Path, extra: JsonObject
) -> None:
    sessions = ChatSessionManager(tmp_path)
    for name in ("past", "other"):
        sessions.create("coder", session_id=name).append(
            ChatMessage.user("needle", timestamp=timestamp(1))
        )
    registry = ToolRegistry()
    register_session_search_tool(registry, CanonicalSessionRecallBackend(sessions))

    result = await registry.dispatch(
        make_context(tmp_path), {"query": "needle", "session_id": "past", **extra}
    )

    assert [hit["session_id"] for hit in success(result)["items"]] == ["past"]


async def test_duplicate_encoded_search_targets_are_rejected_before_search(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    result = await session_search_handler(
        make_context(tmp_path),
        '{"query":"needle","session_id":"one","session_id":"two"}',  # type: ignore[arg-type]
        CanonicalSessionRecallBackend(sessions),
    )
    failure(result, "invalid_arguments")
