"""Budget selection for canonical scans."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.recall import CanonicalSessionRecallBackend, RecallSearchRequest
from core.recall.recall import RecallOrder
from core.sessions import ChatSession, ChatSessionManager, _store_values


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["newest", "oldest"])
@pytest.mark.parametrize("scan_limit", [2, 3, 4])
async def test_scan_budget_checks_eligible_messages_globally_in_request_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: RecallOrder, scan_limit: int
) -> None:
    monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", scan_limit)
    sessions = ChatSessionManager(tmp_path)
    first = sessions.create("agent", session_id="first")
    second = sessions.create("agent", session_id="second")
    eligible = []
    for day, session in [(1, first), (3, first), (2, second)]:
        message = ChatMessage.user("needle", timestamp=datetime(2026, 1, day, tzinfo=UTC))
        session.append(message)
        eligible.append(message)
    # Newer ineligible records cannot hide conversation hits or mark a complete
    # eligible selection partial, regardless of the Session catalog order.
    first.append(ChatMessage.note("needle", timestamp=datetime(2026, 1, 4, tzinfo=UTC)))
    second.append(ChatMessage.user("needle", timestamp=datetime(2026, 1, 5, tzinfo=UTC)))
    request = RecallSearchRequest(
        agent_id="agent",
        project_id=None,
        session_id=None,
        query="needle",
        since=None,
        until=datetime(2026, 1, 4, tzinfo=UTC),
        roles=("user",),
        match_mode="all_terms",
        order=order,
        offset=0,
        limit=10,
    )

    page = await CanonicalSessionRecallBackend(sessions).search_page(request)

    ordered = sorted(eligible, key=lambda message: message.timestamp, reverse=order == "newest")
    assert [hit.message_id for hit in page.hits] == [message.id for message in ordered[:scan_limit]]
    assert page.ranking == f"message_time_{order}"
    assert page.degraded is (scan_limit < len(eligible))
    assert page.has_more is False
    sessions.close()


@pytest.mark.asyncio
async def test_scan_pages_are_exact_without_loading_histories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="history")
    matches = [
        ChatMessage.user(f"needle {day}", timestamp=datetime(2026, 1, day, tzinfo=UTC))
        for day in (1, 2, 3)
    ]
    session.append_many(
        [*matches, ChatMessage.user("other", timestamp=datetime(2026, 1, 4, tzinfo=UTC))]
    )

    def reject_load(*_args: object) -> None:
        raise AssertionError("canonical search must not load Session histories")

    monkeypatch.setattr(ChatSession, "load", reject_load)
    monkeypatch.setattr(ChatSession, "load_active", reject_load)
    request = RecallSearchRequest(
        agent_id="agent",
        project_id=None,
        session_id=None,
        query="NEEDLE",
        since=None,
        until=None,
        roles=("user",),
        match_mode="all_terms",
        order="newest",
        offset=0,
        limit=2,
    )
    recall = CanonicalSessionRecallBackend(sessions)

    first = await recall.search_page(request)
    second = await recall.search_page(replace(request, offset=2, snapshot_id=first.snapshot_id))

    assert [hit.message_id for hit in first.hits] == [matches[2].id, matches[1].id]
    assert [hit.message_id for hit in second.hits] == [matches[0].id]
    assert (first.has_more, second.has_more) == (True, False)
    assert (first.hits[0].match_start, first.hits[0].match_end) == (0, 6)
    assert not first.degraded
    sessions.close()
