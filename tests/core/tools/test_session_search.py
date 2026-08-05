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
from core.sessions import ChatSessionManager
from core.tools.session_search import (
    SESSION_READ_TOOL_NAME,
    SESSION_READ_TOOL_PARAMETERS,
    SESSION_SEARCH_RESULT_MAX_BYTES,
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
    build_session_search_description,
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
        "limit",
        "cursor",
    }
    assert set(read.parameters["properties"]) == {
        "session_id",
        "agent_id",
        "start_message_id",
        "end_message_id",
        "cursor",
    }
    assert search.parameters == SESSION_SEARCH_TOOL_PARAMETERS
    assert read.parameters == SESSION_READ_TOOL_PARAMETERS
    assert "oneOf" not in search.parameters
    assert "additionalProperties" not in search.parameters
    assert search.parameters["required"] == []
    assert search.parameters["properties"]["limit"]["default"] == 10
    assert "oneOf" not in read.parameters
    assert "additionalProperties" not in read.parameters
    assert read.parameters["required"] == []
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
    for backend in (
        JsonlSessionRecallBackend(sessions),
        VectorRecallBackend(context),
        HybridRecallBackend(context),
    ):
        backend_registry = ToolRegistry()
        register_session_search_tool(backend_registry, backend, sessions)
        assert backend_registry.get(SESSION_SEARCH_TOOL_NAME).parameters == search.parameters


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"session_id": "target", "unexpected": True},
        {"session_id": "target", "cursor": "opaque"},
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


async def test_description_explains_active_backend(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    context = RecallBackendContext(data_dir=tmp_path, sessions=sessions)

    assert (
        "literal" in build_session_search_description(JsonlSessionRecallBackend(sessions)).lower()
    )
    assert "meaning" in build_session_search_description(VectorRecallBackend(context)).lower()
    assert "combines" in build_session_search_description(HybridRecallBackend(context)).lower()


@pytest.mark.parametrize(
    "arguments",
    (
        {"request": {"operation": "search", "query": "needle"}},
        {"action": "search", "query": "needle"},
        {"query": "needle", "roles": ["user"]},
        {"query": "needle", "match": "phrase"},
        {"query": "needle", "order": "oldest"},
        {"query": "needle", "since": "2026-05-01"},
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


async def test_list_supports_period_and_session_filters(tmp_path: Path) -> None:
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
    selected = success(
        await session_search_handler(
            make_context(tmp_path),
            {"session_id": "weekday"},
            backend,
        )
    )

    assert [item["session_id"] for item in period["items"]] == ["weekend"]
    assert period["result_type"] == "session"
    assert period["items"][0]["read_ref"] == {
        "agent_id": "coder",
        "session_id": "weekend",
        "start_message_id": weekend_question.id,
        "end_message_id": weekend_answer.id,
    }
    assert [item["session_id"] for item in selected["items"]] == ["weekday"]


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
                "limit": 10,
            },
            JsonlSessionRecallBackend(sessions),
        )
    )

    assert [item["message_id"] for item in data["items"]] == [second.id, first.id]
    assert data["ranking"] == "message_time_newest"


async def test_search_cursor_continues_with_cursor_alone(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="many-hits")
    messages = [
        ChatMessage.user(f"needle {index}", timestamp=timestamp(index + 1)) for index in range(3)
    ]
    for message in messages:
        session.append(message)
    backend = JsonlSessionRecallBackend(sessions)

    first = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle", "limit": 1},
            backend,
        )
    )
    second = success(
        await session_search_handler(
            make_context(tmp_path),
            {"cursor": first["next_cursor"]},
            backend,
        )
    )
    mixed = await session_search_handler(
        make_context(tmp_path),
        {"query": "needle", "cursor": first["next_cursor"]},
        backend,
    )

    assert first["items"][0]["message_id"] == messages[2].id
    assert second["items"][0]["message_id"] == messages[1].id
    failure(mixed, "invalid_arguments")


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
        "start_message_id": question.id,
        "end_message_id": second.id,
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
        "start_message_id": question.id,
        "end_message_id": second.id,
    }


async def test_read_supports_whole_session_and_open_or_closed_ranges(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="ranges")
    messages = [
        ChatMessage.user(f"message {index}", timestamp=timestamp(index + 1)) for index in range(4)
    ]
    for message in messages:
        session.append(message)
    context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)

    whole = success(await session_read_handler(context, {"session_id": "ranges"}, sessions))
    tail = success(
        await session_read_handler(
            context,
            {"session_id": "ranges", "start_message_id": messages[2].id},
            sessions,
        )
    )
    head = success(
        await session_read_handler(
            context,
            {"session_id": "ranges", "end_message_id": messages[1].id},
            sessions,
        )
    )
    exact = success(
        await session_read_handler(
            context,
            {
                "session_id": "ranges",
                "start_message_id": messages[1].id,
                "end_message_id": messages[2].id,
            },
            sessions,
        )
    )

    assert [item["message"] for item in whole["items"]] == [
        message.to_dict() for message in messages
    ]
    assert [item["message"] for item in tail["items"]] == [
        message.to_dict() for message in messages[2:]
    ]
    assert [item["message"] for item in head["items"]] == [
        message.to_dict() for message in messages[:2]
    ]
    assert [item["message"] for item in exact["items"]] == [
        message.to_dict() for message in messages[1:3]
    ]
    assert whole["session"]["message_count"] == 4
    assert whole["session"]["first_message"]["message_id"] == messages[0].id
    assert whole["session"]["last_message"]["message_id"] == messages[-1].id


async def test_oversized_read_record_is_losslessly_segmented(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="segmented-read")
    message = ChatMessage.user("Ü\n" * 70_000, timestamp=timestamp(1))
    session.append(message)
    arguments: JsonObject = {
        "session_id": "segmented-read",
        "start_message_id": message.id,
        "end_message_id": message.id,
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
        segments.append(data["items"][0]["segment"]["record_json"])
        if not data["has_more"]:
            break
        arguments = {"cursor": data["next_cursor"]}

    assert json.loads("".join(segments)) == message.to_dict()


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
                "start_message_id": project_message.id,
                "end_message_id": project_message.id,
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
            {"query": "needle", "limit": 1},
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


async def test_cursors_reject_changed_source_and_cross_tool_reuse(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="changing")
    first_message = ChatMessage.user("needle 1", timestamp=timestamp(1))
    second_message = ChatMessage.user("needle 2", timestamp=timestamp(2))
    session.append(first_message)
    session.append(second_message)
    backend = JsonlSessionRecallBackend(sessions)

    search = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle", "limit": 1},
            backend,
        )
    )
    cross_tool = await session_read_handler(
        make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME),
        {"cursor": search["next_cursor"]},
        sessions,
    )
    session.append(ChatMessage.user("needle changed", timestamp=timestamp(3)))
    stale = await session_search_handler(
        make_context(tmp_path),
        {"cursor": search["next_cursor"]},
        backend,
    )

    failure(cross_tool, "invalid_cursor")
    failure(stale, "stale_cursor")


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
        {"query": "needle", "limit": 3},
        JsonlSessionRecallBackend(sessions),
    )
    data = success(result)

    assert len(data["items"]) == 3
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES
