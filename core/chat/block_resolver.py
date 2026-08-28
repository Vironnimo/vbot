"""Resolve attachment-backed content blocks into provider-ready payloads."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Protocol

from core.attachments import AttachmentStore
from core.chat.errors import ChatError
from core.chat.file_mentions import file_mention_request_text
from core.model_tasks import SpeechError
from core.tools.read import render_text_file
from core.utils.logging import get_logger
from core.utils.paths import model_path

JsonObject = dict[str, Any]

_LOGGER = get_logger("chat.block_resolver")

# Reasons appended to a path note when an attachment cannot be delivered as
# native content. The run degrades to the file path instead of aborting, so the
# agent can still route the file (e.g. hand the path to a capable sub-agent).
_VISION_UNAVAILABLE_REASON = (
    "this model has no vision capability, so the image itself cannot be shown; "
    "only the stored file path is provided"
)
_AUDIO_NO_STT_REASON = (
    "this model cannot accept audio and no speech-to-text service is available, "
    "so only the stored file path is provided"
)
_AUDIO_STT_FAILED_REASON = (
    "speech-to-text could not transcribe this audio, so only the stored file path is provided"
)
_UNSUPPORTED_MEDIA_REASON = (
    "this media type cannot be shown to the model directly, "
    "so only the stored file path is provided"
)


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
        block_index = 0
        while block_index < len(content):
            block = content[block_index]
            if self._is_text_attachment_block(block):
                attachment_blocks, raw = self._resolve_text_attachment_block(block)
                resolved_content.extend(attachment_blocks)
                if raw is not None and block_index + 1 < len(content):
                    following_block = content[block_index + 1]
                    if self._is_duplicate_attachment_text(following_block, raw):
                        block_index += 2
                        continue
                block_index += 1
                continue
            resolved_content.extend(
                await self._resolve_block(
                    block,
                    is_current_turn=is_current_turn,
                    input_modalities=input_modalities,
                    wire_media_types=wire_media_types,
                )
            )
            block_index += 1
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
        if block_type == "file_mention":
            # A durable send-time snapshot: rendered the same on every turn, so
            # replayed history stays byte-identical (prompt-cache friendly).
            return [{"type": "text", "text": file_mention_request_text(block)}]
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
                self._optional_positive_integer(block, "image_reference"),
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
            return self._resolve_video_block(
                attachment_id,
                filename,
                media_type,
                is_current_turn=is_current_turn,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
            )

        # An unexpected media prefix cannot be shown natively — hand over the file
        # path rather than aborting the run.
        return [
            self._path_note_block(
                "Media", attachment_id, filename, media_type, reason=_UNSUPPORTED_MEDIA_REASON
            )
        ]

    def _resolve_image_block(
        self,
        attachment_id: str,
        filename: str,
        media_type: str,
        image_reference: int | None,
        *,
        is_current_turn: bool,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        image_label = f"Image {image_reference}" if image_reference is not None else "Image"
        # A current-turn image to a model that cannot see degrades to a path note
        # explaining why, instead of aborting the run — a channel run would
        # otherwise fail on any inbound image. (Historical images degrade quietly
        # regardless of capability, below.)
        if is_current_turn and "image" not in input_modalities:
            return [
                self._path_note_block(
                    image_label,
                    attachment_id,
                    filename,
                    media_type,
                    reason=_VISION_UNAVAILABLE_REASON,
                )
            ]

        if not (is_current_turn and media_type in wire_media_types):
            # Either an earlier turn, or a vision model whose wire cannot carry this
            # image type: keep the blob path visible so the agent can open it.
            label = image_label if is_current_turn else f"{image_label} from an earlier turn"
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
            self._path_note_block(image_label, attachment_id, filename, media_type),
        ]

    def _resolve_video_block(
        self,
        attachment_id: str,
        filename: str,
        media_type: str,
        *,
        is_current_turn: bool,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        """Pass current-turn video only when both Model and wire support it."""

        if not (is_current_turn and "video" in input_modalities and media_type in wire_media_types):
            return [self._path_note_block("Video", attachment_id, filename, media_type)]

        blob_data = self._read_attachment_bytes(attachment_id)
        return [
            {
                "type": "media",
                "base64": base64.b64encode(blob_data).decode("ascii"),
                "media_type": media_type,
            },
            self._path_note_block("Video", attachment_id, filename, media_type),
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
            # Metadata unreadable: degrade to a path note (renders "file no longer
            # available") instead of aborting the run.
            return [self._path_note_block("Audio", attachment_id, filename, media_type)]

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

        return await self._transcribe_or_path_note(record, attachment_id, filename, media_type)

    async def _transcribe_or_path_note(
        self,
        record: Any,
        attachment_id: str,
        filename: str,
        media_type: str,
    ) -> list[JsonObject]:
        """Transcribe current-turn audio, or degrade to a path note on failure.

        No transcriber, a speech-to-text error, or an empty result never aborts
        the run: the agent keeps the file path to route the audio elsewhere (e.g.
        to a capable sub-agent). Only a genuine blob I/O fault still raises.
        """
        if self._transcriber is None:
            return [
                self._path_note_block(
                    "Audio", attachment_id, filename, media_type, reason=_AUDIO_NO_STT_REASON
                )
            ]

        blob_data = self._read_attachment_bytes(record.id)
        try:
            result = await self._transcriber.transcribe(
                blob_data, filename=filename, media_type=media_type
            )
        except SpeechError as exc:
            _LOGGER.warning(
                "Speech-to-text failed for audio %s (%s); degrading to path note: %s",
                filename,
                media_type,
                exc,
            )
            return [
                self._path_note_block(
                    "Audio", attachment_id, filename, media_type, reason=_AUDIO_STT_FAILED_REASON
                )
            ]

        text = getattr(result, "text", None)
        if not isinstance(text, str) or not text.strip():
            _LOGGER.warning(
                "Speech-to-text produced no text for audio %s (%s); degrading to path note",
                filename,
                media_type,
            )
            return [
                self._path_note_block(
                    "Audio", attachment_id, filename, media_type, reason=_AUDIO_STT_FAILED_REASON
                )
            ]

        try:
            self._attachment_store.set_transcription(record.id, text)
        except Exception as exc:
            _LOGGER.warning("Could not cache transcription for attachment %s: %s", record.id, exc)

        return [
            self._transcription_block(filename, media_type, text),
            self._path_note_block("Audio", attachment_id, filename, media_type),
        ]

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
        *,
        reason: str | None = None,
    ) -> JsonObject:
        # Media that is not resent as binary content keeps the blob path visible
        # so the agent can still open the file with the read tool. An optional
        # reason explains why the binary content itself was withheld.
        record = self._load_record_or_none(attachment_id)
        if record is None:
            return {
                "type": "text",
                "text": f"[{label}: {filename} ({media_type}) — file no longer available]",
            }
        reason_prefix = f"{reason} — " if reason else ""
        note_text = (
            f"[{label}: {filename} ({media_type}) — "
            f"{reason_prefix}Path: {model_path(record.file_path)}]"
        )
        return {"type": "text", "text": note_text}

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

    def _resolve_text_attachment_block(
        self, block: JsonObject
    ) -> tuple[list[JsonObject], bytes | None]:
        """Render a text attachment through the read tool's shared text renderer.

        The file block remains the sole persisted representation. This keeps a
        complete source file out of session history while preserving the exact
        bounded initial read that an agent gets from the ``read`` tool.
        """
        attachment_id = self._require_string(block, "attachment_id")
        filename = self._require_string(block, "filename")
        media_type = self._require_string(block, "media_type")
        path_note = self._path_note_block("File", attachment_id, filename, media_type)
        try:
            raw = self._read_attachment_bytes(attachment_id)
        except AttachmentResolveError:
            return [path_note], None

        content = render_text_file(raw)
        rendered = [path_note]
        if content:
            rendered.append({"type": "text", "text": content})
        return rendered, raw

    @staticmethod
    def _is_text_attachment_block(block: Any) -> bool:
        return (
            isinstance(block, dict)
            and block.get("type") == "file"
            and isinstance(block.get("media_type"), str)
            and block["media_type"].startswith("text/")
        )

    @staticmethod
    def _is_duplicate_attachment_text(block: Any, raw: bytes) -> bool:
        """Recognize the former file-reference-plus-full-text representation.

        Exact duplicate content is never meaningful as a second block. Omitting it
        on request assembly immediately bounds already-persisted conversations,
        while text that merely follows a file attachment remains intact.
        """
        if not isinstance(block, dict) or block.get("type") != "text":
            return False
        text = block.get("text")
        return isinstance(text, str) and text.encode("utf-8") == raw

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

    @staticmethod
    def _optional_positive_integer(data: JsonObject, key: str) -> int | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ChatError(f"content block field '{key}' must be a positive integer")
        return value
