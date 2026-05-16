"""Canonical content block primitives for attachment-aware user messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from core.utils.errors import VBotError


class ContentBlockError(VBotError):
    """Raised when content blocks cannot be serialized or parsed."""


@dataclass(frozen=True)
class TextBlock:
    """Plain text content block."""

    type: Literal["text"]
    text: str


@dataclass(frozen=True)
class MediaBlock:
    """Attachment-backed media block (image/audio/video families)."""

    type: Literal["media"]
    attachment_id: str
    filename: str
    media_type: str


@dataclass(frozen=True)
class FileBlock:
    """Attachment-backed non-media file block."""

    type: Literal["file"]
    attachment_id: str
    filename: str
    media_type: str


ContentBlock = TextBlock | MediaBlock | FileBlock


def content_block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """Serialize one content block to a canonical JSON-compatible object."""
    if isinstance(block, TextBlock):
        return {
            "type": "text",
            "text": block.text,
        }

    if isinstance(block, MediaBlock):
        return {
            "type": "media",
            "attachment_id": block.attachment_id,
            "filename": block.filename,
            "media_type": block.media_type,
        }

    if isinstance(block, FileBlock):
        return {
            "type": "file",
            "attachment_id": block.attachment_id,
            "filename": block.filename,
            "media_type": block.media_type,
        }

    raise ContentBlockError(f"unsupported content block type: {type(block).__name__}")


def content_block_from_dict(data: dict[str, Any]) -> ContentBlock:
    """Parse one canonical content block object."""
    block_type = _require_string(data, "type")

    if block_type == "text":
        return TextBlock(type="text", text=_require_string(data, "text"))

    if block_type == "media":
        return MediaBlock(
            type="media",
            attachment_id=_require_string(data, "attachment_id"),
            filename=_require_string(data, "filename"),
            media_type=_require_string(data, "media_type"),
        )

    if block_type == "file":
        return FileBlock(
            type="file",
            attachment_id=_require_string(data, "attachment_id"),
            filename=_require_string(data, "filename"),
            media_type=_require_string(data, "media_type"),
        )

    raise ContentBlockError(f"unknown content block type: {block_type}")


def _require_string(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise ContentBlockError(f"content block missing required field: {key}")

    value = data[key]
    if not isinstance(value, str):
        raise ContentBlockError(f"content block field '{key}' must be a string")

    return value
