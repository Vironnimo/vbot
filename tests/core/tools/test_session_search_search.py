"""Session search: search behavior."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
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
from core.runs import RunKind
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.tools._session_recall_results import (
    SESSION_SEARCH_EXCERPT_MAX_CHARS,
)
from core.tools.session_search import (
    SESSION_SEARCH_RESULT_MAX_BYTES,
    session_search_handler,
)
from tests.core.sessions.history_fixtures import admit_run, append_tool_fixture
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
    address = SessionAddress(project_id=None, agent_id="coder", session_id="repeated-context")
    sessions.set_title(address, "Repeated context")
    await admit_run(sessions, address, RunKind.USER)

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


async def test_unscoped_search_rechecks_hit_sessions_before_result_shaping(
    tmp_path: Path,
) -> None:
    """Visibility travels in the request; hits a backend still returns are rechecked."""
    sessions = ChatSessionManager(tmp_path)
    root = sessions.create("coder", session_id="root")
    duplicate = ChatMessage.user("needle duplicated", timestamp=timestamp(1))
    root.append(duplicate)
    # Reflection forks inherit the root's entry without copying it.
    reflection_ids = [
        (await sessions.fork(root.address, run_kind=RunKind.SKILL_REFLECTION)).id for _ in range(2)
    ]
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
                    hit(reflection_ids[0], duplicate, 1.0),
                    hit(reflection_ids[1], duplicate, 0.9),
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
    assert seen_requests[0].excluded_session_ids == ("current-session",)
    assert seen_requests[0].include_subagents is False
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
    assert data["sessions"] == [{"agent_id": "coder", "session_id": "indexed"}]
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
    append_tool_fixture(
        session,
        ChatMessage.tool(
            tool_call_id="call-1",
            name=tool_name,
            content="needle artifact",
            timestamp=timestamp(2),
        ),
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


class _RecordingBackend:
    """Returns no hits and records each request."""

    def __init__(self, *, has_more: bool = False) -> None:
        self.requests: list[RecallSearchRequest] = []
        self.has_more = has_more

    def search_capabilities(self) -> RecallSearchCapabilities:
        return RecallSearchCapabilities(result_type="message", guidance="Test search.")

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        self.requests.append(request)
        return RecallSearchPage((), "message", "test", "snapshot", self.has_more, 0)


async def _search_recorded(
    tmp_path: Path,
    arguments: dict[str, Any],
    *,
    timezone: str | None = None,
    has_more: bool = False,
) -> tuple[dict[str, Any], RecallSearchRequest]:
    backend = _RecordingBackend(has_more=has_more)
    data = success(
        await session_search_handler(
            make_context(tmp_path),
            arguments,
            backend,
            sessions=ChatSessionManager(tmp_path),
            timezone_name_loader=(lambda: timezone) if timezone is not None else None,
        )
    )
    assert len(backend.requests) == 1
    return data, backend.requests[0]


@pytest.mark.parametrize(("limit", "expected"), [(3, 3), ("5", 5), (10, 10), (None, 10)], ids=str)
async def test_limit_up_to_ten_sets_the_hit_count(
    tmp_path: Path, limit: object, expected: int
) -> None:
    data, request = await _search_recorded(tmp_path, {"query": "needle", "limit": limit})

    assert request.limit == expected
    assert "limit is at most" not in data["guidance"]


async def test_limit_above_ten_is_capped_with_a_note(tmp_path: Path) -> None:
    data, request = await _search_recorded(tmp_path, {"query": "needle", "max_results": 50})

    assert request.limit == 10
    assert data["guidance"].startswith(
        "limit is at most 10; this search returned up to 10 matches."
    )


async def test_more_matches_under_a_small_limit_point_to_omitting_it(tmp_path: Path) -> None:
    data, _request = await _search_recorded(
        tmp_path, {"query": "needle", "limit": 2}, has_more=True
    )

    assert "More matches exist. Omit limit for up to 10 matches, or refine" in data["guidance"]


@pytest.mark.parametrize(
    ("period", "since", "until", "stated"),
    [
        (
            "2026-07-01T09:00/2026-07-02",
            datetime(2026, 7, 1, 7, 0, tzinfo=UTC),
            datetime(2026, 7, 2, 21, 59, 59, 999999, tzinfo=UTC),
            "2026-07-01T09:00:00+02:00/2026-07-02T23:59:59+02:00 (Europe/Berlin)",
        ),
        (
            "2026-01-15",
            datetime(2026, 1, 14, 23, 0, tzinfo=UTC),
            datetime(2026, 1, 15, 22, 59, 59, 999999, tzinfo=UTC),
            "2026-01-15T00:00:00+01:00/2026-01-15T23:59:59+01:00 (Europe/Berlin)",
        ),
        (
            "2026-02",
            datetime(2026, 1, 31, 23, 0, tzinfo=UTC),
            datetime(2026, 2, 28, 22, 59, 59, 999999, tzinfo=UTC),
            "2026-02-01T00:00:00+01:00/2026-02-28T23:59:59+01:00 (Europe/Berlin)",
        ),
        (
            "2026-07-05/",
            datetime(2026, 7, 4, 22, 0, tzinfo=UTC),
            None,
            "2026-07-05T00:00:00+02:00/ (Europe/Berlin)",
        ),
        (
            "2026-07-01T09:00Z/2026-07-01T10:00+05:00",
            datetime(2026, 7, 1, 5, 0, tzinfo=UTC),
            datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            "2026-07-01T10:00:00+05:00/2026-07-01T09:00:00+00:00",
        ),
    ],
)
async def test_period_without_offset_reads_the_settings_timezone_and_is_stated(
    tmp_path: Path, period: str, since: datetime, until: datetime | None, stated: str
) -> None:
    data, request = await _search_recorded(
        tmp_path, {"query": "needle", "period": period}, timezone="Europe/Berlin"
    )

    assert (request.since, request.until) == (since, until)
    assert data["period"] == stated


async def test_period_without_a_timezone_setting_reads_utc(tmp_path: Path) -> None:
    data, request = await _search_recorded(tmp_path, {"query": "needle", "period": "2026-07-01"})

    assert request.since == datetime(2026, 7, 1, tzinfo=UTC)
    assert data["period"] == "2026-07-01T00:00:00+00:00/2026-07-01T23:59:59+00:00 (UTC)"


async def test_search_without_period_states_none(tmp_path: Path) -> None:
    data, request = await _search_recorded(tmp_path, {"query": "needle"}, timezone="Europe/Berlin")

    assert (request.since, request.until) == (None, None)
    assert "period" not in data


async def test_single_timestamp_period_names_the_open_range_call(tmp_path: Path) -> None:
    result = await session_search_handler(
        make_context(tmp_path),
        {"query": "needle", "period": "2026-07-01T09:00"},
        _RecordingBackend(),
        sessions=ChatSessionManager(tmp_path),
    )

    assert result["ok"] is False
    assert result["error"]["message"] == (
        "period must use start/end, such as 2026-07-01/2026-07-31. Omit period to search all "
        'dates. For everything from 2026-07-01T09:00 on, use "2026-07-01T09:00/".'
    )
