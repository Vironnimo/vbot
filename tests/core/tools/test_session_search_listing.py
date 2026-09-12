"""Session search: listing behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.recall import (
    CanonicalSessionRecallBackend,
)
from core.sessions import ChatSession, ChatSessionManager, SessionAddress, SessionDescriptorSource
from core.tools._session_recall_results import (
    SESSION_DESCRIPTOR_EXCERPT_MAX_CHARS,
)
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


async def test_list_supports_period_filter_without_loading_histories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    backend = CanonicalSessionRecallBackend(sessions)

    def fail_history_load(_session: ChatSession) -> list[ChatMessage]:
        raise AssertionError("period filtering must not load complete Session history")

    monkeypatch.setattr(ChatSession, "load", fail_history_load)

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
            {"include_subagents": True},
            CanonicalSessionRecallBackend(sessions),
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
    backend = CanonicalSessionRecallBackend(sessions)

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


async def test_session_tools_hide_internal_work_and_require_subagent_opt_in(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    definitions: dict[str, JsonObject] = {
        "user": {"run_kinds": ["user"]},
        "channel": {"run_kinds": ["channel"]},
        "cron": {"run_kinds": ["cron"]},
        "legacy": {},
        "system": {"run_kinds": ["system"]},
        "reflection": {"run_kinds": ["reflection"]},
        "memory-reflection": {"run_kinds": ["memory_reflection"]},
        "skill-reflection": {"run_kinds": ["skill_reflection"]},
        "mixed-reflection": {"run_kinds": ["user", "skill_reflection"]},
        "subagent-kind": {"run_kinds": ["subagent"]},
        "subagent-flag": {"is_subagent_session": True},
    }
    messages: dict[str, ChatMessage] = {}
    for day, (session_id, metadata) in enumerate(definitions.items(), start=1):
        session = sessions.create("coder", session_id=session_id)
        message = ChatMessage.user(f"visibilityneedle {session_id}", timestamp=timestamp(day))
        session.append(message)
        messages[session_id] = message
        if metadata:
            sessions.set_metadata(
                SessionAddress(project_id=None, agent_id="coder", session_id=session_id),
                metadata,
            )
    backend = CanonicalSessionRecallBackend(sessions)
    context = make_context(tmp_path)
    read_context = make_context(tmp_path, tool_name=SESSION_READ_TOOL_NAME)
    public_ids = {"user", "channel", "cron", "legacy"}
    subagent_ids = {"subagent-kind", "subagent-flag"}
    always_hidden_ids = {
        "system",
        "reflection",
        "memory-reflection",
        "skill-reflection",
        "mixed-reflection",
    }

    listed = success(await session_search_handler(context, {}, backend))
    searched = success(
        await session_search_handler(context, {"query": "visibilityneedle"}, backend)
    )
    opted_in = success(
        await session_search_handler(
            context,
            {"query": "visibilityneedle", "include_subagents": True},
            backend,
        )
    )

    assert {item["session_id"] for item in listed["items"]} == public_ids
    assert {item["session_id"] for item in searched["items"]} == public_ids
    assert {item["session_id"] for item in opted_in["items"]} == public_ids | subagent_ids

    subagent_hit = next(item for item in opted_in["items"] if item["session_id"] == "subagent-kind")
    assert subagent_hit["read_ref"]["include_subagents"] is True
    subagent_read = success(
        await session_read_handler(read_context, subagent_hit["read_ref"], sessions)
    )
    assert subagent_read["items"][0]["message"] == messages["subagent-kind"].to_dict()

    blocked_subagent_read = await session_read_handler(
        read_context,
        {"session_id": "subagent-kind"},
        sessions,
    )
    failure(blocked_subagent_read, "session_not_found")

    for session_id in always_hidden_ids:
        scoped = success(
            await session_search_handler(
                context,
                {
                    "query": "visibilityneedle",
                    "session_id": session_id,
                    "include_subagents": True,
                },
                backend,
            )
        )
        assert scoped["items"] == []
        hidden_read = await session_read_handler(
            read_context,
            {"session_id": session_id, "include_subagents": True},
            sessions,
        )
        failure(hidden_read, "session_not_found")


@pytest.mark.parametrize(
    "period",
    ("weekend", "/", "2026-05-03/2026-05-02", "2026-05-01/2026-05-02/2026-05-03"),
)
async def test_invalid_period_is_rejected(tmp_path: Path, period: str) -> None:
    sessions = ChatSessionManager(tmp_path)
    result = await session_search_handler(
        make_context(tmp_path),
        {"period": period},
        CanonicalSessionRecallBackend(sessions),
    )

    failure(result, "invalid_arguments")


async def test_large_session_descriptor_list_returns_bounded_first_ten_without_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    summaries: list[JsonObject] = []
    sources: dict[SessionAddress, SessionDescriptorSource] = {}
    for index in range(100):
        session_id = f"context-{index:03d}"
        metadata: JsonObject = {
            "title": "T" * 200,
            "run_kinds": ["subagent"],
            "subagent_parent": {
                "agent_id": "parent-agent",
                "session_id": "parent-" + ("s" * 100),
                "project_id": "project-" + ("p" * 100),
            },
            "id": session_id,
            "created_at": timestamp(1).isoformat(),
            "last_active_at": timestamp(1).isoformat(),
        }
        summaries.append(metadata)
        address = SessionAddress(project_id=None, agent_id="coder", session_id=session_id)
        sources[address] = SessionDescriptorSource(
            metadata,
            1,
            ChatMessage.user(
                "opening " + (str(index % 10) * 400),
                timestamp=timestamp(1),
            ),
        )
    monkeypatch.setattr(
        sessions,
        "list_recall_summaries",
        lambda *_args, **_kwargs: summaries[:11],
    )
    monkeypatch.setattr(
        sessions,
        "descriptor_sources",
        lambda addresses: {address: sources[address] for address in addresses},
    )
    backend = CanonicalSessionRecallBackend(sessions)

    result = await session_search_handler(
        make_context(tmp_path),
        {"include_subagents": True},
        backend,
    )
    data = success(result)

    assert 0 < len(data["items"]) <= 10
    assert data["has_more"] is True
    assert "next_cursor" not in data
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES
