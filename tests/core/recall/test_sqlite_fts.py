"""Tests for the SQLite FTS recall backend."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.chat import ChatMessage, ToolCall
from core.chat.content_blocks import FileBlock, TextBlock
from core.recall import RecallBackendContext, RecallRequest, SqliteFtsRecallBackend
from core.sessions import ChatSessionManager


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def request(
    *,
    query: str,
    match_mode: str = "all_terms",
    roles: tuple[str, ...] = ("user", "assistant", "tool", "error", "compaction_checkpoint"),
    limit: int = 20,
) -> RecallRequest:
    return RecallRequest(
        agent_id="coder",
        session_id=None,
        around_message_id=None,
        query=query,
        since=None,
        until=None,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        limit=limit,
        context_messages=0,
        bookend_messages=2,
        sort="newest",
    )


def backend(tmp_path: Path, sessions: ChatSessionManager) -> SqliteFtsRecallBackend:
    return SqliteFtsRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))


def test_sqlite_fts_builds_index_lazily_and_finds_matches(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="search-session")
    session.append(ChatMessage.user("Release deploy plan", timestamp=timestamp(3)))

    recall = backend(tmp_path, sessions)
    data = recall.search(request(query="release deploy"))

    assert data["matches"][0]["session_id"] == "search-session"
    assert (tmp_path / "recall" / "session_index.sqlite").is_file()


def test_sqlite_fts_reindexes_stale_session_after_append(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="stale-session")
    session.append(ChatMessage.user("Initial release notes", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    first_data = recall.search(request(query="release"))
    session.append(ChatMessage.user("SQLite recall needle", timestamp=timestamp(2)))
    second_data = recall.search(request(query="sqlite recall"))

    assert len(first_data["matches"]) == 1
    assert second_data["matches"][0]["snippet"] == "SQLite recall needle"


def test_sqlite_fts_rebuilds_when_index_file_is_deleted(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="rebuild-session")
    session.append(ChatMessage.user("Disposable recall index", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)
    recall.search(request(query="disposable"))
    index_path = tmp_path / "recall" / "session_index.sqlite"
    index_path.unlink()

    data = recall.search(request(query="disposable"))

    assert data["matches"][0]["session_id"] == "rebuild-session"
    assert index_path.is_file()


def test_sqlite_fts_recovers_from_corrupt_index(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="corrupt-session")
    session.append(ChatMessage.user("Corrupt index still searchable", timestamp=timestamp(1)))
    index_path = tmp_path / "recall" / "session_index.sqlite"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("not sqlite", encoding="utf-8")

    data = backend(tmp_path, sessions).search(request(query="corrupt searchable"))

    assert data["matches"][0]["session_id"] == "corrupt-session"


def test_sqlite_fts_phrase_and_any_term_modes(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="match-session")
    session.append(ChatMessage.user("alpha beta", timestamp=timestamp(1)))
    session.append(ChatMessage.user("gamma", timestamp=timestamp(2)))
    recall = backend(tmp_path, sessions)

    phrase_data = recall.search(request(query="alpha beta", match_mode="phrase"))
    any_data = recall.search(request(query="missing gamma", match_mode="any_term"))

    assert [match["snippet"] for match in phrase_data["matches"]] == ["alpha beta"]
    assert [match["snippet"] for match in any_data["matches"]] == ["gamma"]


def test_sqlite_search_text_matches_jsonl_scanner_sources(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="sources-session")
    content_blocks: list[Any] = [
        TextBlock(type="text", text="visible block text"),
        FileBlock(
            type="file",
            attachment_id="attachment-1",
            filename="contract.pdf",
            media_type="application/pdf",
        ),
    ]
    session.append(ChatMessage.user(content_blocks, timestamp=timestamp(1)))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="tool request",
            reasoning="private recall clue",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="grep",
                    arguments={"pattern": "indexed-argument"},
                )
            ],
            timestamp=timestamp(2),
        )
    )
    recall = backend(tmp_path, sessions)

    block_data = recall.search(request(query="contract pdf"))
    reasoning_data = recall.search(request(query="private clue"))
    tool_call_data = recall.search(request(query="indexed argument"))

    assert block_data["matches"][0]["session_id"] == "sources-session"
    assert reasoning_data["matches"][0]["role"] == "assistant"
    assert tool_call_data["matches"][0]["role"] == "assistant"
