"""Blob-backed attachment storage with sidecar metadata."""

from __future__ import annotations

import io
import lzma
import zlib
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
    validate_non_empty_string,
    validate_optional_string,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    JsonDocumentWriteError,
    json_document,
    render_json_document,
    strip_unknown_fields,
    validate_format_version,
    write_json_document,
)
from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_bytes, atomic_write_text
from core.utils.errors import VBotError
from core.utils.ids import is_safe_id, new_id
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

JsonObject = dict[str, Any]

# Storing sniffs the content, scans the attachment directory for an id claim and
# fsyncs a blob of up to the size limit: never on the Event Loop.
_STORE_WORKERS = BoundedWorkerPool(name="attachments", max_workers=4)

_OOXML_PREFIX = "application/vnd.openxmlformats-officedocument."
_OOXML_WILDCARD = "application/vnd.openxmlformats-officedocument.*"
# An OOXML file's ``[Content_Types].xml`` is a small manifest — a few KiB even for
# large documents. Reading it unbounded lets a crafted ZIP entry decompress to
# gigabytes from a within-upload-limit file (a zip bomb), so the sniff decompresses
# at most this many bytes and treats any overflow as "not OOXML".
_MAX_OOXML_CONTENT_TYPES_BYTES = 1_048_576
_MIME_ALLOWLIST = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/avif",
        "image/heic",
        "image/heif",
        "application/pdf",
        _OOXML_WILDCARD,
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)
_CANONICAL_EXTENSION_BY_MEDIA_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
}
_CANONICAL_BLOB_EXTENSIONS = frozenset(_CANONICAL_EXTENSION_BY_MEDIA_TYPE.values())

ATTACHMENT_METADATA_FORMAT_VERSION = 1
# The blob path is not stored: it follows from the data directory, id and type.
ATTACHMENT_METADATA_SHAPE = json_document(
    {"id", "filename", "media_type", "size_bytes", "stored_at", "transcription"}
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


def validate_attachment_metadata_data(data: Any) -> list[JsonDiagnostic]:
    """Validate one decoded attachment sidecar (``artifacts/attachments/<id>.json``)."""

    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, ATTACHMENT_METADATA_FORMAT_VERSION):
        return diagnostics
    warn_unknown_keys(
        diagnostics, "$", data, ATTACHMENT_METADATA_SHAPE.fields, "attachment metadata field"
    )
    for name in ("id", "filename", "stored_at"):
        validate_non_empty_string(diagnostics, f"$.{name}", data.get(name), required=True)
    media_type = data.get("media_type")
    if not isinstance(media_type, str) or media_type not in _CANONICAL_EXTENSION_BY_MEDIA_TYPE:
        add_error(diagnostics, "$.media_type", "must be a media type vBot stores attachments as")
    size_bytes = data.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        add_error(diagnostics, "$.size_bytes", "must be a non-negative integer")
    validate_optional_string(diagnostics, "$.transcription", data.get("transcription"))
    return diagnostics


def validate_attachment_metadata_file(path: str | Path) -> JsonValidationReport:
    """Validate one attachment sidecar without reading its blob."""

    return validate_json_file(path, validate_attachment_metadata_data, missing_ok=False)


# Written only to cache a transcription; a sidecar that fails to load is left unchanged.
ATTACHMENT_METADATA_FORMAT = JsonDocumentFormat(
    name="Attachment metadata",
    version=ATTACHMENT_METADATA_FORMAT_VERSION,
    shape=ATTACHMENT_METADATA_SHAPE,
    validate=validate_attachment_metadata_data,
    sort_keys=True,
)


