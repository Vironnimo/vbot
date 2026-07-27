"""Contract tests for the built-in session_search Tool."""

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
    SqliteFtsRecallBackend,
)
from core.recall.hybrid import HybridRecallBackend
from core.recall.vector import VectorRecallBackend
from core.sessions import ChatSessionManager
from core.tools.session_search import (
    SESSION_SEARCH_RESULT_MAX_BYTES,
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    build_session_search_description,
    build_session_search_parameters,
    register_session_search_tool,
)
from core.tools.session_search import (
    session_search_handler as _session_search_handler,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope

pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]


async def session_search_handler(
    context: ToolContext,
    arguments: JsonObject,
    backend: Any,
) -> JsonObject:
    canonical = arguments
    if "action" in arguments:
        fields = dict(arguments)
        operation = fields.pop("action")
        canonical = {"request": {"operation": operation, **fields}}
    elif len(arguments) == 1:
        operation, fields = next(iter(arguments.items()))
        if isinstance(fields, dict):
            canonical = {"request": {"operation": operation, **fields}}
    return await _session_search_handler(context, canonical, backend)


def make_context(
    data_root: Path, *, agent_id: str = "coder", project_id: str | None = None
) -> ToolContext:
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


async def test_registered_schema_exposes_strict_operations_and_matches_jsonl() -> None:
    sessions = ChatSessionManager(Path.cwd())
    registry = ToolRegistry()
    register_session_search_tool(registry, sessions)

    tool = registry.get(SESSION_SEARCH_TOOL_NAME)
    branches = tool.parameters["properties"]["request"]["anyOf"]
    operations = {branch["properties"]["operation"]["enum"][0] for branch in branches}
    assert operations == {"list", "overview", "search", "read"}
    search = next(
        branch for branch in branches if branch["properties"]["operation"]["enum"] == ["search"]
    )
    search_properties = search["properties"]
    assert {"query", "match", "roles", "order"} <= set(search_properties)
    assert search["required"] == ["operation", "query"]
    assert search_properties["order"]["enum"] == ["newest", "oldest"]
    assert tool.description.startswith(SESSION_SEARCH_TOOL_DESCRIPTION)


