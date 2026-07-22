"""Tests for append-only chat session JSONL storage."""

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import UUID

import pytest

from core.chat import ChatMessage, ToolCall
from core.sessions import (
    FORK_SOURCE_META_KEY,
    ChatSession,
    ChatSessionError,
    ChatSessionManager,
    is_skill_context_note,
    skill_context_note_name,
)
from core.sessions.sessions import (
    SKILL_CONTEXT_NOTE_PREFIX,
    SKILL_TOOL_LOADED_STATUS,
    SKILL_TOOL_MESSAGE_NAME,
    _skill_context_note_content,
)
from core.tools import tool_failure, tool_success
from core.tools.skill import SKILL_STATUS_LOADED, SKILL_TOOL_NAME

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)


def test_identity_agent_reference_retarget_updates_only_live_unqualified_parent_links(
    tmp_path,
) -> None:
    manager = ChatSessionManager(tmp_path)
    manager.create("child", session_id="identity-child")
    manager.set_metadata(
        "child",
        "identity-child",
        {
            "subagent_parent": {
                "agent_id": "coder",
                "session_id": "parent-session",
                "run_id": "parent-run",
                "project_id": None,
            },
            FORK_SOURCE_META_KEY: {
                "agent_id": "coder",
                "session_id": "historical-source",
            },
        },
    )
    manager.create("project-child", session_id="qualified-parent", project_id="vbot")
    manager.set_metadata(
        "project-child",
        "qualified-parent",
        {
            "subagent_parent": {
                "agent_id": "coder",
                "session_id": "project-parent",
                "run_id": "project-run",
                "project_id": "vbot",
            }
        },
        project_id="vbot",
    )

    updates = manager.retarget_identity_agent_references("coder", "researcher")

    assert len(updates) == 1
    identity_metadata = manager.get_metadata("child", "identity-child")
    assert identity_metadata["subagent_parent"]["agent_id"] == "researcher"
    assert identity_metadata[FORK_SOURCE_META_KEY]["agent_id"] == "coder"
    assert (
        manager.get_metadata("project-child", "qualified-parent", project_id="vbot")[
            "subagent_parent"
        ]["agent_id"]
        == "coder"
    )

    manager.restore_identity_agent_references(updates)

    assert manager.get_metadata("child", "identity-child") == updates[0].previous_metadata


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

    def test_activated_skill_contents_uses_preloaded_messages_without_file_read(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.activate_skill_context("demo", {"content": "Skill body", "resources": []})
        loaded_messages = session.load()

        fresh_handle = ChatSession(session.path)
        session.path.unlink()

        assert fresh_handle.activated_skill_contents(loaded_messages) == {"demo": "Skill body"}

    def test_activated_skill_contents_preserve_activation_order(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.activate_skill_context("zeta", {"content": "Zeta body", "resources": []})
        session.activate_skill_context("alpha", {"content": "Alpha body", "resources": []})

        expected = {"zeta": "Zeta body", "alpha": "Alpha body"}
        assert session.activated_skill_contents() == expected
        assert list(session.activated_skill_contents()) == ["zeta", "alpha"]

        fresh_handle = ChatSession(session.path)
        assert list(fresh_handle.activated_skill_contents()) == ["zeta", "alpha"]

    def test_activate_skill_context_dedups_and_reports(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        assert session.activate_skill_context("demo", {"content": "Body", "resources": []}) is True
        assert session.activate_skill_context("demo", {"content": "Body", "resources": []}) is False
        assert len([m for m in session.load() if is_skill_context_note(m)]) == 1

    def test_register_skill_activation_dedups_without_persisting(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        assert session.register_skill_activation("demo", "<skill_content>…</skill_content>") is True
        assert session.register_skill_activation("demo", "other") is False
        assert session.load() == []
        assert session.activated_skill_contents() == {"demo": "<skill_content>…</skill_content>"}

    def test_activated_skill_contents_scans_skill_tool_results(self, tmp_path):
        # The ``skill`` tool result is the durable carrier of a tool-loaded skill:
        # a fresh handle recovers name+content from the persisted envelope, while
        # already-active stubs and failures contribute nothing.
        session = ChatSession.create(tmp_path, session_id="session-one")
        loaded_envelope = tool_success(
            {
                "name": "docx",
                "status": "loaded",
                "content": '<skill_content name="docx">B</skill_content>',
            }
        )
        stub_envelope = tool_success(
            {"name": "docx", "status": "already_active", "message": "already active"}
        )
        failure_envelope = tool_failure("skill_not_found", "Skill not found: nope")
        for index, envelope in enumerate([loaded_envelope, stub_envelope, failure_envelope]):
            session.append(
                ChatMessage.tool(
                    tool_call_id=f"call-{index}",
                    name="skill",
                    content=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
                )
            )

        fresh_handle = ChatSession(session.path)
        assert fresh_handle.activated_skill_contents() == {
            "docx": '<skill_content name="docx">B</skill_content>'
        }

    def test_skill_tool_carrier_literals_match_tool_constants(self):
        # The sessions-side carrier scan matches the tool name and loaded status
        # as literals (importing the tool module would cycle); this pins them to
        # the tool's own constants so the two can never silently drift.
        assert SKILL_TOOL_MESSAGE_NAME == SKILL_TOOL_NAME
        assert SKILL_TOOL_LOADED_STATUS == SKILL_STATUS_LOADED

    def test_load_rejects_invalid_json_line(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.path.write_text("{not-json}\n", encoding="utf-8")

        with pytest.raises(ChatSessionError, match="invalid JSON at line 1"):
            session.load()

    def test_load_recovers_unterminated_final_line_with_invalid_message_shape(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        message = ChatMessage.user("Survives crash", timestamp=FIXED_TIMESTAMP)
        session.append(message)
        valid_content = session.path.read_bytes()
        session.path.write_bytes(
            valid_content + b'{"id":"d4e5f6","timestamp":"2026-05-03T14:30:01+00:00","role":"user"}'
        )

        messages = session.load()

        assert [loaded_message.to_dict() for loaded_message in messages] == [message.to_dict()]
        assert session.path.read_bytes() == valid_content

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


class TestSkillContextNoteName:
    def test_returns_name_from_valid_skill_context_note(self):
        note = ChatMessage.note(_skill_context_note_content("deploy", "Ship it."))

        assert skill_context_note_name(note) == "deploy"

    def test_returns_none_for_non_skill_context_note(self):
        assert skill_context_note_name(ChatMessage.note("[channel-message] hi")) is None
        assert skill_context_note_name(ChatMessage.user("hello")) is None

    def test_returns_none_for_malformed_json_payload(self):
        broken = ChatMessage.note(SKILL_CONTEXT_NOTE_PREFIX + "{not-json")

        assert skill_context_note_name(broken) is None

    def test_returns_none_when_payload_lacks_name(self):
        nameless = ChatMessage.note(SKILL_CONTEXT_NOTE_PREFIX + json.dumps({"content": "body"}))

        assert skill_context_note_name(nameless) is None

    def test_returns_none_when_name_is_empty_or_non_string(self):
        empty = ChatMessage.note(SKILL_CONTEXT_NOTE_PREFIX + json.dumps({"name": ""}))
        non_string = ChatMessage.note(SKILL_CONTEXT_NOTE_PREFIX + json.dumps({"name": 7}))

        assert skill_context_note_name(empty) is None
        assert skill_context_note_name(non_string) is None


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

    def test_auto_title_stays_beneath_manual_override_and_reappears_when_cleared(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        manager.set_auto_title("coder", "session-one", "Local request")
        manager.set_title("coder", "session-one", "Manual name")

        manager.set_auto_title("coder", "session-one", "Generated title")

        metadata = manager.get_metadata("coder", "session-one")
        assert metadata["title"] == "Manual name"
        assert metadata["auto_title"] == "Generated title"
        manager.set_title("coder", "session-one", "")
        assert manager.get_metadata("coder", "session-one")["auto_title"] == "Generated title"

    def test_title_change_callbacks_cover_manual_and_automatic_titles(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")
        calls = []
        unsubscribe = manager.add_title_changed_callback(
            lambda agent_id, session_id, project_id: calls.append(
                (agent_id, session_id, project_id)
            )
        )

        manager.set_auto_title("coder", "session-one", "Local request")
        manager.set_title("coder", "session-one", "Manual name")
        unsubscribe()
        manager.set_title("coder", "session-one", "Later name")

        assert calls == [
            ("coder", "session-one", None),
            ("coder", "session-one", None),
        ]

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
                "latest_completion_run_id": None,
                "has_unread_completion": False,
                "unread_run_id": None,
                "unread_run_status": None,
                "unread_run_at": None,
                "source_channel_id": "tg-assistant",
                "platform": "telegram",
                "platform_conv_id": "12345678",
            },
            {
                "id": "session-b",
                "created_at": fallback_timestamp.isoformat(),
                "last_active_at": fallback_timestamp.isoformat(),
                "latest_completion_run_id": None,
                "has_unread_completion": False,
                "unread_run_id": None,
                "unread_run_status": None,
                "unread_run_at": None,
            },
        ]

    def test_terminal_run_stays_unread_until_exact_run_is_acknowledged(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-a")
        first_timestamp = "2026-07-20T10:00:00+00:00"
        second_timestamp = "2026-07-20T10:05:00+00:00"

        manager.record_terminal_run("coder", "session-a", "run-one", "completed", first_timestamp)

        unread = manager.list_with_metadata("coder")[0]
        assert unread["has_unread_completion"] is True
        assert unread["latest_completion_run_id"] == "run-one"
        assert unread["unread_run_id"] == "run-one"
        assert unread["unread_run_status"] == "completed"
        assert unread["unread_run_at"] == first_timestamp
        assert session.activity_path.exists()

        manager.record_terminal_run("coder", "session-a", "run-two", "failed", second_timestamp)
        stale = manager.mark_terminal_run_read("coder", "session-a", "run-one")

        assert stale["marked_read"] is False
        assert stale["has_unread_completion"] is True
        assert stale["unread_run_id"] == "run-two"

        acknowledged = manager.mark_terminal_run_read("coder", "session-a", "run-two")

        assert acknowledged == {
            "latest_completion_run_id": "run-two",
            "has_unread_completion": False,
            "unread_run_id": None,
            "unread_run_status": None,
            "unread_run_at": None,
            "marked_read": True,
        }
        assert manager.list_with_metadata("coder")[0]["has_unread_completion"] is False

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
                "latest_completion_run_id": None,
                "has_unread_completion": False,
                "unread_run_id": None,
                "unread_run_status": None,
                "unread_run_at": None,
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
        manager.record_terminal_run(
            "coder",
            "session-one",
            "run-one",
            "completed",
            "2026-07-20T10:00:00+00:00",
        )

        manager.delete("coder", "session-one")

        assert not session.path.exists()
        assert not session.sidecar_path.exists()
        assert not session.activity_path.exists()

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

    def test_archive_moves_files_out_of_live_dir(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("coder", session_id="session-one")
        manager.set_metadata("coder", "session-one", {"title": "Keep me"})
        manager.record_terminal_run(
            "coder",
            "session-one",
            "run-one",
            "completed",
            "2026-07-20T10:00:00+00:00",
        )

        archive_dir = asyncio.run(manager.archive("coder", "session-one"))

        # Gone from the live location, so list() no longer sees it.
        assert not session.path.exists()
        assert not session.sidecar_path.exists()
        assert not session.activity_path.exists()
        assert manager.list("coder") == []
        # The transcript and both sidecars stay recoverable by hand.
        assert (archive_dir / session.path.name).exists()
        assert (archive_dir / session.sidecar_path.name).exists()
        assert (archive_dir / session.activity_path.name).exists()

    def test_archive_lands_under_sessions_archive_root(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="session-one")

        archive_dir = asyncio.run(manager.archive("coder", "session-one"))

        assert archive_dir == tmp_path / "archive" / "sessions" / "agents" / "coder"

    def test_archive_project_session_uses_project_archive_layout(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="shared", project_id="acme")

        archive_dir = asyncio.run(manager.archive("coder", "shared", project_id="acme"))

        assert (
            archive_dir
            == tmp_path / "archive" / "sessions" / "projects" / "acme" / "agents" / "coder"
        )
        assert (archive_dir / "shared.jsonl").exists()

    def test_archive_missing_session_raises(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="session does not exist"):
            asyncio.run(manager.archive("coder", "ghost"))

    def test_archive_replaces_prior_archive_for_same_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        manager.create("coder", session_id="dup")
        first_dir = asyncio.run(manager.archive("coder", "dup"))
        # Re-create and re-archive the same id; the prior archive is replaced.
        manager.create("coder", session_id="dup")
        second_dir = asyncio.run(manager.archive("coder", "dup"))

        assert first_dir == second_dir
        assert (second_dir / "dup.jsonl").exists()

    def test_rejects_empty_agent_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="agent id"):
            manager.create("", session_id="session-one")

    def test_sessions_dir_rejects_path_traversal_agent_id(self, tmp_path):
        # The agent id becomes a path segment; a traversal component must be refused
        # in both the identity and project layouts.
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="agent id"):
            manager.sessions_dir("../escape")
        with pytest.raises(ChatSessionError, match="agent id"):
            manager.sessions_dir("../escape", project_id="proj")

    def test_delete_rejects_path_traversal_agent_id_leaves_sibling_untouched(self, tmp_path):
        # A traversal agent id is refused before any file is touched, so a sibling the
        # resolved path would target survives.
        manager = ChatSessionManager(tmp_path)
        sibling = tmp_path / "secret"
        sibling.mkdir()
        sibling.joinpath("keep.jsonl").write_text("important", encoding="utf-8")

        with pytest.raises(ChatSessionError, match="agent id"):
            manager.delete("../secret", "session-one")

        assert sibling.joinpath("keep.jsonl").read_text(encoding="utf-8") == "important"

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

    def test_write_lock_reenters_from_child_task_spawned_while_held(self, tmp_path):
        # The tool executor runs every tool in its own asyncio.create_task, so a
        # tool that re-acquires its Run's session lock (channel_send targeting the
        # same chat) runs in a child of the lock-holding task, not the holder
        # itself. Keying reentrancy on the running task would deadlock here: the
        # holder awaits the child, the child blocks on the held lock. The child
        # must inherit the holder's reentrancy depth via contextvars and nest.
        manager = ChatSessionManager(tmp_path)

        async def scenario() -> bool:
            lock = manager.write_lock("coder", "session-one")
            async with lock:  # "Run" holds the lock across its tool cycle.

                async def tool() -> bool:
                    async with lock:  # re-entry from the child task must not block
                        return True

                # Created while the lock is held, so it copies the holder's
                # context — mirroring ToolExecutor.execute_many.
                return await asyncio.create_task(tool())

        assert asyncio.run(asyncio.wait_for(scenario(), timeout=1.0)) is True

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
        manager.record_terminal_run(
            "alpha",
            "sess",
            "run-one",
            "completed",
            "2026-07-20T10:00:00+00:00",
            source_project_id,
        )
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

        # Source home is empty afterwards (transcript and both sidecars gone).
        assert manager.exists("alpha", "sess", source_project_id) is False
        assert not source.sidecar_path.exists()
        assert not source.activity_path.exists()
        # Destination owns the session with identical history, ids, and timestamps.
        assert manager.exists("beta", "sess", target_project_id) is True
        assert destination.path == manager.sessions_dir("beta", target_project_id) / "sess.jsonl"
        assert [message.to_dict() for message in destination.load()] == original
        assert manager.get_metadata("beta", "sess", target_project_id) == {"platform": "telegram"}
        assert (
            manager.list_with_metadata("beta", target_project_id)[0]["unread_run_id"] == "run-one"
        )

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
            {"ephemeral_key": "remove", "platform": "telegram"},
        )

        asyncio.run(
            manager.move(
                "alpha",
                "sess",
                "beta",
                target_project_id="acme",
                strip_meta_keys=frozenset({"ephemeral_key"}),
            )
        )

        assert manager.get_metadata("beta", "sess", "acme") == {"platform": "telegram"}

    def test_move_fails_cleanly_on_destination_collision(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        source = self._populate(manager, "alpha", "sess")
        manager.set_metadata("alpha", "sess", {"platform": "telegram"})
        source.append_continuation_record({"type": "active"})
        # An (improbable) id collision already occupies the destination home.
        manager.create("beta", session_id="sess")

        with pytest.raises(ChatSessionError, match="destination session already exists"):
            asyncio.run(manager.move("alpha", "sess", "beta"))

        # No partial move: the source keeps both of its files.
        assert manager.exists("alpha", "sess") is True
        assert source.sidecar_path.exists()
        assert source.load_continuation_records() == [{"type": "active"}]
        assert [message.role for message in source.load()] == ["user", "assistant"]


class TestChatSessionManagerFork:
    """1:1 copy of a session into a fresh id with recorded provenance."""

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

    def test_fork_copies_transcript_verbatim_into_fresh_id(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        source = self._populate(manager, "alpha", "sess")

        fork = asyncio.run(manager.fork("alpha", "sess"))

        # Fresh, valid id distinct from the source; transcript copied byte-for-byte.
        assert fork.id != "sess"
        assert UUID(fork.id).version == 4
        assert fork.path.read_bytes() == source.path.read_bytes()
        assert [message.to_dict() for message in fork.load()] == [
            message.to_dict() for message in source.load()
        ]

    def test_fork_leaves_source_untouched(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        source = self._populate(manager, "alpha", "sess")
        manager.set_metadata("alpha", "sess", {"platform": "telegram"})
        source_bytes = source.path.read_bytes()
        source_sidecar = source.sidecar_path.read_bytes()

        asyncio.run(manager.fork("alpha", "sess"))

        assert source.path.read_bytes() == source_bytes
        assert source.sidecar_path.read_bytes() == source_sidecar

    def test_fork_does_not_copy_unread_completion(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        source = self._populate(manager, "alpha", "sess")
        manager.record_terminal_run(
            "alpha",
            "sess",
            "run-one",
            "completed",
            "2026-07-20T10:00:00+00:00",
        )

        fork = asyncio.run(manager.fork("alpha", "sess"))

        assert source.activity_path.exists()
        assert not fork.activity_path.exists()
        fork_row = next(row for row in manager.list_with_metadata("alpha") if row["id"] == fork.id)
        assert fork_row["has_unread_completion"] is False

    def test_fork_records_provenance(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        self._populate(manager, "alpha", "sess", project_id="acme")

        fork = asyncio.run(
            manager.fork("alpha", "sess", source_project_id="acme", target_project_id="acme")
        )

        provenance = manager.get_metadata("alpha", fork.id, "acme")[FORK_SOURCE_META_KEY]
        assert provenance["agent_id"] == "alpha"
        assert provenance["session_id"] == "sess"
        assert provenance["project_id"] == "acme"
        assert provenance["message_count"] == 2
        assert isinstance(provenance["forked_at"], str) and provenance["forked_at"]

    def test_fork_strips_requested_meta_keys_but_keeps_others(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        self._populate(manager, "alpha", "sess")
        manager.set_metadata(
            "alpha",
            "sess",
            {"pinned_skill_catalog": {"text": "cached"}, "title": "Keep me"},
        )

        fork = asyncio.run(
            manager.fork(
                "alpha",
                "sess",
                strip_meta_keys=frozenset({"pinned_skill_catalog"}),
            )
        )

        metadata = manager.get_metadata("alpha", fork.id)
        assert "pinned_skill_catalog" not in metadata
        assert metadata["title"] == "Keep me"
        assert FORK_SOURCE_META_KEY in metadata

    def test_fork_to_other_agent_lands_under_target(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        self._populate(manager, "alpha", "sess")

        fork = asyncio.run(manager.fork("alpha", "sess", target_agent_id="beta"))

        assert fork.path.parent == manager.sessions_dir("beta")
        assert manager.exists("beta", fork.id) is True
        assert manager.get_metadata("beta", fork.id)[FORK_SOURCE_META_KEY]["agent_id"] == "alpha"

    def test_fork_without_sidecar_writes_fork_source_only(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        source = self._populate(manager, "alpha", "sess")
        assert not source.sidecar_path.exists()

        fork = asyncio.run(manager.fork("alpha", "sess"))

        assert list(manager.get_metadata("alpha", fork.id)) == [FORK_SOURCE_META_KEY]

    def test_fork_of_unknown_session_raises(self, tmp_path):
        manager = ChatSessionManager(tmp_path)

        with pytest.raises(ChatSessionError, match="session does not exist"):
            asyncio.run(manager.fork("alpha", "missing"))


class TestForkStripPolicy:
    """Drift guards: the literal strip-set keys must track the owning domains.

    The sets live beside the fork primitive with literal key names (importing
    the owning constants would cycle back through ``core/chat``); these
    assertions fail the moment an owning domain renames its sidecar key.
    """

    def test_always_strip_set_tracks_owning_domain_constants(self) -> None:
        from core.automation.reflection import REFLECTION_COUNTERS_META_KEY
        from core.sessions import SESSION_FORK_ALWAYS_STRIP_META_KEYS
        from core.subagents.subagents import (
            SUBAGENT_PARENT_METADATA_KEY,
            SUBAGENT_SESSION_METADATA_FLAG,
        )

        assert SUBAGENT_SESSION_METADATA_FLAG in SESSION_FORK_ALWAYS_STRIP_META_KEYS
        assert SUBAGENT_PARENT_METADATA_KEY in SESSION_FORK_ALWAYS_STRIP_META_KEYS
        assert REFLECTION_COUNTERS_META_KEY in SESSION_FORK_ALWAYS_STRIP_META_KEYS

    def test_cross_agent_strip_set_tracks_chat_constants(self) -> None:
        from core.chat.chat import PINNED_SKILL_CATALOG_META_KEY, SEEN_SKILLS_META_KEY
        from core.sessions import SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS

        assert {
            PINNED_SKILL_CATALOG_META_KEY,
            SEEN_SKILLS_META_KEY,
        } == SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS


class TestContinuationJournal:
    def test_round_trips_compact_ordered_records(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")

        session.append_continuation_records(
            [{"type": "first", "value": "ä"}, {"type": "second", "value": 2}]
        )

        assert session.continuation_path == tmp_path / "session-one.continuation.jsonl"
        assert session.load_continuation_records() == [
            {"type": "first", "value": "ä"},
            {"type": "second", "value": 2},
        ]
        assert session.continuation_path.read_bytes().count(b"\n") == 2

    def test_load_truncates_only_a_torn_final_record(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.append_continuation_record({"type": "complete"})
        with session.continuation_path.open("ab") as journal:
            journal.write(b'{"type":"torn"')

        assert session.load_continuation_records() == [{"type": "complete"}]
        assert session.continuation_path.read_bytes().endswith(b"\n")

    def test_load_truncates_unterminated_final_record_with_non_object_shape(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.append_continuation_record({"type": "complete"})
        valid_content = session.continuation_path.read_bytes()
        with session.continuation_path.open("ab") as journal:
            journal.write(b"[]")

        assert session.load_continuation_records() == [{"type": "complete"}]
        assert session.continuation_path.read_bytes() == valid_content

    def test_load_rejects_a_complete_record_with_non_object_shape(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.continuation_path.write_bytes(b"[]\n")

        with pytest.raises(ChatSessionError, match="continuation record at line 1"):
            session.load_continuation_records()

    def test_load_rejects_a_malformed_complete_record(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.continuation_path.write_bytes(b'{"type":}\n')

        with pytest.raises(ChatSessionError, match="invalid continuation JSON"):
            session.load_continuation_records()

    def test_delete_removes_continuation_sidecar(self, tmp_path):
        session = ChatSession.create(tmp_path, session_id="session-one")
        session.append_continuation_record({"type": "active"})

        session.delete()

        assert not session.continuation_path.exists()

    def test_move_carries_continuation_for_identity_and_project_sessions(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        identity = manager.create("alpha", "identity")
        identity.append_continuation_record({"scope": "identity"})
        project = manager.create("alpha", "project", "acme")
        project.append_continuation_record({"scope": "project"})

        moved_identity = asyncio.run(manager.move("alpha", "identity", "beta"))
        moved_project = asyncio.run(
            manager.move(
                "alpha",
                "project",
                "beta",
                source_project_id="acme",
                target_project_id="other",
            )
        )

        assert moved_identity.load_continuation_records() == [{"scope": "identity"}]
        assert moved_project.load_continuation_records() == [{"scope": "project"}]
        assert not identity.continuation_path.exists()
        assert not project.continuation_path.exists()

    def test_archive_replaces_and_carries_continuation(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        session = manager.create("alpha", "session-one")
        session.append_continuation_record({"generation": 2})
        archive_dir = manager._archive_dir("alpha", None)
        archive_dir.mkdir(parents=True)
        archived = archive_dir / session.continuation_path.name
        archived.write_text('{"generation":1}\n', encoding="utf-8")

        asyncio.run(manager.archive("alpha", "session-one"))

        assert json.loads(archived.read_text(encoding="utf-8")) == {"generation": 2}
        assert not session.continuation_path.exists()

    def test_fork_deliberately_omits_continuation(self, tmp_path):
        manager = ChatSessionManager(tmp_path)
        source = manager.create("alpha", "session-one")
        source.append(ChatMessage.user("work"))
        source.append_continuation_record({"type": "active"})

        fork = asyncio.run(manager.fork("alpha", "session-one"))

        assert source.continuation_path.exists()
        assert not fork.continuation_path.exists()
