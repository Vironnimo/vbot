"""The internal ``canonical_scan`` backend: budgeted, exact pages without history loads."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.chat import ChatMessage
from core.recall import CanonicalSessionRecallBackend, RecallSearchRequest
from core.recall.recall import RecallMatchMode, RecallOrder
from core.sessions import ChatSession, ChatSessionManager, _store_values


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["newest", "oldest"])
# Below the eligible count the page is partial; at it the page is complete.
@pytest.mark.parametrize("scan_limit", [2, 3])
async def test_scan_budget_checks_eligible_messages_globally_in_request_order(
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    order: RecallOrder,
    scan_limit: int,
) -> None:
    monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", scan_limit)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prefix", "term", "query", "match_mode"),
    [
        ("", "needle", "NEEDLE", "all_terms"),
        ("Straße: ", "needle", "NEEDLE", "all_terms"),
        ("", "Straße", "STRASSE", "all_terms"),
        ("Straße: ", "alpha\n\t beta", "alpha beta", "phrase"),
    ],
    ids=["ascii", "expanded-prefix", "expanded-match", "normalized-phrase"],
)
async def test_scan_pages_are_exact_without_loading_histories(
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    term: str,
    query: str,
    match_mode: RecallMatchMode,
) -> None:
    session = sessions.create("agent", session_id="history")
    matches = [
        ChatMessage.user(f"{prefix}{term} {day}", timestamp=datetime(2026, 1, day, tzinfo=UTC))
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
        query=query,
        since=None,
        until=None,
        roles=("user",),
        match_mode=match_mode,
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
    hit = first.hits[0]
    assert (hit.match_start, hit.match_end) == (len(prefix), len(prefix) + len(term))
    assert hit.text[hit.match_start : hit.match_end] == term
    assert not first.degraded
