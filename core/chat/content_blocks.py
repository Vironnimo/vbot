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
    # Assigned by Chat when an image enters a Session. This is a durable
    # conversation reference, not a filename or a storage address.
    image_reference: int | None = None


@dataclass(frozen=True)
class FileBlock:
    """Attachment-backed non-media file block."""

    type: Literal["file"]
    attachment_id: str
    filename: str
    media_type: str


# Snapshot outcomes for a file the user referenced with an ``@``-mention:
# ``inlined`` carries the file content, the others degrade to a reference note.
FILE_MENTION_STATUSES = ("inlined", "too_large", "not_text", "missing")


@dataclass(frozen=True)
class FileMentionBlock:
    """Send-time snapshot of a file the user referenced with an ``@``-mention.

    ``path`` is the path as mentioned (resolved against the session cwd),
    ``text`` is the file content snapshot and is present exactly when
    ``status == "inlined"``. The snapshot is durable: replaying history always
    shows the file as it was when the user sent the message.
    """

    type: Literal["file_mention"]
    path: str
    status: str
    text: str | None
    size_bytes: int | None


ContentBlock = TextBlock | MediaBlock | FileBlock | FileMentionBlock


def content_block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """Serialize one content block to a canonical JSON-compatible object."""
    if isinstance(block, TextBlock):
        return {
            "type": "text",
            "text": block.text,
        }

    if isinstance(block, MediaBlock):
        payload: dict[str, Any] = {
            "type": "media",
            "attachment_id": block.attachment_id,
            "filename": block.filename,
            "media_type": block.media_type,
        }
        if block.image_reference is not None:
            payload["image_reference"] = block.image_reference
        return payload

    if isinstance(block, FileBlock):
        return {
            "type": "file",
            "attachment_id": block.attachment_id,
            "filename": block.filename,
            "media_type": block.media_type,
        }

    if isinstance(block, FileMentionBlock):
        return {
            "type": "file_mention",
            "path": block.path,
            "status": block.status,
            "text": block.text,
            "size_bytes": block.size_bytes,
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
            image_reference=_optional_positive_integer(data, "image_reference"),
        )

    if block_type == "file":
        return FileBlock(
            type="file",
            attachment_id=_require_string(data, "attachment_id"),
            filename=_require_string(data, "filename"),
            media_type=_require_string(data, "media_type"),
        )

    if block_type == "file_mention":
        return _file_mention_from_dict(data)

    raise ContentBlockError(f"unknown content block type: {block_type}")


def _file_mention_from_dict(data: dict[str, Any]) -> FileMentionBlock:
    status = _require_string(data, "status")
    if status not in FILE_MENTION_STATUSES:
        raise ContentBlockError(f"unknown file_mention status: {status}")

    text = data.get("text")
    if text is not None and not isinstance(text, str):
        raise ContentBlockError("content block field 'text' must be a string or null")
    if (status == "inlined") != (text is not None):
        raise ContentBlockError("file_mention text must be present exactly for status 'inlined'")

    size_bytes = data.get("size_bytes")
    if size_bytes is not None and (not isinstance(size_bytes, int) or isinstance(size_bytes, bool)):
        raise ContentBlockError("content block field 'size_bytes' must be an integer or null")

    return FileMentionBlock(
        type="file_mention",
        path=_require_string(data, "path"),
        status=status,
        text=text,
        size_bytes=size_bytes,
    )


def _require_string(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise ContentBlockError(f"content block missing required field: {key}")

    value = data[key]
    if not isinstance(value, str):
        raise ContentBlockError(f"content block field '{key}' must be a string")

    return value


def _optional_positive_integer(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ContentBlockError(f"content block field '{key}' must be a positive integer")
    return value
