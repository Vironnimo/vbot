"""Tests for append-only chat session JSONL storage."""

import json
import os
from datetime import UTC, datetime
from uuid import UUID

import pytest

from core.chat import ChatMessage, ToolCall
from core.sessions import ChatSession, ChatSessionError, ChatSessionManager

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)


class TestChatSession:
    def test_create_writes_empty_jsonl_file(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        assert session.id == "session-one"
        assert session.path == tmp_path / "session-one.jsonl"
        assert session.path.read_text(encoding="utf-8") == ""

    def test_sidecar_path_points_to_session_meta_json_file(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        assert session.sidecar_path == tmp_path / "session-one.meta.json"

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

    def test_get_or_create_creates_new_session_when_missing(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        session = manager.get_or_create("coder", "session-one")

        assert session.id == "session-one"
        assert session.path.exists()

    def test_get_or_create_returns_existing_session(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        created = manager.create("coder", session_id="session-one")

        session = manager.get_or_create("coder", "session-one")

        assert session.path == created.path

    def test_exists_returns_true_for_existing_session(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")

        assert manager.exists("coder", "session-one") is True

    def test_exists_returns_false_for_missing_session(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        assert manager.exists("coder", "missing") is False

    def test_get_or_create_rejects_invalid_session_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="session id"):
            manager.get_or_create("coder", "../outside")

    def test_get_metadata_returns_empty_object_when_sidecar_missing(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")

        metadata = manager.get_metadata("coder", "session-one")

        assert metadata == {}

    def test_get_metadata_returns_sidecar_payload(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        payload = {
            "source_channel_id": "tg-assistant",
            "platform": "telegram",
            "platform_conv_id": "12345678",
        }
        manager.set_metadata("coder", "session-one", payload)

        metadata = manager.get_metadata("coder", "session-one")

        assert metadata == payload

    def test_set_metadata_creates_sidecar_file(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-one")
        payload = {
            "source_channel_id": "tg-assistant",
            "platform": "telegram",
        }

        manager.set_metadata("coder", "session-one", payload)

        assert session.sidecar_path.exists()
        assert json.loads(session.sidecar_path.read_text(encoding="utf-8")) == payload

    def test_set_metadata_overwrites_existing_sidecar_payload(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-one")

        manager.set_metadata("coder", "session-one", {"platform": "telegram"})
        manager.set_metadata(
            "coder",
            "session-one",
            {
                "platform": "telegram",
                "platform_conv_id": "12345678",
                "last_reply_target": {
                    "channel_id": "tg-assistant",
                    "platform_target": "12345678",
                },
            },
        )

        assert json.loads(session.sidecar_path.read_text(encoding="utf-8")) == {
            "platform": "telegram",
            "platform_conv_id": "12345678",
            "last_reply_target": {
                "channel_id": "tg-assistant",
                "platform_target": "12345678",
            },
        }

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

    def test_list_with_metadata_returns_timestamps_and_sidecar_fields(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session_a = manager.create("coder", session_id="session-a")
        session_b = manager.create("coder", session_id="session-b")
        first_timestamp = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)
        last_timestamp = datetime(2026, 5, 3, 15, 45, tzinfo=UTC)
        fallback_timestamp = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)

        session_a.append(ChatMessage.user("hello", timestamp=first_timestamp))
        session_a.append(
            ChatMessage.assistant(model="openai/gpt-5", content="hi", timestamp=last_timestamp)
        )
        manager.set_metadata(
            "coder",
            "session-a",
            {
                "source_channel_id": "tg-assistant",
                "platform": "telegram",
                "platform_conv_id": "12345678",
            },
        )
        fallback_epoch = fallback_timestamp.timestamp()
        os.utime(session_b.path, (fallback_epoch, fallback_epoch))

        sessions = manager.list_with_metadata("coder")

        assert sessions == [
            {
                "id": "session-a",
                "created_at": first_timestamp.isoformat(),
                "last_active_at": last_timestamp.isoformat(),
                "source_channel_id": "tg-assistant",
                "platform": "telegram",
                "platform_conv_id": "12345678",
            },
            {
                "id": "session-b",
                "created_at": fallback_timestamp.isoformat(),
                "last_active_at": fallback_timestamp.isoformat(),
            },
        ]

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
