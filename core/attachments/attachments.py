"""Blob-backed attachment storage with sidecar metadata."""

from __future__ import annotations

import io
import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass
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


class AttachmentStore:
    """Store and fetch attachment blobs under ``<data_dir>/attachments``."""

    def __init__(self, data_dir: Path, *, max_size_bytes: int = 20_971_520) -> None:
        if max_size_bytes <= 0:
            raise AttachmentError("max_size_bytes must be greater than 0")

        self._attachments_dir = Path(data_dir).expanduser() / "attachments"
        self._max_size_bytes = max_size_bytes

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

        sidecar_path = self._sidecar_path(attachment_id)
        if not sidecar_path.exists():
            raise AttachmentNotFoundError(f"Attachment not found: {attachment_id}")

        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AttachmentError(f"Cannot read attachment metadata {sidecar_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AttachmentError(f"Invalid attachment metadata JSON: {sidecar_path}") from exc

        if not isinstance(data, dict):
            raise AttachmentError(f"Attachment metadata must be an object: {sidecar_path}")

        return _record_from_dict(data)

    def delete(self, attachment_id: str) -> None:
        """Delete one attachment blob and sidecar. Missing files are ignored."""

        for target_path in (self._blob_path(attachment_id), self._sidecar_path(attachment_id)):
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


def _sniff_mime(data: bytes, filename: str) -> str:
    """Detect one allowed MIME type using a bounded magic-bytes strategy."""

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF"):
        return "application/pdf"

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


def _sniff_ooxml_media_type(data: bytes) -> str | None:
    try:
        with ZipFile(io.BytesIO(data)) as archive, archive.open("[Content_Types].xml") as handle:
            content_types = handle.read().decode("utf-8", errors="ignore")
    except (BadZipFile, KeyError, OSError):
        return None

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
    if media_type.startswith("text/"):
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

    return AttachmentRecord(
        id=attachment_id,
        filename=filename,
        media_type=media_type,
        size_bytes=size_bytes,
        stored_at=stored_at,
        file_path=file_path,
        text_content=text_content,
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
]
