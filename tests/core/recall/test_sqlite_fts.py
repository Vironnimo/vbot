"""The ``sqlite_fts`` backend: Message search over the Session store's own FTS."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.chat import ChatMessage, ToolCall
from core.chat.content_blocks import FileBlock, TextBlock
from core.recall import RecallBackendContext, RecallOrder, SqliteFtsRecallBackend
from core.recall.canonical import CANONICAL_FALLBACK_PARTIAL_REASON
from core.sessions import ChatSession, ChatSessionManager, FtsHealth, _store_fts, _store_values
from tests.core.recall.recall_test_support import ALL_ROLES, request, timestamp
from tests.core.sessions.history_fixtures import append_tool_fixture

pytestmark = pytest.mark.asyncio


def backend(tmp_path: Path, sessions: ChatSessionManager) -> SqliteFtsRecallBackend:
    return SqliteFtsRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))


def _without_integrated_fts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _store_fts,
        "_fts_health_from_connection",
        lambda *_args, **_kwargs: FtsHealth(state="unavailable", reason="test fallback"),
    )


# ---------------------------------------------------------------------------
# Message search over the Sessions-owned FTS
# ---------------------------------------------------------------------------


def _searchable_history(session: ChatSession) -> dict[str, ChatMessage]:
    messages = {
        "phrase": ChatMessage.user("alpha beta", timestamp=timestamp(1)),
        "term": ChatMessage.user("gamma", timestamp=timestamp(2)),
        "lower": ChatMessage.user("Switched the agent to gpt4o today", timestamp=timestamp(3)),
        "upper": ChatMessage.user("Running GPT4O benchmark", timestamp=timestamp(4)),
        "short": ChatMessage.user("Go fast", timestamp=timestamp(5)),
        "blocks": ChatMessage.user(
            [
                TextBlock(type="text", text="visible block text"),
                FileBlock(
                    type="file",
                    attachment_id="attachment-1",
                    filename="contract.pdf",
                    media_type="application/pdf",
                ),
            ],
            timestamp=timestamp(6),
        ),
        "assistant": ChatMessage.assistant(
            model="openai/gpt-5",
            content="tool request",
            reasoning="private recall clue",
            tool_calls=[
                ToolCall(id="call-1", name="grep", arguments={"pattern": "indexed-argument"})
            ],
            timestamp=timestamp(7),
        ),
    }
    session.append_many(list(messages.values()))
    return messages


@pytest.mark.parametrize(
    ("query", "match_mode", "expected"),
    [
        ("alpha beta", "phrase", ["phrase"]),
        ("missing gamma", "any_term", ["term"]),
        # Substrings inside tokens match case-insensitively.
        ("pt4", "all_terms", ["upper", "lower"]),
        # Queries below the trigram length use whole tokens.
        ("go", "all_terms", ["short"]),
        # Attachment names are searchable; reasoning and Tool arguments are not.
        ("contract pdf", "all_terms", ["blocks"]),
        ("private clue", "all_terms", []),
        ("indexed argument", "all_terms", []),
    ],
    ids=["phrase", "any-term", "substring", "short-token", "file-block", "reasoning", "tool-args"],
)
async def test_message_search_matches_conversation_text(
    tmp_path: Path,
    sessions: ChatSessionManager,
    query: str,
    match_mode: str,
    expected: list[str],
) -> None:
    messages = _searchable_history(sessions.create("coder", session_id="history"))

    page = await backend(tmp_path, sessions).search_page(
        request(query, match_mode=match_mode, roles=ALL_ROLES, order="newest")
    )

    assert [hit.message_id for hit in page.hits] == [messages[name].id for name in expected]


async def test_message_search_sees_appended_messages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="stale-session")
    session.append(ChatMessage.user("Initial release notes", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    first = await recall.search_page(request("release"))
    session.append(ChatMessage.user("SQLite recall needle", timestamp=timestamp(2)))
    second = await recall.search_page(request("sqlite recall"))

    assert len(first.hits) == 1
    assert [hit.text for hit in second.hits] == ["SQLite recall needle"]


def _short_term_history(sessions: ChatSessionManager) -> list[ChatMessage]:
    """Store Messages whose better-ranked ``c`` tokens do not contain ``C#``."""
    session = sessions.create("coder", session_id="languages")
    session.append_many([ChatMessage.user("c c c", timestamp=timestamp(1)) for _ in range(30)])
    matches = [
        ChatMessage.user(f"We compared C# generics with Java, part {index}", timestamp=timestamp(2))
        for index in range(12)
    ]
    session.append_many(matches)
    return matches


