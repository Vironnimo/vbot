"""Blob-backed attachment storage with sidecar metadata."""

from __future__ import annotations

import io
import json
import os
import re
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from core.utils.errors import VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

_OOXML_PREFIX = "application/vnd.openxmlformats-officedocument."
_OOXML_WILDCARD = "application/vnd.openxmlformats-officedocument.*"
# An OOXML file's ``[Content_Types].xml`` is a small manifest — a few KiB even for
# large documents. Reading it unbounded lets a crafted ZIP entry decompress to
# gigabytes from a within-upload-limit file (a zip bomb), so the sniff decompresses
# at most this many bytes and treats any overflow as "not OOXML".
_MAX_OOXML_CONTENT_TYPES_BYTES = 1_048_576
_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_MIME_ALLOWLIST = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "application/pdf",
        _OOXML_WILDCARD,
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)

_LOGGER = get_logger("attachments")


class AttachmentError(VBotError):
    """Base class for expected attachment-storage errors."""


class AttachmentNotFoundError(AttachmentError):
    """Raised when attachment metadata is missing for a requested id."""


class AttachmentTooLargeError(AttachmentError):
    """Raised when an uploaded file exceeds the configured size limit."""


class AttachmentTypeNotAllowedError(AttachmentError):
    """Raised when a file's sniffed MIME type is outside the allowlist."""


