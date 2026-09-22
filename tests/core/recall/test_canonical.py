"""Budget selection for degraded canonical searches."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.recall import CanonicalSessionRecallBackend, RecallSearchRequest
from core.recall.recall import RecallOrder
from core.sessions import ChatSessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["newest", "oldest"])
@pytest.mark.parametrize("scan_limit", [2, 3, 4])
async def test_scan_budget_selects_newest_eligible_messages_globally(
    tmp_path: Path, order: RecallOrder, scan_limit: int
) -> None:
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

    page = await CanonicalSessionRecallBackend(sessions, search_scan_limit=scan_limit).search_page(
        request
    )

    selected = sorted(eligible, key=lambda message: message.timestamp, reverse=True)[:scan_limit]
    if order == "oldest":
        selected.reverse()
    assert [hit.message_id for hit in page.hits] == [message.id for message in selected]
    assert page.degraded is (scan_limit < len(eligible))
    sessions.close()
