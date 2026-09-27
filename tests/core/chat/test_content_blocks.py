"""Tests for canonical content block primitives."""

from dataclasses import FrozenInstanceError

import pytest

from core.chat.content_blocks import (
    ContentBlockError,
    FileBlock,
    FileMentionBlock,
    MediaBlock,
    TextBlock,
    content_block_from_dict,
    content_block_to_dict,
)


class TestContentBlocks:
    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            (
                TextBlock(type="text", text="hello world"),
                {"type": "text", "text": "hello world"},
            ),
            (
                MediaBlock(
                    type="media",
                    attachment_id="att_123",
                    filename="photo.png",
                    media_type="image/png",
                    image_reference=2,
                ),
                {
                    "type": "media",
                    "attachment_id": "att_123",
                    "filename": "photo.png",
                    "media_type": "image/png",
                    "image_reference": 2,
                },
            ),
            (
                FileBlock(
                    type="file",
                    attachment_id="att_456",
                    filename="report.pdf",
                    media_type="application/pdf",
                ),
                {
                    "type": "file",
                    "attachment_id": "att_456",
                    "filename": "report.pdf",
                    "media_type": "application/pdf",
                },
            ),
            (
                FileMentionBlock(
                    type="file_mention",
                    path="src/app.py",
                    status="inlined",
                    text="print('hi')",
                    size_bytes=11,
                ),
                {
                    "type": "file_mention",
                    "path": "src/app.py",
                    "status": "inlined",
                    "text": "print('hi')",
                    "size_bytes": 11,
                },
            ),
            (
                FileMentionBlock(
                    type="file_mention",
                    path="big.log",
                    status="too_large",
                    text=None,
                    size_bytes=9_000_000,
                ),
                {
                    "type": "file_mention",
                    "path": "big.log",
                    "status": "too_large",
                    "text": None,
                    "size_bytes": 9_000_000,
                },
            ),
        ],
    )
    def test_round_trip_for_each_block_type(self, block, expected):
        serialized = content_block_to_dict(block)

        assert serialized == expected
        assert content_block_from_dict(serialized) == block
        with pytest.raises(FrozenInstanceError):
            block.type = "changed"

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "unknown", "text": "hello"},
            {"type": "text"},
            {"type": "media", "attachment_id": "att_1", "filename": "photo.png"},
            {"type": "file", "attachment_id": "att_2", "media_type": "application/pdf"},
            {"type": "file_mention", "status": "inlined", "text": "x"},
            {"type": "file_mention", "path": "a.py", "status": "weird", "text": None},
            {"type": "file_mention", "path": "a.py", "status": "inlined", "text": None},
            {"type": "file_mention", "path": "a.py", "status": "missing", "text": "body"},
            {
                "type": "media",
                "attachment_id": "att_1",
                "filename": "a.png",
                "media_type": "image/png",
                "image_reference": 0,
            },
            {
                "type": "file_mention",
                "path": "a.py",
                "status": "inlined",
                "text": "body",
                "size_bytes": "4",
            },
        ],
        ids=[
            "unknown-type",
            "text-without-text",
            "media-without-media-type",
            "file-without-filename",
            "mention-without-path",
            "mention-unknown-status",
            "inlined-without-text",
            "degraded-with-text",
            "non-positive-image-reference",
            "non-integer-size",
        ],
    )
    def test_invalid_payloads_raise_content_block_error(self, payload):
        with pytest.raises(ContentBlockError):
            content_block_from_dict(payload)
