"""Tests for canonical content block primitives."""

from dataclasses import FrozenInstanceError

import pytest

from core.chat.content_blocks import (
    ContentBlockError,
    FileBlock,
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
                ),
                {
                    "type": "media",
                    "attachment_id": "att_123",
                    "filename": "photo.png",
                    "media_type": "image/png",
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
        ],
    )
    def test_round_trip_for_each_block_type(self, block, expected):
        serialized = content_block_to_dict(block)

        assert serialized == expected
        assert content_block_from_dict(serialized) == block

    def test_unknown_type_raises_content_block_error(self):
        with pytest.raises(ContentBlockError, match="unknown content block type"):
            content_block_from_dict({"type": "unknown", "text": "hello"})

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "text"},
            {"type": "media", "attachment_id": "att_1", "filename": "photo.png"},
            {"type": "file", "attachment_id": "att_2", "media_type": "application/pdf"},
        ],
    )
    def test_missing_required_fields_raise_content_block_error(self, payload):
        with pytest.raises(ContentBlockError):
            content_block_from_dict(payload)

    def test_blocks_are_frozen(self):
        block = TextBlock(type="text", text="immutable")

        with pytest.raises(FrozenInstanceError):
            block.text = "changed"  # type: ignore[misc]
