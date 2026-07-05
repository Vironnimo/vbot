"""Unit tests for shared channel adapter helpers."""

from __future__ import annotations

import pytest

from core.attachments import AttachmentRecord
from core.channels.adapter import DeniedChatLog, content_blocks_for_attachment
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock


def _record(media_type: str, *, text_content: str | None = None) -> AttachmentRecord:
    return AttachmentRecord(
        id="att-1",
        filename="inbound.bin",
        media_type=media_type,
        size_bytes=0,
        stored_at="2026-01-01T00:00:00Z",
        file_path="inbound.bin",
        text_content=text_content,
    )


@pytest.mark.parametrize(
    "media_type",
    ["image/png", "image/jpeg", "audio/mpeg", "audio/ogg", "video/mp4", "video/webm"],
)
def test_image_audio_video_become_media_block(media_type: str) -> None:
    blocks = content_blocks_for_attachment(_record(media_type))

    assert blocks == [
        MediaBlock(
            type="media",
            attachment_id="att-1",
            filename="inbound.bin",
            media_type=media_type,
        )
    ]


def test_text_becomes_file_reference_plus_extracted_content() -> None:
    # A text attachment is carried as a file reference (resolved to a path note so
    # the agent can forward or reopen the original) plus the extracted content.
    blocks = content_blocks_for_attachment(_record("text/plain", text_content="hello"))

    assert blocks == [
        FileBlock(
            type="file",
            attachment_id="att-1",
            filename="inbound.bin",
            media_type="text/plain",
        ),
        TextBlock(type="text", text="hello"),
    ]


def test_text_without_extracted_content_is_file_reference_only() -> None:
    blocks = content_blocks_for_attachment(_record("text/markdown", text_content=None))

    assert blocks == [
        FileBlock(
            type="file",
            attachment_id="att-1",
            filename="inbound.bin",
            media_type="text/markdown",
        )
    ]


def test_other_types_stay_generic_file_block() -> None:
    blocks = content_blocks_for_attachment(_record("application/pdf"))

    assert blocks == [
        FileBlock(
            type="file",
            attachment_id="att-1",
            filename="inbound.bin",
            media_type="application/pdf",
        )
    ]


class TestDeniedChatLog:
    def test_first_record_is_new_and_listed(self) -> None:
        log = DeniedChatLog()

        is_new = log.record(chat_id="123", kind="direct", display_name="Julian")

        assert is_new is True
        entries = log.entries()
        assert len(entries) == 1
        assert entries[0].chat_id == "123"
        assert entries[0].kind == "direct"
        assert entries[0].display_name == "Julian"
        assert entries[0].count == 1
        assert entries[0].last_seen_at

    def test_repeat_record_updates_count_and_is_not_new(self) -> None:
        log = DeniedChatLog()
        log.record(chat_id="123", kind="direct", display_name="Julian")

        is_new = log.record(chat_id="123", kind="direct", display_name="Julian")

        assert is_new is False
        entries = log.entries()
        assert len(entries) == 1
        assert entries[0].count == 2

    def test_display_name_never_regresses_to_none(self) -> None:
        log = DeniedChatLog()
        log.record(chat_id="123", kind="group", display_name="Team Chat")

        log.record(chat_id="123", kind="group", display_name=None)

        assert log.entries()[0].display_name == "Team Chat"

    def test_missing_display_name_is_backfilled_later(self) -> None:
        log = DeniedChatLog()
        log.record(chat_id="123", kind="group", display_name=None)

        log.record(chat_id="123", kind="group", display_name="Team Chat")

        assert log.entries()[0].display_name == "Team Chat"

    def test_limit_evicts_oldest_entry(self) -> None:
        log = DeniedChatLog(limit=2)
        log.record(chat_id="first", kind="direct", display_name=None)
        log.record(chat_id="second", kind="direct", display_name=None)

        log.record(chat_id="third", kind="direct", display_name=None)

        chat_ids = {entry.chat_id for entry in log.entries()}
        assert chat_ids == {"second", "third"}

    def test_entries_are_most_recent_first(self) -> None:
        log = DeniedChatLog()
        log.record(chat_id="older", kind="direct", display_name=None)
        log.record(chat_id="newer", kind="direct", display_name=None)

        log.record(chat_id="older", kind="direct", display_name=None)

        assert [entry.chat_id for entry in log.entries()] == ["older", "newer"]