async def test_schema_changes_with_backend_capabilities(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    context = RecallBackendContext(data_dir=tmp_path, sessions=sessions)

    vector = build_session_search_parameters(VectorRecallBackend(context).search_capabilities())
    hybrid = build_session_search_parameters(HybridRecallBackend(context).search_capabilities())
    vector_search = next(
        branch
        for branch in vector["properties"]["request"]["anyOf"]
        if branch["properties"]["operation"]["enum"] == ["search"]
    )
    hybrid_search = next(
        branch
        for branch in hybrid["properties"]["request"]["anyOf"]
        if branch["properties"]["operation"]["enum"] == ["search"]
    )
    vector_properties = vector_search["properties"]
    hybrid_properties = hybrid_search["properties"]

    assert "match" not in vector_properties
    assert "literal_match" not in vector_properties
    assert "roles" not in vector_properties
    assert "order" not in vector_properties
    assert "literal_match" in hybrid_properties
    assert "match" not in hybrid_properties
    assert "roles" not in hybrid_properties
    assert "order" not in hybrid_properties


async def test_description_explains_active_backend(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    context = RecallBackendContext(data_dir=tmp_path, sessions=sessions)

    assert (
        "literal" in build_session_search_description(JsonlSessionRecallBackend(sessions)).lower()
    )
    assert "meaning" in build_session_search_description(VectorRecallBackend(context)).lower()
    assert "combines" in build_session_search_description(HybridRecallBackend(context)).lower()


async def test_action_is_required_and_fields_are_action_specific(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    backend = JsonlSessionRecallBackend(sessions)

    missing = await session_search_handler(make_context(tmp_path), {"query": "needle"}, backend)
    unsupported = await session_search_handler(
        make_context(tmp_path), {"action": "list", "query": "needle"}, backend
    )

    failure(missing, "invalid_arguments")
    error = failure(unsupported, "invalid_arguments")
    assert "Unsupported arguments for list" in error["message"]


async def test_list_and_overview_are_tool_owned(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="owned-session")
    first = ChatMessage.user("first", timestamp=timestamp(1))
    last = ChatMessage.assistant(model="test", content="last", timestamp=timestamp(2))
    session.append(first)
    session.append(last)
    backend = JsonlSessionRecallBackend(sessions)

    listed = success(await session_search_handler(make_context(tmp_path), {"list": {}}, backend))
    overview = success(
        await session_search_handler(
            make_context(tmp_path),
            {"overview": {"session_id": "owned-session"}},
            backend,
        )
    )

    assert [item["session_id"] for item in listed["items"]] == ["owned-session"]
    assert overview["session"]["message_count"] == 2
    assert overview["session"]["first_message"]["message_id"] == first.id
    assert overview["session"]["last_message"]["message_id"] == last.id


async def test_jsonl_search_returns_every_literal_substring_match_globally_ordered(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    first_session = sessions.create("coder", session_id="first-session")
    second_session = sessions.create("coder", session_id="second-session")
    first = ChatMessage.user("Telegraminstallation one", timestamp=timestamp(1))
    second = ChatMessage.assistant(model="test", content="telegram two", timestamp=timestamp(2))
    third = ChatMessage.user("TELEGRAM three", timestamp=timestamp(3))
    first_session.append(first)
    second_session.append(second)
    first_session.append(third)
    backend = JsonlSessionRecallBackend(sessions)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"action": "search", "query": "telegram", "order": "oldest", "limit": 10},
            backend,
        )
    )

    assert [item["message_id"] for item in data["items"]] == [first.id, second.id, third.id]
    assert data["result_type"] == "message"
    assert data["ranking"] == "message_time_oldest"
    assert all("content" not in item and "context" not in item for item in data["items"])


async def test_search_cursor_returns_more_than_one_hit_without_session_grouping(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="many-hits")
    messages = [
        ChatMessage.user(f"needle {index}", timestamp=timestamp(index + 1)) for index in range(4)
    ]
    for message in messages:
        session.append(message)
    backend = JsonlSessionRecallBackend(sessions)

    first_page = success(
        await session_search_handler(
            make_context(tmp_path),
            {"action": "search", "query": "needle", "order": "oldest", "limit": 2},
            backend,
        )
    )
    second_page = success(
        await session_search_handler(
            make_context(tmp_path),
            {"action": "search", "cursor": first_page["next_cursor"]},
            backend,
        )
    )

    assert [item["message_id"] for item in first_page["items"]] == [
        messages[0].id,
        messages[1].id,
    ]
    assert [item["message_id"] for item in second_page["items"]] == [
        messages[2].id,
        messages[3].id,
    ]
    assert second_page["has_more"] is False


async def test_search_excerpt_is_source_faithful_and_not_hard_capped(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="long-source")
    original = "prefix\n\n" + ("ä  spacing\t" * 500) + " needle suffix"
    message = ChatMessage.user(original, timestamp=timestamp(1))
    session.append(message)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"action": "search", "query": "needle"},
            JsonlSessionRecallBackend(sessions),
        )
    )
    excerpt = data["items"][0]["excerpt"]

    assert excerpt["text"] == original
    assert len(excerpt["text"]) > 360
    assert excerpt["leading_truncated"] is False
    assert excerpt["trailing_truncated"] is False


async def test_multiple_large_excerpts_adapt_only_to_whole_result_limit(tmp_path: Path) -> None:
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
        {"action": "search", "query": "needle", "match": "any_term", "limit": 3},
        JsonlSessionRecallBackend(sessions),
    )
    data = success(result)

    assert len(data["items"]) == 3
    assert all(len(item["excerpt"]["text"]) > 360 for item in data["items"])
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES


async def test_read_returns_exact_canonical_message_and_neighbors(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="exact-read")
    before = ChatMessage.user("before", timestamp=timestamp(1))
    target = ChatMessage.assistant(
        model="test", content="  exact\ntext\tkept  ", timestamp=timestamp(2)
    )
    after = ChatMessage.user("after", timestamp=timestamp(3))
    for message in (before, target, after):
        session.append(message)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {
                "action": "read",
                "session_id": "exact-read",
                "message_id": target.id,
                "before": 1,
                "after": 1,
            },
            JsonlSessionRecallBackend(sessions),
        )
    )

    assert [item["message"] for item in data["items"]] == [
        before.to_dict(),
        target.to_dict(),
        after.to_dict(),
    ]


