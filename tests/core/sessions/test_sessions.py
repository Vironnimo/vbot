"""Tests for append-only chat session JSONL storage."""

import asyncio
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

    def test_load_recovers_partial_trailing_json_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        message = ChatMessage.user("Survives crash", timestamp=FIXED_TIMESTAMP)
        session.append(message)
        valid_content = session.path.read_bytes()
        session.path.write_bytes(valid_content + b'{"id":"partial"')

        messages = session.load()

        assert [loaded_message.to_dict() for loaded_message in messages] == [message.to_dict()]
        assert session.path.read_bytes() == valid_content

    def test_load_recovers_partial_trailing_utf8_sequence(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        message = ChatMessage.user("Valid", timestamp=FIXED_TIMESTAMP)
        session.append(message)
        valid_content = session.path.read_bytes()
        partial_line = '{"id":"partial","content":"Grü'.encode()[:-1]
        session.path.write_bytes(valid_content + partial_line)

        messages = session.load()

        assert [loaded_message.to_dict() for loaded_message in messages] == [message.to_dict()]
        assert session.path.read_bytes() == valid_content

    def test_bookend_timestamps_returns_first_and_last_message_timestamps(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        first_message = ChatMessage.user(
            "first", timestamp=datetime(2026, 5, 3, 14, 30, tzinfo=UTC)
        )
        middle_message = ChatMessage.user(
            "middle", timestamp=datetime(2026, 5, 3, 15, 0, tzinfo=UTC)
        )
        last_message = ChatMessage.user("last", timestamp=datetime(2026, 5, 3, 15, 45, tzinfo=UTC))
        for message in (first_message, middle_message, last_message):
            session.append(message)

        assert session.bookend_timestamps() == (first_message.timestamp, last_message.timestamp)

    def test_bookend_timestamps_returns_same_timestamp_for_single_message(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        message = ChatMessage.user("only", timestamp=FIXED_TIMESTAMP)
        session.append(message)

        assert session.bookend_timestamps() == (message.timestamp, message.timestamp)

    def test_bookend_timestamps_returns_none_for_empty_file(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        assert session.bookend_timestamps() is None

    def test_bookend_timestamps_returns_none_for_partial_trailing_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.append(ChatMessage.user("valid", timestamp=FIXED_TIMESTAMP))
        session.path.write_bytes(session.path.read_bytes() + b'{"id":"partial"')

        assert session.bookend_timestamps() is None

    def test_bookend_timestamps_reads_last_line_larger_than_tail_chunk(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        first_message = ChatMessage.user(
            "first", timestamp=datetime(2026, 5, 3, 14, 30, tzinfo=UTC)
        )
        large_message = ChatMessage.user(
            "x" * 20000, timestamp=datetime(2026, 5, 3, 15, 45, tzinfo=UTC)
        )
        session.append(first_message)
        session.append(large_message)

        assert session.bookend_timestamps() == (first_message.timestamp, large_message.timestamp)

    def test_skill_context_messages_uses_preloaded_messages_without_file_read(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.activate_skill_context("demo", {"content": "Skill body", "resources": []})
        loaded_messages = session.load()

        fresh_handle = ChatSession(session.path)
        session.path.unlink()

        assert fresh_handle.skill_context_messages(loaded_messages) == [
            {"role": "user", "content": "Skill body"}
        ]

    def test_load_rejects_invalid_json_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.path.write_text("{not-json}\n", encoding="utf-8")

        with pytest.raises(ChatSessionError, match="invalid JSON at line 1"):
            session.load()

    def test_load_rejects_invalid_final_message_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.path.write_text(
            '{"id":"d4e5f6","timestamp":"2026-05-03T14:30:01+00:00","role":"user"}',
            encoding="utf-8",
        )

        with pytest.raises(ChatSessionError, match="invalid message at line 1"):
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
        session.sidecar_path.write_text('{"source_channel_id":"tg"}', encoding="utf-8")

        session.delete()
        session.delete()

        assert not session.path.exists()
        assert not session.sidecar_path.exists()


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

    def test_set_title_stores_title_and_returns_it(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")

        stored = manager.set_title("coder", "session-one", "Release planning")

        assert stored == "Release planning"
        assert manager.get_metadata("coder", "session-one") == {"title": "Release planning"}

    def test_set_title_collapses_whitespace_to_single_line(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")

        stored = manager.set_title("coder", "session-one", "  multi\n  line\ttitle  ")

        assert stored == "multi line title"

    def test_set_title_caps_length(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")

        stored = manager.set_title("coder", "session-one", "x" * 500)

        assert stored == "x" * 200

    def test_set_title_blank_clears_and_returns_none(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        manager.set_title("coder", "session-one", "Release planning")

        cleared = manager.set_title("coder", "session-one", "   ")

        assert cleared is None
        assert "title" not in manager.get_metadata("coder", "session-one")

    def test_set_title_preserves_other_metadata(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        manager.set_metadata("coder", "session-one", {"platform": "telegram"})

        manager.set_title("coder", "session-one", "Release planning")

        assert manager.get_metadata("coder", "session-one") == {
            "platform": "telegram",
            "title": "Release planning",
        }

    def test_set_title_clear_keeps_other_metadata(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        manager.set_metadata("coder", "session-one", {"platform": "telegram", "title": "old"})

        manager.set_title("coder", "session-one", "")

        assert manager.get_metadata("coder", "session-one") == {"platform": "telegram"}

    def test_set_title_rejects_missing_session(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="does not exist"):
            manager.set_title("coder", "missing", "Release planning")

    def test_set_title_surfaces_in_list_with_metadata(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        manager.set_title("coder", "session-one", "Release planning")

        sessions = manager.list_with_metadata("coder")

        assert sessions[0]["title"] == "Release planning"

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

    def test_list_with_metadata_recovers_timestamps_from_partial_trailing_line(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-a")
        message_timestamp = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)
        session.append(ChatMessage.user("hello", timestamp=message_timestamp))
        session.path.write_bytes(session.path.read_bytes() + b'{"id":"partial"')

        sessions = manager.list_with_metadata("coder")

        assert sessions == [
            {
                "id": "session-a",
                "created_at": message_timestamp.isoformat(),
                "last_active_at": message_timestamp.isoformat(),
            }
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
        manager.set_metadata("coder", "session-one", {"is_subagent_session": True})

        manager.delete("coder", "session-one")

        assert not session.path.exists()
        assert not session.sidecar_path.exists()

    def test_delete_recreated_session_does_not_inherit_metadata(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        manager.set_metadata("coder", "session-one", {"is_subagent_session": True})

        manager.delete("coder", "session-one")
        manager.create("coder", session_id="session-one")

        assert manager.get_metadata("coder", "session-one") == {}

    def test_delete_rejects_unsafe_session_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="session id"):
            manager.delete("coder", "../outside")

    def test_rejects_empty_agent_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="agent id"):
            manager.create("", session_id="session-one")

    def test_write_lock_is_shared_across_manager_instances(self, tmp_path):
        manager_a = ChatSessionManager(tmp_path)
        manager_b = ChatSessionManager(tmp_path)

        lock = manager_a.write_lock("coder", "session-one")

        assert manager_b.write_lock("coder", "session-one") is lock
        assert manager_a.write_lock("coder", "session-two") is not lock

    def test_sessions_dir_without_project_keeps_global_layout(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        assert manager.sessions_dir("coder") == tmp_path / "agents" / "coder" / "sessions"

    def test_sessions_dir_with_project_uses_anchor_layout(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        assert (
            manager.sessions_dir("coder", project_id="acme")
            == tmp_path / "projects" / "acme" / "agents" / "coder" / "sessions"
        )

    def test_sessions_dir_matches_project_store_layout(self, tmp_path):
        from core.projects.store import ProjectStore

        manager = ChatSessionManager(tmp_path)
        store = ProjectStore(tmp_path)

        assert manager.sessions_dir("coder", project_id="acme") == store.sessions_dir(
            "acme", "coder"
        )

    def test_create_with_project_places_session_under_anchor(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        session = manager.create("coder", session_id="session-one", project_id="acme")

        assert (
            session.path
            == tmp_path
            / "projects"
            / "acme"
            / "agents"
            / "coder"
            / "sessions"
            / "session-one.jsonl"
        )

    def test_global_and_project_session_with_same_id_are_separate_files(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        global_session = manager.create("coder", session_id="shared")
        project_session = manager.create("coder", session_id="shared", project_id="acme")

        assert global_session.path != project_session.path
        assert manager.exists("coder", "shared") is True
        assert manager.exists("coder", "shared", project_id="acme") is True
        # A project session does not leak into the global scope and vice versa.
        manager.delete("coder", "shared", project_id="acme")
        assert manager.exists("coder", "shared") is True
        assert manager.exists("coder", "shared", project_id="acme") is False

    def test_project_scope_isolates_get_or_create_and_list(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        manager.get_or_create("coder", "shared")
        manager.get_or_create("coder", "shared", project_id="acme")
        manager.get_or_create("coder", "project-only", project_id="acme")

        assert [session.id for session in manager.list("coder")] == ["shared"]
        assert [session.id for session in manager.list("coder", project_id="acme")] == [
            "project-only",
            "shared",
        ]

    def test_project_scope_isolates_metadata(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="shared")
        manager.create("coder", session_id="shared", project_id="acme")

        manager.set_metadata("coder", "shared", {"scope": "global"})
        manager.set_metadata("coder", "shared", {"scope": "project"}, project_id="acme")

        assert manager.get_metadata("coder", "shared") == {"scope": "global"}
        assert manager.get_metadata("coder", "shared", project_id="acme") == {"scope": "project"}
        assert [
            entry["scope"] for entry in manager.list_with_metadata("coder", project_id="acme")
        ] == ["project"]

    def test_write_lock_separates_global_and_project_scope(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        global_lock = manager.write_lock("coder", "shared")
        project_lock = manager.write_lock("coder", "shared", project_id="acme")

        assert global_lock is not project_lock
        # Same project + id resolves back to the same lock.
        assert manager.write_lock("coder", "shared", project_id="acme") is project_lock

    def test_write_lock_is_task_reentrant(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        async def reenter() -> bool:
            lock = manager.write_lock("coder", "session-one")
            async with lock, lock:  # same task re-enters; a plain lock would deadlock here
                return True

        assert asyncio.run(asyncio.wait_for(reenter(), timeout=1.0)) is True

    def test_open_tool_cycle_blocks_out_of_band_note_until_release(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-one")
        assistant_message = ChatMessage.assistant(
            model="anthropic/claude-sonnet-4",
            content=None,
            tool_calls=[ToolCall(id="call_abc", name="get_weather", arguments={"city": "Berlin"})],
            timestamp=FIXED_TIMESTAMP,
        )
        tool_message = ChatMessage.tool(
            tool_call_id="call_abc",
            name="get_weather",
            content='{"temp":22}',
            timestamp=FIXED_TIMESTAMP,
        )
        release_tool = asyncio.Event()
        note_persisted = asyncio.Event()

        async def run_tool_cycle() -> None:
            async with manager.write_lock("coder", "session-one"):
                session.append(assistant_message)
                await release_tool.wait()
                session.append(tool_message)

        async def observe_note() -> None:
            # A Run on another accessor: must wait behind the open tool cycle.
            async with manager.write_lock("coder", "session-one"):
                manager.get("coder", "session-one").add_note("[channel] observed")
                note_persisted.set()

        async def scenario() -> None:
            run_task = asyncio.create_task(run_tool_cycle())
            await asyncio.sleep(0)  # Run acquires the lock and appends the tool-call message.
            observe_task = asyncio.create_task(observe_note())
            await asyncio.sleep(0)  # The note attempts the lock and must block.
            assert not note_persisted.is_set()
            release_tool.set()
            await asyncio.gather(run_task, observe_task)

        asyncio.run(scenario())

        roles = [message.role for message in manager.get("coder", "session-one").load()]
        assert roles == ["assistant", "tool", "note"]


class TestChatSessionManagerMove:
    """Relocating a session's two files between any (agent, project) homes."""

    @staticmethod
    def _populate(manager, agent_id, session_id, *, project_id=None):
        session = manager.create(agent_id, session_id=session_id, project_id=project_id)
        session.append(ChatMessage.user("hello", timestamp=FIXED_TIMESTAMP))
        session.append(
            ChatMessage.assistant(
                model="openai/gpt-4.1", content="hi there", timestamp=FIXED_TIMESTAMP
            )
        )
        return session

    @pytest.mark.parametrize(
        ("source_project_id", "target_project_id"),
        [
            (None, None),  # personal -> personal
            (None, "acme"),  # personal -> project
            ("acme", None),  # project -> personal
            ("acme", "acme"),  # project -> project (same project, different agent)
        ],
    )
    def test_move_relocates_both_files_in_every_direction(
        self, tmp_path, source_project_id, target_project_id
    ):
        manager = ChatSessionManager(tmp_path)
        source = self._populate(manager, "alpha", "sess", project_id=source_project_id)
        manager.set_metadata("alpha", "sess", {"platform": "telegram"}, source_project_id)
        original = [message.to_dict() for message in source.load()]

        destination = asyncio.run(
            manager.move(
                "alpha",
                "sess",
                "beta",
                source_project_id=source_project_id,
                target_project_id=target_project_id,
            )
        )

        # Source home is empty afterwards (both files gone).
        assert manager.exists("alpha", "sess", source_project_id) is False
        assert not source.sidecar_path.exists()
        # Destination owns the session with identical history, ids, and timestamps.
        assert manager.exists("beta", "sess", target_project_id) is True
        assert destination.path == manager.sessions_dir("beta", target_project_id) / "sess.jsonl"
        assert [message.to_dict() for message in destination.load()] == original
        assert manager.get_metadata("beta", "sess", target_project_id) == {"platform": "telegram"}

    def test_move_tolerates_missing_sidecar(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        self._populate(manager, "alpha", "sess")

        destination = asyncio.run(manager.move("alpha", "sess", "beta"))

        assert manager.exists("beta", "sess") is True
        assert not destination.sidecar_path.exists()
        assert manager.get_metadata("beta", "sess") == {}

    def test_move_strips_requested_meta_keys(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        self._populate(manager, "alpha", "sess")
        manager.set_metadata(
            "alpha",
            "sess",
            {"visited_projects": ["acme"], "platform": "telegram"},
        )

        asyncio.run(
            manager.move(
                "alpha",
                "sess",
                "beta",
                target_project_id="acme",
                strip_meta_keys=frozenset({"visited_projects"}),
            )
        )

        assert manager.get_metadata("beta", "sess", "acme") == {"platform": "telegram"}

    def test_move_fails_cleanly_on_destination_collision(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        source = self._populate(manager, "alpha", "sess")
        manager.set_metadata("alpha", "sess", {"platform": "telegram"})
        # An (improbable) id collision already occupies the destination home.
        manager.create("beta", session_id="sess")

        with pytest.raises(ChatSessionError, match="destination session already exists"):
            asyncio.run(manager.move("alpha", "sess", "beta"))

        # No partial move: the source keeps both of its files.
        assert manager.exists("alpha", "sess") is True
        assert source.sidecar_path.exists()
        assert [message.role for message in source.load()] == ["user", "assistant"]
