"""Tests for append-only chat session JSONL storage."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from core.chat import ChatMessage, ChatSession, ChatSessionError, ChatSessionManager, ToolCall

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)


class TestChatSession:
    def test_create_writes_empty_jsonl_file(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        assert session.id == "session-one"
        assert session.path == tmp_path / "session-one.jsonl"
        assert session.path.read_text(encoding="utf-8") == ""

    def test_create_rejects_duplicate_session(self, tmp_path):
        ChatSession.create(tmp_path, session_id="session-one")

        with pytest.raises(ChatSessionError, match="already exists"):
            ChatSession.create(tmp_path, session_id="session-one")

    def test_create_generates_uuid_session_id(self, tmp_path):
        session = ChatSession.create(tmp_path)

        assert session.path.exists()
        assert session.path.suffix == ".jsonl"
        assert UUID(session.id)

    @pytest.mark.parametrize(
        "session_id",
        [
            "",
            "../outside",
            "..\\outside",
            ".hidden",
            "with space",
            "name.jsonl",
            "name/slash",
            "a" * 129,
        ],
    )
    def test_create_rejects_unsafe_session_id(self, tmp_path, session_id):
        with pytest.raises(ChatSessionError, match="session id"):
            ChatSession.create(tmp_path, session_id=session_id)

    def test_init_rejects_non_jsonl_path(self, tmp_path):
        with pytest.raises(ChatSessionError, match=".jsonl"):
            ChatSession(tmp_path / "session.txt")

    def test_append_writes_single_compact_utf8_json_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        message = ChatMessage.user("Grüße aus Berlin", timestamp=FIXED_TIMESTAMP)

        session.append(message)

        content = session.path.read_text(encoding="utf-8")
        assert content.endswith("\n")
        assert len(content.splitlines()) == 1
        assert "Grüße" in content
        assert json.loads(content) == message.to_dict()

    def test_add_note_appends_valid_note_jsonl_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        session.add_note("Background task completed")

        content = session.path.read_text(encoding="utf-8")
        assert content.endswith("\n")
        note_data = json.loads(content)
        assert note_data["role"] == "note"
        assert note_data["content"] == "Background task completed"
        assert ChatMessage.from_dict(note_data).to_dict() == note_data

    def test_load_includes_added_note(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        session.add_note("Background task completed")

        messages = session.load()
        assert len(messages) == 1
        assert messages[0].role == "note"
        assert messages[0].content == "Background task completed"

    def test_drain_pending_notes_returns_added_notes_and_clears_queue(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        session.add_note("First reminder")
        session.add_note("Second reminder")

        pending_notes = session.drain_pending_notes()
        assert [note.content for note in pending_notes] == ["First reminder", "Second reminder"]
        assert [note.role for note in pending_notes] == ["note", "note"]
        assert session.drain_pending_notes() == []

    def test_load_returns_messages_in_append_order(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        user_message = ChatMessage.user("Weather?", timestamp=FIXED_TIMESTAMP)
        assistant_message = ChatMessage.assistant(
            model="anthropic/claude-sonnet-4",
            content=None,
            reasoning="Need a tool.",
            reasoning_meta={"signature": "opaque"},
            tool_calls=[ToolCall(id="call_abc", name="get_weather", arguments={"city": "Berlin"})],
            timestamp=FIXED_TIMESTAMP,
        )
        tool_message = ChatMessage.tool(
            tool_call_id="call_abc",
            name="get_weather",
            content='{"temp":22}',
            timestamp=FIXED_TIMESTAMP,
        )

        session.append(user_message)
        session.append(assistant_message)
        session.append(tool_message)

        assert [message.to_dict() for message in session.load()] == [
            user_message.to_dict(),
            assistant_message.to_dict(),
            tool_message.to_dict(),
        ]

    def test_load_rejects_invalid_json_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.path.write_text("{not-json}\n", encoding="utf-8")

        with pytest.raises(ChatSessionError, match="invalid JSON at line 1"):
            session.load()

    def test_load_rejects_invalid_message_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.path.write_text(
            '{"id":"d4e5f6","timestamp":"2026-05-03T14:30:01+00:00","role":"user"}\n',
            encoding="utf-8",
        )

        with pytest.raises(ChatSessionError, match="invalid message at line 1"):
            session.load()

    def test_load_rejects_missing_file(self, tmp_path):
        session = ChatSession(tmp_path / "missing.jsonl")

        with pytest.raises(ChatSessionError, match="does not exist"):
            session.load()

    def test_delete_removes_file_and_is_idempotent(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        session.delete()
        session.delete()

        assert not session.path.exists()


class TestChatSessionManager:
    def test_create_places_session_under_agent_sessions_directory(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        session = manager.create("coder", session_id="session-one")

        assert session.path == tmp_path / "agents" / "coder" / "sessions" / "session-one.jsonl"

    def test_get_returns_existing_session(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")

        session = manager.get("coder", "session-one")

        assert session.id == "session-one"

    def test_get_rejects_missing_session(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="does not exist"):
            manager.get("coder", "missing")

    @pytest.mark.parametrize("session_id", ["../outside", "..\\outside", "with space"])
    def test_get_rejects_unsafe_session_id_before_path_lookup(self, tmp_path, session_id):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="session id"):
            manager.get("coder", session_id)

        assert not (tmp_path / "agents").exists()

    def test_list_returns_sessions_sorted_by_filename(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-b")
        manager.create("coder", session_id="session-a")

        sessions = manager.list("coder")

        assert [session.id for session in sessions] == ["session-a", "session-b"]

    def test_list_ignores_unsafe_session_filenames(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-one")
        unsafe_path = session.path.parent / "unsafe.name.jsonl"
        unsafe_path.write_text("", encoding="utf-8")

        sessions = manager.list("coder")

        assert [listed.id for listed in sessions] == ["session-one"]

    def test_list_returns_empty_for_agent_without_sessions(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        assert manager.list("coder") == []

    def test_delete_removes_session_file(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-one")

        manager.delete("coder", "session-one")

        assert not session.path.exists()

    def test_delete_rejects_unsafe_session_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="session id"):
            manager.delete("coder", "../outside")

    def test_rejects_empty_agent_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="agent id"):
            manager.create("", session_id="session-one")
