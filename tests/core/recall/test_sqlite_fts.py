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


def test_sqlite_fts_finds_substring_within_token(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="substring-session")
    session.append(ChatMessage.user("Switched the agent to gpt4o today", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    data = recall.search(request(query="gpt"))

    assert data["matches"][0]["session_id"] == "substring-session"


def test_sqlite_fts_case_insensitive_substring(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="case-session")
    session.append(ChatMessage.user("Running GPT4O benchmark", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    data = recall.search(request(query="gpt4o"))

    assert data["matches"][0]["session_id"] == "case-session"


def test_sqlite_fts_short_query_falls_back_to_jsonl_substring(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="short-session")
    session.append(ChatMessage.user("Go fast", timestamp=timestamp(1)))
    recall = backend(tmp_path, sessions)

    data = recall.search(request(query="go"))

    assert data["matches"][0]["session_id"] == "short-session"


def _project_request(*, query: str, project_id: str | None, limit: int = 20) -> RecallRequest:
    return RecallRequest(
        agent_id="coder",
        session_id=None,
        around_message_id=None,
        query=query,
        since=None,
        until=None,
        roles=("user", "assistant", "tool", "error", "compaction_checkpoint"),
        match_mode="all_terms",
        limit=limit,
        context_messages=0,
        bookend_messages=2,
        sort="newest",
        project_id=project_id,
    )


def test_sqlite_fts_project_recall_finds_only_project_sessions(tmp_path: Path) -> None:
    """A project-scoped recall searches only the project's Sessions."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="global-s").append(
        ChatMessage.user("global release notes", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="proj-s", project_id="alpha").append(
        ChatMessage.user("project release notes", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions)

    project = recall.search(_project_request(query="release", project_id="alpha"))
    identity = recall.search(_project_request(query="release", project_id=None))

    assert [m["session_id"] for m in project["matches"]] == ["proj-s"]
    assert [m["session_id"] for m in identity["matches"]] == ["global-s"]


def test_sqlite_fts_same_uuid_global_and_project_do_not_collide(tmp_path: Path) -> None:
    """The same session UUID under two scopes indexes and matches separately."""

    sessions = ChatSessionManager(tmp_path)
    shared_id = "22222222-2222-2222-2222-222222222222"
    sessions.create("coder", session_id=shared_id).append(
        ChatMessage.user("global carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id=shared_id, project_id="alpha").append(
        ChatMessage.user("project bananas", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions)

    # Each scope sees only its own content for the shared UUID.
    global_data = recall.search(_project_request(query="carrots", project_id=None))
    project_data = recall.search(_project_request(query="bananas", project_id="alpha"))

    assert [m["snippet"] for m in global_data["matches"]] == ["global carrots"]
    assert [m["snippet"] for m in project_data["matches"]] == ["project bananas"]
    # The global scope must not surface the project-only term and vice versa.
    assert recall.search(_project_request(query="bananas", project_id=None))["matches"] == []
    assert recall.search(_project_request(query="carrots", project_id="alpha"))["matches"] == []


def test_sqlite_fts_identity_recall_unchanged_by_project_field(tmp_path: Path) -> None:
    """Explicit ``project_id=None`` matches the implicit-default behavior."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="s1").append(
        ChatMessage.user("Release deploy plan", timestamp=timestamp(1))
    )
    recall = backend(tmp_path, sessions)

    explicit_none = recall.search(_project_request(query="release deploy", project_id=None))
    default = recall.search(request(query="release deploy"))

    assert [m["session_id"] for m in explicit_none["matches"]] == ["s1"]
    assert [m["session_id"] for m in default["matches"]] == ["s1"]
