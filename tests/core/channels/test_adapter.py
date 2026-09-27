"""Shared Channel adapter helpers: inbound attachment blocks and the denied-chat log."""

from __future__ import annotations

import pytest

from core.attachments import AttachmentRecord
from core.channels.adapter import DeniedChatLog, content_blocks_for_attachment
from core.chat.content_blocks import FileBlock, MediaBlock


@pytest.mark.parametrize(
    ("media_type", "is_media"),
    [
        ("image/png", True),
        ("audio/ogg", True),
        ("video/mp4", True),
        # Text stays a file reference for the shared read renderer.
        ("text/plain", False),
        ("application/pdf", False),
    ],
)
def test_inbound_attachments_become_media_or_file_blocks(media_type: str, is_media: bool) -> None:
    record = AttachmentRecord(
        id="att-1",
        filename="inbound.bin",
        media_type=media_type,
        size_bytes=0,
        stored_at="2026-01-01T00:00:00Z",
        file_path="inbound.bin",
    )
    expected = (
        MediaBlock(
            type="media", attachment_id="att-1", filename="inbound.bin", media_type=media_type
        )
        if is_media
        else FileBlock(
            type="file", attachment_id="att-1", filename="inbound.bin", media_type=media_type
        )
    )

    assert content_blocks_for_attachment(record) == [expected]


def test_denied_chat_log_reports_new_chats_and_counts_repeats() -> None:
    log = DeniedChatLog()

    assert log.record(chat_id="123", kind="direct", display_name="Julian") is True
    [first] = log.entries()
    assert (first.chat_id, first.kind, first.display_name, first.count) == (
        "123",
        "direct",
        "Julian",
        1,
    )
    assert first.last_seen_at

    assert log.record(chat_id="123", kind="direct", display_name="Julian") is False
    assert [entry.count for entry in log.entries()] == [2]


def test_denied_chat_display_name_is_backfilled_but_never_lost() -> None:
    log = DeniedChatLog()

    log.record(chat_id="123", kind="group", display_name=None)
    log.record(chat_id="123", kind="group", display_name="Team Chat")
    log.record(chat_id="123", kind="group", display_name=None)

    assert log.entries()[0].display_name == "Team Chat"


def test_denied_chat_log_lists_most_recent_first_and_evicts_the_least_recent() -> None:
    log = DeniedChatLog(limit=2)
    log.record(chat_id="older", kind="direct", display_name=None)
    log.record(chat_id="newer", kind="direct", display_name=None)
    # Seeing a chat again makes it the most recent one.
    log.record(chat_id="older", kind="direct", display_name=None)
    assert [entry.chat_id for entry in log.entries()] == ["older", "newer"]

    log.record(chat_id="third", kind="direct", display_name=None)

    assert [entry.chat_id for entry in log.entries()] == ["third", "older"]