async def test_oversized_read_record_is_losslessly_segmented(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="segmented-read")
    message = ChatMessage.user("Ü\n" * 70_000, timestamp=timestamp(1))
    session.append(message)
    backend = JsonlSessionRecallBackend(sessions)
    arguments: JsonObject = {
        "action": "read",
        "session_id": "segmented-read",
        "message_id": message.id,
    }
    segments: list[str] = []

    while True:
        result = await session_search_handler(make_context(tmp_path), arguments, backend)
        data = success(result)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES
        segment = data["items"][0]["segment"]
        segments.append(segment["record_json"])
        if not data["has_more"]:
            break
        arguments = {"action": "read", "cursor": data["next_cursor"]}

    assert json.loads("".join(segments)) == message.to_dict()


async def test_project_scope_is_preserved_for_search_and_read(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    global_session = sessions.create("coder", session_id="global")
    project_session = sessions.create("coder", session_id="project", project_id="p1")
    global_session.append(ChatMessage.user("needle global", timestamp=timestamp(1)))
    project_message = ChatMessage.user("needle project", timestamp=timestamp(2))
    project_session.append(project_message)
    backend = JsonlSessionRecallBackend(sessions)

    data = success(
        await session_search_handler(
            make_context(tmp_path, project_id="p1"),
            {"action": "search", "query": "needle"},
            backend,
        )
    )
    exact = success(
        await session_search_handler(
            make_context(tmp_path, project_id="p1"),
            {
                "action": "read",
                "session_id": "project",
                "message_id": project_message.id,
            },
            backend,
        )
    )

    assert [item["session_id"] for item in data["items"]] == ["project"]
    assert exact["items"][0]["message"] == project_message.to_dict()


async def test_fts_search_uses_relevance_and_substring_semantics(tmp_path: Path) -> None:
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
            make_context(tmp_path), {"action": "search", "query": "telegram"}, backend
        )
    )

    assert data["backend"] == "sqlite_fts"
    assert data["ranking"] == "bm25"
    assert [item["session_id"] for item in data["items"]] == ["dense", "sparse"]


@pytest.mark.parametrize("backend_kind", ["jsonl", "fts"])
async def test_search_excludes_its_own_persisted_results_before_limiting(
    tmp_path: Path, backend_kind: str
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="artifact-loop")
    session.append(
        ChatMessage.tool(
            tool_call_id="call-1",
            name="session_search",
            content="needle artifact",
            timestamp=timestamp(2),
        )
    )
    real = ChatMessage.user("needle real", timestamp=timestamp(1))
    session.append(real)
    recall: Any = JsonlSessionRecallBackend(sessions)
    if backend_kind == "fts":
        recall = SqliteFtsRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {
                "action": "search",
                "query": "needle",
                "roles": ["user", "tool"],
                "limit": 1,
            },
            recall,
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
            {"action": "search", "query": "legacy"},
            legacy,
        )
    )

    assert data["result_type"] == "backend_defined"
    assert data["items"][0]["backend_result"]["matches"][0]["query"] == "legacy"
    assert legacy.search_thread is not None
    assert legacy.search_thread != caller_thread


async def test_cursor_rejects_changed_source(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="changing")
    for index in range(2):
        session.append(ChatMessage.user(f"needle {index}", timestamp=timestamp(index + 1)))
    backend = JsonlSessionRecallBackend(sessions)
    first = success(
        await session_search_handler(
            make_context(tmp_path),
            {"action": "search", "query": "needle", "limit": 1},
            backend,
        )
    )
    session.append(ChatMessage.user("needle changed", timestamp=timestamp(3)))

    result = await session_search_handler(
        make_context(tmp_path),
        {"action": "search", "cursor": first["next_cursor"]},
        backend,
    )

    failure(result, "stale_cursor")