async def test_short_term_pages_stay_full_when_better_ranked_tokens_do_not_match(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    matches = _short_term_history(sessions)
    recall = backend(tmp_path, sessions)
    first = await recall.search_page(request("C#"))
    second = await recall.search_page(request("C#", offset=10, snapshot_id=first.snapshot_id))

    assert (len(first.hits), first.has_more) == (10, True)
    assert (len(second.hits), second.has_more) == (2, False)
    assert {hit.message_id for hit in first.hits + second.hits} == {
        message.id for message in matches
    }
    assert first.ranking == "bm25"
    assert not first.degraded and not second.degraded


async def test_candidate_budget_marks_rejected_candidates_partial(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", 5)
    _short_term_history(sessions)

    page = await backend(tmp_path, sessions).search_page(request("C#"))

    assert page.hits == ()
    assert page.has_more is False
    assert page.ranking == "bm25"
    assert page.degraded is True
    assert page.degradation_reason == CANONICAL_FALLBACK_PARTIAL_REASON


async def test_time_ordered_pages_return_the_newest_or_oldest_matches(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="history")
    # Short older Messages outrank the longer newer ones.
    older = [ChatMessage.user("needle", timestamp=timestamp(day)) for day in range(1, 11)]
    newer = [
        ChatMessage.user("one needle in a much longer deployment note", timestamp=timestamp(day))
        for day in range(11, 16)
    ]
    session.append_many(older + newer)
    recall = backend(tmp_path, sessions)

    newest_page = await recall.search_page(request("needle", limit=3, order="newest"))
    oldest_page = await recall.search_page(request("needle", limit=3, order="oldest"))

    assert [hit.message_id for hit in newest_page.hits] == [
        message.id for message in reversed(newer[-3:])
    ]
    assert [hit.message_id for hit in oldest_page.hits] == [message.id for message in older[:3]]
    assert newest_page.ranking == "message_time_newest"
    assert newest_page.has_more and oldest_page.has_more


# ---------------------------------------------------------------------------
# Substring-scan fallback
# ---------------------------------------------------------------------------


async def test_tool_inclusive_substring_search_uses_complete_bounded_fallback(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    target = ChatMessage.tool(
        tool_call_id="call-target",
        name="read",
        content='{"ok":true,"data":"prefixneedle"}',
        timestamp=timestamp(1),
    )
    append_tool_fixture(sessions.create("coder", session_id="tool-substring"), target)

    page = await backend(tmp_path, sessions).search_page(request("needle", roles=("tool",)))

    assert [hit.message_id for hit in page.hits] == [target.id]
    assert page.ranking == "substring_scan_newest"
    assert page.degraded is False
    assert page.degradation_reason is None


async def test_large_canonical_fallback_reports_partial_instead_of_false_empty(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", 3)
    session = sessions.create("coder", session_id="bounded-fallback")
    target = ChatMessage.user("prefixhiddenneedle", timestamp=timestamp(1))
    session.append_many(
        [target]
        + [ChatMessage.user(f"filler {day}", timestamp=timestamp(day)) for day in range(2, 8)]
    )
    _without_integrated_fts(monkeypatch)

    page = await backend(tmp_path, sessions).search_page(request("hiddenneedle"))

    assert page.hits == ()
    assert page.ranking == "substring_scan_newest"
    assert page.degraded is True
    assert page.degradation_reason


@pytest.mark.parametrize(
    ("order", "complete"),
    [("relevance", True), ("relevance", False), ("oldest", True), ("newest", False)],
)
async def test_fallback_reason_names_the_scan_order_actually_used(
    tmp_path: Path,
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    order: RecallOrder,
    complete: bool,
) -> None:
    if not complete:
        monkeypatch.setattr(_store_values, "_SEARCH_CANDIDATE_LIMIT", 3)
    sessions.create("coder", session_id="fallback-order").append_many(
        [ChatMessage.user(f"needle {day}", timestamp=timestamp(day)) for day in range(1, 8)]
    )
    _without_integrated_fts(monkeypatch)

    page = await backend(tmp_path, sessions).search_page(request("needle", order=order))

    scan_order, other_order = ("oldest", "newest") if order == "oldest" else ("newest", "oldest")
    assert page.ranking == f"substring_scan_{scan_order}"
    assert page.degraded is True
    reason = page.degradation_reason or ""
    assert f"{scan_order}-first" in reason
    assert f"{other_order}-first" not in reason
    assert ("relevance" in reason.lower()) == (order == "relevance" and complete)
    assert ("incomplete" in reason.lower()) == (not complete)


async def test_fts_fallback_preserves_original_selection_snapshot(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions.create("coder", session_id="one").append(ChatMessage.user("needle"))
    recall = backend(tmp_path, sessions)
    _without_integrated_fts(monkeypatch)

    first = await recall.search_page(request("needle", limit=1))
    continued = await recall.search_page(
        request("needle", limit=1, offset=1, snapshot_id=first.snapshot_id)
    )

    assert first.degraded and continued.degraded
    assert continued.snapshot_id == first.snapshot_id
    assert continued.hits == ()


async def test_message_search_keeps_no_derived_index(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="one").append(
        ChatMessage.user("Integrated message search", timestamp=timestamp(1))
    )

    page = await backend(tmp_path, sessions).search_page(request("integrated"))

    assert [hit.session_id for hit in page.hits] == ["one"]
    assert not (tmp_path / "recall").exists()
