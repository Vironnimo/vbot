"""Session search: search behavior."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import (
    CanonicalSessionRecallBackend,
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
    SqliteFtsRecallBackend,
)
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.tools._session_recall_results import (
    SESSION_SEARCH_EXCERPT_MAX_CHARS,
)
from core.tools.session_search import (
    SESSION_READ_TOOL_NAME,
    SESSION_SEARCH_RESULT_MAX_BYTES,
    session_read_handler,
    session_search_handler,
)
from tests.core.tools.session_search_helpers import (
    make_context,
    success,
    timestamp,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


async def test_search_applies_period_and_backend_default_ranking(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="dated")
    outside = ChatMessage.user("needle old", timestamp=timestamp(1))
    first = ChatMessage.user(
        "We will not implement Telegram in this Session.",
        timestamp=timestamp(2),
    )
    second = ChatMessage.user(
        "Actually, implement Telegram completely from start to finish.",
        timestamp=timestamp(3),
    )
    for message in (outside, first, second):
        session.append(message)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {
                "query": "Telegram",
                "period": "2026-05-02/2026-05-03",
            },
            CanonicalSessionRecallBackend(sessions),
        )
    )

    assert [item["message_id"] for item in data["items"]] == [second.id, first.id]
    assert "ranking" not in data


async def test_unscoped_search_keeps_repeated_hits_and_one_session_descriptor(
    tmp_path: Path,
) -> None:
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
            CanonicalSessionRecallBackend(sessions),
        )
    )

    assert [item["message_id"] for item in data["items"]] == [second.id, first.id]
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
    messages = []
    for index in range(12):
        session = sessions.create("coder", session_id=f"hit-{index}")
        message = ChatMessage.user(f"needle {index}", timestamp=timestamp(index + 1))
        session.append(message)
        messages.append(message)
    backend = CanonicalSessionRecallBackend(sessions)

    data = success(
        await session_search_handler(make_context(tmp_path), {"query": "needle"}, backend)
    )

    assert len(data["items"]) == 10
    assert data["items"][0]["message_id"] == messages[-1].id
    assert data["has_more"] is True
    assert "next_cursor" not in data


async def test_unscoped_search_filters_internal_sessions_before_result_shaping(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    root = sessions.create("coder", session_id="root")
    duplicate = ChatMessage.user("needle duplicated", timestamp=timestamp(1))
    root.append(duplicate)
    for session_id in ("reflection-one", "reflection-two"):
        reflection = sessions.create("coder", session_id=session_id)
        reflection.append(duplicate)
        sessions.set_metadata(
            SessionAddress(project_id=None, agent_id="coder", session_id=session_id),
            {
                "fork_source": {
                    "agent_id": "coder",
                    "session_id": "root",
                    "project_id": None,
                },
                "run_kinds": ["skill_reflection"],
            },
        )
    other_messages = []
    for index in range(2):
        session = sessions.create("coder", session_id=f"other-{index}")
        message = ChatMessage.user(f"needle distinct {index}", timestamp=timestamp(index + 2))
        session.append(message)
        other_messages.append(message)

    seen_requests: list[RecallSearchRequest] = []

    class _RankedBackend:
        def search_capabilities(self) -> RecallSearchCapabilities:
            return RecallSearchCapabilities(result_type="message", guidance="Test search.")

        async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
            seen_requests.append(request)

            def hit(session_id: str, message: ChatMessage, score: float) -> RecallSearchHit:
                return RecallSearchHit(
                    result_type="message",
                    session_id=session_id,
                    message_id=message.id,
                    role=str(message.role),
                    timestamp=str(message.timestamp),
                    text=str(message.content),
                    score=score,
                )

            return RecallSearchPage(
                hits=(
                    hit("reflection-one", duplicate, 1.0),
                    hit("reflection-two", duplicate, 0.9),
                    hit("root", duplicate, 0.8),
                    hit("other-0", other_messages[0], 0.7),
                    hit("other-1", other_messages[1], 0.6),
                ),
                result_type="message",
                ranking="test",
                snapshot_id="snapshot",
                has_more=False,
                total_candidate_sessions=5,
            )

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle"},
            _RankedBackend(),
            sessions=sessions,
        )
    )

    assert [request.limit for request in seen_requests] == [10]
    assert set(seen_requests[0].excluded_session_ids) >= {
        "reflection-one",
        "reflection-two",
        "current-session",
    }
    assert [item["session_id"] for item in data["items"]] == [
        "root",
        "other-0",
        "other-1",
    ]
    assert data["has_more"] is False


async def test_session_scoped_search_keeps_multiple_hits_and_does_not_overfetch(
    tmp_path: Path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="target")
    messages = [
        ChatMessage.user(f"needle {index}", timestamp=timestamp(index + 1)) for index in range(2)
    ]
    for message in messages:
        session.append(message)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle", "session_id": "target"},
            CanonicalSessionRecallBackend(sessions),
        )
    )

    assert [item["message_id"] for item in data["items"]] == list(
        reversed([message.id for message in messages])
    )


async def test_project_scope_is_preserved_for_search_and_read(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    global_session = sessions.create("coder", session_id="global")
    project_session = sessions.create("coder", session_id="project", project_id="p1")
    global_session.append(ChatMessage.user("needle global", timestamp=timestamp(1)))
    project_message = ChatMessage.user("needle project", timestamp=timestamp(2))
    project_session.append(project_message)
    backend = CanonicalSessionRecallBackend(sessions)
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

    assert "backend" not in data
    assert "ranking" not in data
    assert [item["session_id"] for item in data["items"]] == ["dense", "sparse"]


async def test_fts_tool_search_does_not_reconstruct_complete_session_histories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="indexed")
    message = ChatMessage.user("indexed needle", timestamp=timestamp(1))
    session.append(message)
    backend = SqliteFtsRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))

    def fail_history_load(_session: ChatSession) -> list[ChatMessage]:
        raise AssertionError("FTS Tool search must not load complete Session history")

    monkeypatch.setattr(ChatSession, "load", fail_history_load)
    monkeypatch.setattr(ChatSession, "load_active", fail_history_load)

    data = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "needle"},
            backend,
        )
    )

    assert [item["message_id"] for item in data["items"]] == [message.id]
    assert data["sessions"][0]["message_count"] == 1
    assert data["sessions"][0]["first_user_excerpt"] == {
        "text": "indexed needle",
        "trailing_truncated": False,
    }
    missing = success(
        await session_search_handler(
            make_context(tmp_path),
            {"query": "absent"},
            backend,
        )
    )
    assert missing["items"] == []


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
            CanonicalSessionRecallBackend(sessions),
        )
    )

    assert [item["message_id"] for item in data["items"]] == [real.id]


async def test_sync_extension_search_runs_outside_event_loop(tmp_path: Path) -> None:
    from core.recall import RecallSearchCapabilities, RecallSearchPage

    caller_thread = threading.get_ident()

    class _SyncBackend:
        sessions = ChatSessionManager(tmp_path)
        search_thread: int | None = None

        def search_capabilities(self) -> RecallSearchCapabilities:
            return RecallSearchCapabilities(result_type="message", guidance="Search messages.")

        def search_page(self, request: Any) -> RecallSearchPage:
            assert request.query == "extension"
            self.search_thread = threading.get_ident()
            return RecallSearchPage((), "message", "extension", "snapshot", False, 0)

    backend = _SyncBackend()
    data = success(
        await session_search_handler(make_context(tmp_path), {"query": "extension"}, backend)
    )

    assert data["result_type"] == "message"
    assert data["items"] == []
    assert backend.search_thread is not None
    assert backend.search_thread != caller_thread


async def test_multiple_large_excerpts_stay_within_result_limit(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    for index in range(3):
        session = sessions.create("coder", session_id=f"large-excerpt-{index}")
        session.append(
            ChatMessage.user(
                f"needle-{index} " + (chr(65 + index) * 30_000),
                timestamp=timestamp(index + 1),
            )
        )

    result = await session_search_handler(
        make_context(tmp_path),
        {"query": "needle"},
        CanonicalSessionRecallBackend(sessions),
    )
    data = success(result)

    assert len(data["items"]) == 3
    assert all(
        len(item["excerpt"]["text"]) <= SESSION_SEARCH_EXCERPT_MAX_CHARS for item in data["items"]
    )
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= SESSION_SEARCH_RESULT_MAX_BYTES