@dataclass(frozen=True)
class AttachmentRecord:
    """Persisted metadata for one attachment blob."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    stored_at: str
    # The blob's current location, derived on load and never stored in the sidecar.
    file_path: str
    # Cached speech-to-text result for audio attachments; written once on first
    # transcription so later requests reuse it instead of re-calling STT.
    transcription: str | None = None


class AttachmentStore:
    """Store and fetch attachment blobs under the canonical artifact path."""

    def __init__(self, data_dir: Path, *, max_size_bytes: int = 20_971_520) -> None:
        if max_size_bytes <= 0:
            raise AttachmentError("max_size_bytes must be greater than 0")

        self._attachments_dir = DataDirectoryLayout(data_dir).attachments
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
        canonical_extension = canonical_extension_for_media_type(media_type)
        stored_filename = _filename_with_extension(filename, canonical_extension)

        stored_at = datetime.now(UTC).isoformat()
        self._attachments_dir.mkdir(parents=True, exist_ok=True)

        def claim(candidate: str) -> bool:
            # Reserve the shared sidecar name, independent of blob extension.
            try:
                with self._sidecar_path(candidate).open("x", encoding="utf-8"):
                    pass
            except FileExistsError:
                return False
            if any(
                path != self._sidecar_path(candidate)
                for path in self._attachments_dir.glob(f"{candidate}.*")
            ):
                self._sidecar_path(candidate).unlink()
                return False
            return True

        try:
            attachment_id = new_id("att", claim=claim)
        except OSError as exc:
            raise AttachmentError(str(exc)) from exc

        blob_path = self._blob_path(attachment_id, media_type)
        sidecar_path = self._sidecar_path(attachment_id)
        record = AttachmentRecord(
            id=attachment_id,
            filename=stored_filename,
            media_type=media_type,
            size_bytes=size_bytes,
            stored_at=stored_at,
            file_path=str(blob_path),
        )

        try:
            self._write_blob(blob_path, data)
            self._write_new_sidecar(sidecar_path, record)
        except AttachmentError:
            self._safe_remove_path(blob_path)
            self._safe_remove_path(sidecar_path)
            raise

        _LOGGER.debug("Stored attachment %s (%s, %d bytes)", attachment_id, media_type, size_bytes)
        return record

    async def store_async(self, filename: str, data: bytes) -> AttachmentRecord:
        """Event-Loop-safe :meth:`store`, run on the ``attachments`` worker pool."""

        return await _STORE_WORKERS.run(self.store, filename, data)

    def get(self, attachment_id: str) -> AttachmentRecord:
        """Load one attachment record by id from sidecar metadata."""

        normalized_id = _normalize_attachment_id(attachment_id)
        sidecar_path = self._sidecar_path(normalized_id)
        if not sidecar_path.exists():
            raise AttachmentNotFoundError(f"Attachment not found: {normalized_id}")

        try:
            data = load_validated_json_file(
                sidecar_path, validate_attachment_metadata_data, missing_ok=False
            )
        except OSError as exc:
            raise AttachmentError(f"Cannot read attachment metadata {sidecar_path}: {exc}") from exc
        except JsonConfigValidationError as exc:
            raise AttachmentError(f"Invalid attachment metadata: {exc}") from exc

        metadata = strip_unknown_fields(data, ATTACHMENT_METADATA_SHAPE)
        if metadata["id"].lower() != normalized_id:
            raise AttachmentError(
                f"Attachment metadata id mismatch: expected {normalized_id}, got {metadata['id']}"
            )

        blob_path = self._blob_path(normalized_id, metadata["media_type"])
        if not blob_path.is_file():
            raise AttachmentNotFoundError(f"Attachment blob not found: {normalized_id}")

        return AttachmentRecord(
            id=normalized_id,
            filename=metadata["filename"],
            media_type=metadata["media_type"],
            size_bytes=metadata["size_bytes"],
            stored_at=metadata["stored_at"],
            file_path=str(blob_path),
            transcription=metadata.get("transcription"),
        )

    def set_transcription(self, attachment_id: str, transcription: str) -> AttachmentRecord:
        """Persist a cached transcription for one attachment and return the record."""

        if not isinstance(transcription, str) or not transcription.strip():
            raise AttachmentError("transcription must be a non-empty string")

        record = self.get(attachment_id)
        updated_record = replace(record, transcription=transcription)
        sidecar_path = self._sidecar_path(updated_record.id)
        try:
            write_json_document(
                sidecar_path, _metadata_body(updated_record), ATTACHMENT_METADATA_FORMAT
            )
        except (OSError, JsonDocumentWriteError) as exc:
            raise AttachmentError(
                f"Cannot write attachment metadata {sidecar_path}: {exc}"
            ) from exc
        return updated_record

    def delete(self, attachment_id: str) -> None:
        """Delete one attachment blob and sidecar. Missing files are ignored."""

        normalized_id = _normalize_attachment_id(attachment_id)
        blob_paths = (
            self._attachments_dir / f"{normalized_id}{extension}"
            for extension in _CANONICAL_BLOB_EXTENSIONS
        )
        for target_path in (*blob_paths, self._sidecar_path(normalized_id)):
            try:
                target_path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AttachmentError(
                    f"Cannot delete attachment file {target_path}: {exc}"
                ) from exc

    def _blob_path(self, attachment_id: str, media_type: str) -> Path:
        extension = canonical_extension_for_media_type(media_type)
        return self._attachments_dir / f"{attachment_id}{extension}"

    def _sidecar_path(self, attachment_id: str) -> Path:
        return self._attachments_dir / f"{attachment_id}.json"

    def _write_blob(self, path: Path, data: bytes) -> None:
        try:
            atomic_write_bytes(path, data)
        except OSError as exc:
            raise AttachmentError(f"Cannot write attachment blob {path}: {exc}") from exc

    def _write_new_sidecar(self, path: Path, record: AttachmentRecord) -> None:
        # Replaces the empty reservation of a new attachment: there is nothing to keep.
        serialized = render_json_document(
            _metadata_body(record), version=ATTACHMENT_METADATA_FORMAT_VERSION, sort_keys=True
        )
        try:
            atomic_write_text(path, serialized)
        except OSError as exc:
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

    if data.startswith(b"BM") and len(data) >= 26 and data[6:10] == b"\x00" * 4:
        header_size = int.from_bytes(data[14:18], "little")
        pixel_offset = int.from_bytes(data[10:14], "little")
        if header_size in {12, 16, 40, 52, 56, 64, 108, 124} and (
            14 + header_size <= pixel_offset < len(data)
        ):
            return "image/bmp"
    if data.startswith((b"II\x2a\x00", b"MM\x00\x2a", b"II\x2b\x00", b"MM\x00\x2b")):
        return "image/tiff"
    if len(data) >= 16 and data[4:8] == b"ftyp":
        # ISO-BMFF images must be recognized before the generic MP4 fallback.
        # Inspect only complete brand words in the bounded leading ftyp box.
        box_end = min(int.from_bytes(data[:4], "big"), len(data), 4096)
        brands = [data[8:12], *[data[i : i + 4] for i in range(16, box_end - 3, 4)]]
        if any(brand in {b"avif", b"avis"} for brand in brands):
            return "image/avif"
        if any(
            brand in {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
            for brand in brands
        ):
            return "image/heic"
        if any(brand in {b"mif1", b"msf1"} for brand in brands):
            return "image/heif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a") and _has_gif_first_block(data):
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


def _has_gif_first_block(data: bytes) -> bool:
    """Require the block that must follow a GIF's screen descriptor.

    Text such as ``GIF89a version notes`` carries the signature too. A real GIF
    continues after the 13-byte header and its optional global colour table with an
    extension (``0x21``), an image descriptor (``0x2C``) or the trailer (``0x3B``).
    The marker sits within the first 782 bytes, so bounded file probes and whole
    uploads classify the same bytes identically; shorter data is not a GIF.
    """

    if len(data) < 13:
        return False
    flags = data[10]
    offset = 13
    if flags & 0x80:
        offset += 3 * 2 ** ((flags & 0x07) + 1)
    return len(data) > offset and data[offset] in (0x21, 0x2C, 0x3B)


def _sniff_audio_video_media_type(data: bytes) -> str | None:
    # ASCII magic words alone also start ordinary text ("ID3 tags", "OggS notes"),
    # so each requires the binary header fields that must follow it.
    if data.startswith(b"OggS") and len(data) >= 6 and data[4] == 0 and data[5] <= 0x07:
        # Ogg can also carry video (Theora), but in practice — especially Telegram
        # voice messages — it is audio (Opus/Vorbis).
        return "audio/ogg"
    if (
        data.startswith(b"ID3")
        and len(data) >= 5
        and data[3] in (2, 3, 4)
        and data[4] != 0xFF
        and all(byte < 0x80 for byte in data[6:10])
    ):
        return "audio/mpeg"
    if data.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        # Raw MP3 frame sync without an ID3 tag.
        return "audio/mpeg"
    if (
        data.startswith(b"fLaC")
        and data[4:5] in (b"\x00", b"\x80")
        and data[5:8] == b"\x00\x00\x22"
    ):
        # The first metadata block is always a 34-byte STREAMINFO block.
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
    if not isinstance(attachment_id, str) or not is_safe_id(attachment_id.lower()):
        raise AttachmentNotFoundError(f"Invalid attachment id: {attachment_id}")
    return attachment_id.lower()


def canonical_extension_for_media_type(media_type: str) -> str:
    """Return the stable storage extension for one sniffed attachment media type."""

    extension = _CANONICAL_EXTENSION_BY_MEDIA_TYPE.get(media_type)
    if extension is None:
        raise AttachmentError(f"No canonical filename extension for media type: {media_type}")
    return extension


def _filename_with_extension(filename: str, canonical_extension: str) -> str:
    if not filename.strip():
        return f"attachment{canonical_extension}"
    if Path(filename).suffix:
        return filename
    extensionless_name = filename.rstrip(". ")
    if not extensionless_name:
        extensionless_name = "attachment"
    return f"{extensionless_name}{canonical_extension}"


def _sniff_ooxml_media_type(data: bytes) -> str | None:
    try:
        with ZipFile(io.BytesIO(data)) as archive, archive.open("[Content_Types].xml") as handle:
            # Bounded read = bounded decompression: ``read(n)`` inflates at most ``n``
            # bytes, so a zip bomb in this entry cannot exhaust memory here.
            content_types_bytes = handle.read(_MAX_OOXML_CONTENT_TYPES_BYTES + 1)
    except (BadZipFile, KeyError, OSError, EOFError, RuntimeError, zlib.error, lzma.LZMAError):
        # Encrypted entries, unsupported compression and invalid compressed data
        # are unrecognizable input, not failures of the attachment service.
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


def _is_allowed_mime(media_type: str) -> bool:
    if media_type.startswith(("text/", "audio/", "video/")):
        return True
    if media_type.startswith(_OOXML_PREFIX):
        return True
    return media_type in _MIME_ALLOWLIST


def _metadata_body(record: AttachmentRecord) -> JsonObject:
    """The sidecar fields of one record; the blob path is derived, never stored."""
    body: JsonObject = {
        "id": record.id,
        "filename": record.filename,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "stored_at": record.stored_at,
    }
    if record.transcription is not None:
        body["transcription"] = record.transcription
    return body


__all__ = [
    "AttachmentError",
    "AttachmentNotFoundError",
    "AttachmentRecord",
    "AttachmentStore",
    "AttachmentTooLargeError",
    "AttachmentTypeNotAllowedError",
    "canonical_extension_for_media_type",
    "sniff_media_type",
    "validate_attachment_metadata_file",
]
