"""Resolve attachment-backed content blocks into provider-ready payloads."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from core.attachments import AttachmentStore
from core.chat.chat import ChatError

JsonObject = dict[str, Any]


class AttachmentResolveError(ChatError):
    """Raised when an attachment blob cannot be loaded for content resolution."""


class ContentBlockResolver:
    """Resolve canonical content blocks into provider-facing request parts."""

    def __init__(self, attachment_store: AttachmentStore) -> None:
        self._attachment_store = attachment_store

    def resolve_messages(
        self,
        messages: list[JsonObject],
        *,
        current_user_message_id: str,
        vision_supported: bool,
    ) -> list[JsonObject]:
        """Return a new message list with user content blocks resolved."""
        resolved_messages: list[JsonObject] = []
        for message in messages:
            resolved_messages.append(
                self._resolve_message(
                    message,
                    current_user_message_id=current_user_message_id,
                    vision_supported=vision_supported,
                )
            )
        return resolved_messages

    def _resolve_message(
        self,
        message: JsonObject,
        *,
        current_user_message_id: str,
        vision_supported: bool,
    ) -> JsonObject:
        resolved_message = dict(message)
        if message.get("role") != "user":
            return resolved_message

        content = message.get("content")
        if not isinstance(content, list):
            return resolved_message

        is_current_turn = message.get("id") == current_user_message_id
        resolved_message["content"] = [
            self._resolve_block(
                block,
                is_current_turn=is_current_turn,
                vision_supported=vision_supported,
            )
            for block in content
        ]
        return resolved_message

    def _resolve_block(
        self,
        block: Any,
        *,
        is_current_turn: bool,
        vision_supported: bool,
    ) -> JsonObject:
        if not isinstance(block, dict):
            raise ChatError("content blocks must be objects")

        block_type = block.get("type")
        if block_type == "text":
            return {"type": "text", "text": self._require_string(block, "text")}
        if block_type == "media":
            return self._resolve_media_block(
                block,
                is_current_turn=is_current_turn,
                vision_supported=vision_supported,
            )
        if block_type == "file":
            return self._resolve_file_block(block)
        raise ChatError(f"unsupported content block type: {block_type}")

    def _resolve_media_block(
        self,
        block: JsonObject,
        *,
        is_current_turn: bool,
        vision_supported: bool,
    ) -> JsonObject:
        attachment_id = self._require_string(block, "attachment_id")
        filename = self._require_string(block, "filename")
        media_type = self._require_string(block, "media_type")

        if not is_current_turn:
            return {"type": "text", "text": f"[Bild: {filename}]"}

        if not vision_supported:
            raise ChatError("Model does not support vision; cannot process image attachment")

        if not media_type.startswith("image/"):
            raise ChatError(
                f"V1 supports only image/* media attachments; received media_type={media_type}"
            )

        blob_data = self._read_attachment_bytes(attachment_id)
        return {
            "type": "media",
            "base64": base64.b64encode(blob_data).decode("ascii"),
            "media_type": media_type,
        }

    def _resolve_file_block(self, block: JsonObject) -> JsonObject:
        attachment_id = self._require_string(block, "attachment_id")
        filename = self._require_string(block, "filename")
        media_type = self._require_string(block, "media_type")
        record = self._read_attachment_record(attachment_id)
        return {
            "type": "text",
            "text": f"[File: {filename} ({media_type}) — Path: {record.file_path}]",
        }

    def _read_attachment_record(self, attachment_id: str) -> Any:
        try:
            return self._attachment_store.get(attachment_id)
        except Exception as exc:  # pragma: no cover - exact exception depends on store state
            raise AttachmentResolveError(
                f"Failed to load attachment metadata for id '{attachment_id}'"
            ) from exc

    def _read_attachment_bytes(self, attachment_id: str) -> bytes:
        record = self._read_attachment_record(attachment_id)
        try:
            return Path(record.file_path).read_bytes()
        except OSError as exc:
            raise AttachmentResolveError(
                f"Failed to read attachment blob for id '{attachment_id}'"
            ) from exc

    @staticmethod
    def _require_string(data: JsonObject, key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str):
            raise ChatError(f"content block field '{key}' must be a string")
        return value