@dataclass(frozen=True)
class AttachmentRecord:
    """Persisted metadata for one attachment blob."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    stored_at: str
    file_path: str
    text_content: str | None
    # Cached speech-to-text result for audio attachments; written once on first
    # transcription so later requests reuse it instead of re-calling STT.
    transcription: str | None = None


class AttachmentStore:
    """Store and fetch attachment blobs under ``<data_dir>/attachments``."""

    def __init__(self, data_dir: Path, *, max_size_bytes: int = 20_971_520) -> None:
        if max_size_bytes <= 0:
            raise AttachmentError("max_size_bytes must be greater than 0")

        self._attachments_dir = Path(data_dir).expanduser() / "attachments"
        self._max_size_bytes = max_size_bytes

    @property
    def max_size_bytes(self) -> int:
        """Configured maximum accepted attachment size in bytes."""

        return self._max_size_bytes

    def ensure_within_limit(self, reported_size_bytes: int | None) -> None:
        """Reject an oversized attachment from its reported size, before its bytes exist.

        Transport adapters (channels) learn a file's size from platform metadata before
        downloading it. Calling this first refuses an oversized file without ever
        materializing it in memory; ``store`` still re-checks once the bytes arrive, as a
        backstop. A ``None`` size means the platform reported none, so the pre-check is
        skipped and only the backstop applies.
        """

        if reported_size_bytes is not None and reported_size_bytes > self._max_size_bytes:
            raise AttachmentTooLargeError(
                f"Attachment size {reported_size_bytes} exceeds limit {self._max_size_bytes}"
            )

    def store(self, filename: str, data: bytes) -> AttachmentRecord:
        """Persist one blob and sidecar metadata, then return the record."""

        size_bytes = len(data)
        if size_bytes > self._max_size_bytes:
            raise AttachmentTooLargeError(
                f"Attachment size {size_bytes} exceeds limit {self._max_size_bytes}"
            )

        media_type = _sniff_mime(data, filename)
        if not _is_allowed_mime(media_type):
            raise AttachmentTypeNotAllowedError(f"Attachment type not allowed: {media_type}")

        attachment_id = str(uuid4())
        stored_at = datetime.now(UTC).isoformat()

        self._attachments_dir.mkdir(parents=True, exist_ok=True)

        blob_path = self._blob_path(attachment_id)
        sidecar_path = self._sidecar_path(attachment_id)
        text_content = _extract_text_content(data, media_type)
        record = AttachmentRecord(
            id=attachment_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes,
            stored_at=stored_at,
            file_path=str(blob_path),
            text_content=text_content,
        )

        self._write_blob(blob_path, data)
        try:
            self._write_sidecar(sidecar_path, asdict(record))
        except AttachmentError:
            self._safe_remove_path(blob_path)
            raise

        _LOGGER.debug("Stored attachment %s (%s, %d bytes)", attachment_id, media_type, size_bytes)
        return record

    def get(self, attachment_id: str) -> AttachmentRecord:
        """Load one attachment record by id from sidecar metadata."""

        normalized_id = _normalize_attachment_id(attachment_id)
        sidecar_path = self._sidecar_path(normalized_id)
        if not sidecar_path.exists():
            raise AttachmentNotFoundError(f"Attachment not found: {normalized_id}")

        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AttachmentError(f"Cannot read attachment metadata {sidecar_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AttachmentError(f"Invalid attachment metadata JSON: {sidecar_path}") from exc

        if not isinstance(data, dict):
            raise AttachmentError(f"Attachment metadata must be an object: {sidecar_path}")

        record = _record_from_dict(data)
        if record.id.lower() != normalized_id:
            raise AttachmentError(
                f"Attachment metadata id mismatch: expected {normalized_id}, got {record.id}"
            )

        blob_path = self._blob_path(normalized_id)
        if not blob_path.is_file():
            raise AttachmentNotFoundError(f"Attachment blob not found: {normalized_id}")

        return replace(record, id=normalized_id, file_path=str(blob_path))

    def set_transcription(self, attachment_id: str, transcription: str) -> AttachmentRecord:
        """Persist a cached transcription for one attachment and return the record."""

        if not isinstance(transcription, str) or not transcription.strip():
            raise AttachmentError("transcription must be a non-empty string")

        record = self.get(attachment_id)
        updated_record = replace(record, transcription=transcription)
        self._write_sidecar(self._sidecar_path(updated_record.id), asdict(updated_record))
        return updated_record

    def delete(self, attachment_id: str) -> None:
        """Delete one attachment blob and sidecar. Missing files are ignored."""

        normalized_id = _normalize_attachment_id(attachment_id)
        for target_path in (self._blob_path(normalized_id), self._sidecar_path(normalized_id)):
            try:
                target_path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AttachmentError(
                    f"Cannot delete attachment file {target_path}: {exc}"
                ) from exc

    def _blob_path(self, attachment_id: str) -> Path:
        return self._attachments_dir / attachment_id

    def _sidecar_path(self, attachment_id: str) -> Path:
        return self._attachments_dir / f"{attachment_id}.json"

    def _write_blob(self, path: Path, data: bytes) -> None:
        temp_path = _temporary_path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_bytes(data)
            os.replace(temp_path, path)
        except OSError as exc:
            _safe_remove_temporary_file(temp_path)
            raise AttachmentError(f"Cannot write attachment blob {path}: {exc}") from exc

    def _write_sidecar(self, path: Path, payload: JsonObject) -> None:
        temp_path = _temporary_path(path)
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temp_path, path)
        except OSError as exc:
            _safe_remove_temporary_file(temp_path)
            raise AttachmentError(f"Cannot write attachment metadata {path}: {exc}") from exc

    @staticmethod
    def _safe_remove_path(path: Path) -> None:
        with suppress(OSError):
            path.unlink(missing_ok=True)


def sniff_media_type(data: bytes, filename: str) -> str:
    """Detect a file's MIME type from its bytes without storing it.

    Public wrapper over the internal magic-bytes sniffer so callers (notably the
    ``read`` tool) can classify a file as image/audio/video/text before deciding
    whether to promote it to an attachment. Does not touch disk or the allowlist.
    """

    return _sniff_mime(data, filename)


def _sniff_mime(data: bytes, filename: str) -> str:
    """Detect one allowed MIME type using a bounded magic-bytes strategy."""

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF"):
        riff_format = data[8:12]
        if riff_format == b"WEBP":
            return "image/webp"
        if riff_format == b"WAVE":
            return "audio/wav"
        if riff_format == b"AVI ":
            return "video/x-msvideo"
    if data.startswith(b"%PDF"):
        return "application/pdf"

    audio_video_media_type = _sniff_audio_video_media_type(data)
    if audio_video_media_type is not None:
        return audio_video_media_type

    if data.startswith(b"PK\x03\x04"):
        ooxml_media_type = _sniff_ooxml_media_type(data)
        if ooxml_media_type is not None:
            return ooxml_media_type

    legacy_office_media_type = _sniff_legacy_office_media_type(data, filename)
    if legacy_office_media_type is not None:
        return legacy_office_media_type

    if _is_utf8_text(data):
        return "text/plain"

    return "application/octet-stream"


def _sniff_audio_video_media_type(data: bytes) -> str | None:
    if data.startswith(b"OggS"):
        # Ogg can also carry video (Theora), but in practice — especially Telegram
        # voice messages — it is audio (Opus/Vorbis).
        return "audio/ogg"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if data.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        # Raw MP3 frame sync without an ID3 tag.
        return "audio/mpeg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML container (Matroska/WebM). Audio-only WebM exists but is rare for
        # uploaded files; classify as video.
        return "video/webm"
    return None


def _normalize_attachment_id(attachment_id: str) -> str:
    if not isinstance(attachment_id, str) or not _UUID4_RE.match(attachment_id.lower()):
        raise AttachmentNotFoundError(f"Invalid attachment id: {attachment_id}")
    return attachment_id.lower()


def _sniff_ooxml_media_type(data: bytes) -> str | None:
    try:
        with ZipFile(io.BytesIO(data)) as archive, archive.open("[Content_Types].xml") as handle:
            # Bounded read = bounded decompression: ``read(n)`` inflates at most ``n``
            # bytes, so a zip bomb in this entry cannot exhaust memory here.
            content_types_bytes = handle.read(_MAX_OOXML_CONTENT_TYPES_BYTES + 1)
    except (BadZipFile, KeyError, OSError):
        return None

    if len(content_types_bytes) > _MAX_OOXML_CONTENT_TYPES_BYTES:
        # Larger than any legitimate manifest — treat as a decompression bomb, not Office.
        return None
    content_types = content_types_bytes.decode("utf-8", errors="ignore")

    if "wordprocessingml.document" in content_types:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if "spreadsheetml.sheet" in content_types:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if "presentationml.presentation" in content_types:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"

    return None


def _sniff_legacy_office_media_type(data: bytes, filename: str) -> str | None:
    if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return None

    extension = Path(filename).suffix.lower()
    if extension in {".doc", ".dot"}:
        return "application/msword"
    if extension in {".xls", ".xlt", ".xla"}:
        return "application/vnd.ms-excel"
    if extension in {".ppt", ".pps", ".pot"}:
        return "application/vnd.ms-powerpoint"
    return None


def _is_utf8_text(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _extract_text_content(data: bytes, media_type: str) -> str | None:
    if not media_type.startswith("text/"):
        return None
    return data.decode("utf-8")


def _is_allowed_mime(media_type: str) -> bool:
    if media_type.startswith(("text/", "audio/", "video/")):
        return True
    if media_type.startswith(_OOXML_PREFIX):
        return True
    return media_type in _MIME_ALLOWLIST


def _record_from_dict(data: JsonObject) -> AttachmentRecord:
    attachment_id = _require_string(data, "id")
    filename = _require_string(data, "filename")
    media_type = _require_string(data, "media_type")
    stored_at = _require_string(data, "stored_at")
    file_path = _require_string(data, "file_path")

    size_bytes = data.get("size_bytes")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
        raise AttachmentError("Attachment metadata field 'size_bytes' must be an integer")

    text_content = data.get("text_content")
    if text_content is not None and not isinstance(text_content, str):
        raise AttachmentError("Attachment metadata field 'text_content' must be a string or null")

    transcription = data.get("transcription")
    if transcription is not None and not isinstance(transcription, str):
        raise AttachmentError("Attachment metadata field 'transcription' must be a string or null")

    return AttachmentRecord(
        id=attachment_id,
        filename=filename,
        media_type=media_type,
        size_bytes=size_bytes,
        stored_at=stored_at,
        file_path=file_path,
        text_content=text_content,
        transcription=transcription,
    )


def _require_string(data: JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise AttachmentError(f"Attachment metadata field '{key}' must be a string")
    return value


def _temporary_path(target_path: Path) -> Path:
    return target_path.with_name(f"{target_path.name}.{uuid4().hex}.tmp")


def _safe_remove_temporary_file(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


__all__ = [
    "AttachmentError",
    "AttachmentNotFoundError",
    "AttachmentRecord",
    "AttachmentStore",
    "AttachmentTooLargeError",
    "AttachmentTypeNotAllowedError",
    "sniff_media_type",
]
