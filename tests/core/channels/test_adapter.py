"""Unit tests for shared channel adapter helpers."""

from __future__ import annotations

import pytest

from core.attachments import AttachmentRecord
from core.channels.adapter import content_block_for_attachment
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
    block = content_block_for_attachment(_record(media_type))

    assert isinstance(block, MediaBlock)
    assert block.media_type == media_type
    assert block.attachment_id == "att-1"
    assert block.filename == "inbound.bin"


def test_text_uses_extracted_content() -> None:
    block = content_block_for_attachment(_record("text/plain", text_content="hello"))

    assert isinstance(block, TextBlock)
    assert block.text == "hello"


def test_text_without_extracted_content_is_empty_text_block() -> None:
    block = content_block_for_attachment(_record("text/markdown", text_content=None))

    assert isinstance(block, TextBlock)
    assert block.text == ""


def test_other_types_stay_generic_file_block() -> None:
    block = content_block_for_attachment(_record("application/pdf"))

    assert isinstance(block, FileBlock)
    assert block.media_type == "application/pdf"
    assert block.attachment_id == "att-1"
