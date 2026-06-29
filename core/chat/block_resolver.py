"""Resolve attachment-backed content blocks into provider-ready payloads."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Protocol

from core.attachments import AttachmentStore
from core.chat.errors import ChatError
from core.model_tasks import SpeechError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

_LOGGER = get_logger("chat.block_resolver")


class SpeechTranscriber(Protocol):
    """Speech-to-text hook used to degrade audio attachments to text."""

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Any:
        """Return an object with a ``text`` attribute for the given audio bytes."""
        ...


class AttachmentResolveError(ChatError):
    """Raised when an attachment blob cannot be loaded for content resolution."""


class ContentBlockResolver:
    """Resolve canonical content blocks into provider-facing request parts."""

    def __init__(
        self,
        attachment_store: AttachmentStore,
        *,
        transcriber: SpeechTranscriber | None = None,
    ) -> None:
        self._attachment_store = attachment_store
        self._transcriber = transcriber

    async def resolve_messages(
        self,
        messages: list[JsonObject],
        *,
        current_user_message_id: str,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        """Return a new message list with user content blocks resolved.

        ``input_modalities`` is what the *model* can consume; ``wire_media_types``
        is what the chosen *adapter*'s wire can carry. An attachment goes native
        only on their intersection for the current turn; otherwise it degrades by
        per-modality policy. The resolver holds no provider format knowledge — it
        only intersects the two sets it is handed.
        """
        resolved_messages: list[JsonObject] = []
        for message in messages:
            resolved_messages.append(
                await self._resolve_message(
                    message,
                    current_user_message_id=current_user_message_id,
                    input_modalities=input_modalities,
                    wire_media_types=wire_media_types,
                )
            )
        return resolved_messages

    async def _resolve_message(
        self,
        message: JsonObject,
        *,
        current_user_message_id: str,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> JsonObject:
        resolved_message = dict(message)
        if message.get("role") != "user":
            return resolved_message

        content = message.get("content")
        if not isinstance(content, list):
            return resolved_message

        is_current_turn = message.get("id") == current_user_message_id
        resolved_content: list[JsonObject] = []
        for block in content:
            resolved_content.extend(
                await self._resolve_block(
                    block,
                    is_current_turn=is_current_turn,
                    input_modalities=input_modalities,
                    wire_media_types=wire_media_types,
                )
            )
        resolved_message["content"] = resolved_content
        return resolved_message

    async def _resolve_block(
        self,
        block: Any,
        *,
        is_current_turn: bool,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        if not isinstance(block, dict):
            raise ChatError("content blocks must be objects")

        block_type = block.get("type")
        if block_type == "text":
            return [{"type": "text", "text": self._require_string(block, "text")}]
        if block_type == "media":
            return await self._resolve_media_block(
                block,
                is_current_turn=is_current_turn,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
            )
        if block_type == "file":
            return self._resolve_file_block(
                block,
                is_current_turn=is_current_turn,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
            )
        raise ChatError(f"unsupported content block type: {block_type}")

    async def _resolve_media_block(
        self,
        block: JsonObject,
        *,
        is_current_turn: bool,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        attachment_id = self._require_string(block, "attachment_id")
        filename = self._require_string(block, "filename")
        media_type = self._require_string(block, "media_type")

        if media_type.startswith("image/"):
            return self._resolve_image_block(
                attachment_id,
                filename,
                media_type,
                is_current_turn=is_current_turn,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
            )
        if media_type.startswith("audio/"):
            return await self._resolve_audio_block(
                attachment_id,
                filename,
                media_type,
                is_current_turn=is_current_turn,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
            )
        if media_type.startswith("video/"):
            # No supported provider wire accepts raw video; the path note lets the
            # agent work on the file with tools instead.
            return [self._path_note_block("Video", attachment_id, filename, media_type)]

        raise ChatError(f"unsupported media attachment type: {media_type}")

    def _resolve_image_block(
        self,
        attachment_id: str,
        filename: str,
        media_type: str,
        *,
        is_current_turn: bool,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        # A current-turn image sent to a model that cannot see is a hard error —
        # the agent intended the model to look at it. (Historical images degrade
        # quietly regardless of model capability, so this gate is current-turn only.)
        if is_current_turn and "image" not in input_modalities:
            raise ChatError("Model does not support vision; cannot process image attachment")

        if not (is_current_turn and media_type in wire_media_types):
            # Either an earlier turn, or a vision model whose wire cannot carry this
            # image type: keep the blob path visible so the agent can open it.
            label = "Image" if is_current_turn else "Image from an earlier turn"
            return [self._path_note_block(label, attachment_id, filename, media_type)]

        blob_data = self._read_attachment_bytes(attachment_id)
        native_block = {
            "type": "media",
            "base64": base64.b64encode(blob_data).decode("ascii"),
            "media_type": media_type,
        }
        # The native image rides with a path note so the agent also holds a handle to
        # the original file (e.g. to forward it), not only the pixels.
        return [
            native_block,
            self._path_note_block("Image", attachment_id, filename, media_type),
        ]

    async def _resolve_audio_block(
        self,
        attachment_id: str,
        filename: str,
        media_type: str,
        *,
        is_current_turn: bool,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        record = self._load_record_or_none(attachment_id)

        if record is not None and isinstance(record.transcription, str):
            return [
                self._transcription_block(filename, media_type, record.transcription),
                self._path_note_block("Audio", attachment_id, filename, media_type),
            ]

        if not is_current_turn:
            return [
                self._path_note_block(
                    "Audio from an earlier turn", attachment_id, filename, media_type
                )
            ]

        if record is None:
            raise AttachmentResolveError(
                f"Failed to load attachment metadata for id '{attachment_id}'"
            )

        if "audio" in input_modalities and media_type in wire_media_types:
            blob_data = self._read_attachment_bytes(attachment_id)
            native_block = {
                "type": "media",
                "base64": base64.b64encode(blob_data).decode("ascii"),
                "media_type": media_type,
            }
            return [
                native_block,
                self._path_note_block("Audio", attachment_id, filename, media_type),
            ]

        transcription = await self._transcribe_attachment(record, filename, media_type)
        return [
            self._transcription_block(filename, media_type, transcription),
            self._path_note_block("Audio", attachment_id, filename, media_type),
        ]

    async def _transcribe_attachment(
        self,
        record: Any,
        filename: str,
        media_type: str,
    ) -> str:
        if self._transcriber is None:
            raise ChatError(
                "Model does not support audio input and no speech-to-text "
                "service is available; cannot process audio attachment"
            )

        blob_data = self._read_attachment_bytes(record.id)
        try:
            result = await self._transcriber.transcribe(
                blob_data, filename=filename, media_type=media_type
            )
        except SpeechError as exc:
            raise ChatError(
                f"Audio attachment '{filename}' could not be transcribed: {exc}"
            ) from exc

        text = getattr(result, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ChatError(f"Audio attachment '{filename}' produced an empty transcription")

        try:
            self._attachment_store.set_transcription(record.id, text)
        except Exception as exc:
            _LOGGER.warning("Could not cache transcription for attachment %s: %s", record.id, exc)
        return text

    @staticmethod
    def _transcription_block(filename: str, media_type: str, transcription: str) -> JsonObject:
        return {
            "type": "text",
            "text": (
                f"[Audio attachment {filename} ({media_type}) — automatic transcription, "
                f"may contain recognition errors]:\n{transcription}"
            ),
        }

    def _path_note_block(
        self,
        label: str,
        attachment_id: str,
        filename: str,
        media_type: str,
    ) -> JsonObject:
        # Media that is not resent as binary content keeps the blob path visible
        # so the agent can still open the file with the read tool.
        record = self._load_record_or_none(attachment_id)
        if record is None:
            return {
                "type": "text",
                "text": f"[{label}: {filename} ({media_type}) — file no longer available]",
            }
        return {
            "type": "text",
            "text": f"[{label}: {filename} ({media_type}) — Path: {record.file_path}]",
        }

    def _resolve_file_block(
        self,
        block: JsonObject,
        *,
        is_current_turn: bool,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        attachment_id = self._require_string(block, "attachment_id")
        filename = self._require_string(block, "filename")
        media_type = self._require_string(block, "media_type")

        modality = "pdf" if media_type == "application/pdf" else "file"
        # A text file's content rides inline in its sibling text block, so the file
        # reference only contributes the path note — never a native document, which
        # would send the same content a second time.
        native = (
            is_current_turn
            and not media_type.startswith("text/")
            and modality in input_modalities
            and media_type in wire_media_types
        )
        if native:
            blob_data = self._read_attachment_bytes(attachment_id)
            document_block = {
                "type": "document",
                "base64": base64.b64encode(blob_data).decode("ascii"),
                "media_type": media_type,
                "filename": filename,
            }
            # The native document rides with a path note so the agent also holds a
            # handle to the original file, not only the parsed document.
            return [
                document_block,
                self._path_note_block("File", attachment_id, filename, media_type),
            ]

        # Not native (text, unsupported model/wire, or an earlier turn): the path
        # note keeps the blob openable with the read tool and forwardable as a file.
        return [self._path_note_block("File", attachment_id, filename, media_type)]

    def _load_record_or_none(self, attachment_id: str) -> Any | None:
        try:
            return self._attachment_store.get(attachment_id)
        except Exception:
            return None

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
