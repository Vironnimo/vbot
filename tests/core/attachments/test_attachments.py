"""Tests for blob-backed attachment storage."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.attachments import attachments as attachments_module
from core.attachments.attachments import (
    _MAX_OOXML_CONTENT_TYPES_BYTES,
    AttachmentError,
    AttachmentNotFoundError,
    AttachmentStore,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
    sniff_media_type,
    validate_attachment_metadata_file,
)
from core.storage.layout import DataDirectoryLayout

_OOXML_PREFIX = "application/vnd.openxmlformats-officedocument."
_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _build_ooxml_payload(content_types_xml: bytes) -> bytes:
    """Build a minimal ZIP carrying one ``[Content_Types].xml`` entry."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "data", "expected_media_type"),
    [
        ("photo.jpg", b"\xff\xd8\xff\x00\x10", "image/jpeg"),
        ("diagram.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00", "image/png"),
        ("song.mp3", b"ID3\x04\x00mp3-data", "audio/mpeg"),
        ("clip.wav", b"RIFF\x24\x00\x00\x00WAVEfmt ", "audio/wav"),
        ("movie.mp4", b"\x00\x00\x00\x18ftypisommp4-data", "video/mp4"),
        ("report.pdf", b"%PDF-1.7\n1 0 obj\n", "application/pdf"),
        ("notes.txt", "héllo wörld\n".encode(), "text/plain"),
        ("payload.bin", b"\x00\x01\x02\xff\xfe", "application/octet-stream"),
    ],
)
def test_sniff_media_type_classifies_known_signatures(
    filename: str,
    data: bytes,
    expected_media_type: str,
) -> None:
    assert sniff_media_type(data, filename) == expected_media_type


def test_sniff_media_type_does_not_create_attachments(tmp_path: Path) -> None:
    # Sniffing is a pure byte inspection: it must not write any blob/sidecar.
    sniff_media_type(b"\x89PNG\r\n\x1a\n", "diagram.png")

    assert not DataDirectoryLayout(tmp_path).attachments.exists()


def test_sniff_media_type_classifies_valid_ooxml() -> None:
    # A small, well-formed [Content_Types].xml still classifies as docx — the bomb
    # guard must not break legitimate Office files.
    payload = _build_ooxml_payload(
        b'<?xml version="1.0"?><Types><Override '
        b'ContentType="application/vnd.openxmlformats-officedocument.'
        b'wordprocessingml.document.main+xml"/></Types>'
    )

    assert sniff_media_type(payload, "report.docx") == _DOCX_MEDIA_TYPE


def test_sniff_media_type_rejects_oversized_ooxml_content_types() -> None:
    # A [Content_Types].xml that decompresses past the sniff cap is a zip bomb, not
    # an Office file — even though it carries the docx marker. The bounded read keeps
    # this from exhausting memory, and the type must not be classified as OOXML.
    bomb_xml = b"wordprocessingml.document" + b" " * (_MAX_OOXML_CONTENT_TYPES_BYTES + 1)
    payload = _build_ooxml_payload(bomb_xml)

    media_type = sniff_media_type(payload, "bomb.docx")

    assert not media_type.startswith(_OOXML_PREFIX)
    assert media_type == "application/octet-stream"


@pytest.mark.parametrize("damage", ["encrypted", "unsupported_compression", "invalid_deflate"])
def test_store_rejects_unreadable_ooxml_as_unsupported_type(tmp_path: Path, damage: str) -> None:
    payload = bytearray(_build_ooxml_payload(b"wordprocessingml.document"))
    central_header = payload.index(b"PK\x01\x02")
    if damage == "encrypted":
        payload[6] |= 1
        payload[central_header + 8] |= 1
    elif damage == "unsupported_compression":
        payload[8:10] = (99).to_bytes(2, "little")
        payload[central_header + 10 : central_header + 12] = (99).to_bytes(2, "little")
    else:
        compressed_start = 30 + len(b"[Content_Types].xml")
        payload[compressed_start:central_header] = b"\xff" * (central_header - compressed_start)

    assert sniff_media_type(bytes(payload), "report.docx") == "application/octet-stream"
    with pytest.raises(AttachmentTypeNotAllowedError):
        AttachmentStore(tmp_path).store("report.docx", bytes(payload))
    assert not DataDirectoryLayout(tmp_path).attachments.exists()


@pytest.mark.parametrize(
    ("filename", "data", "expected_media_type", "expected_extension"),
    [
        ("photo.jpg", b"\xff\xd8\xff\x00\x10", "image/jpeg", ".jpg"),
        ("diagram.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00", "image/png", ".png"),
        ("report.pdf", b"%PDF-1.7\n1 0 obj\n", "application/pdf", ".pdf"),
        ("notes.txt", b"line one\nline two\n", "text/plain", ".txt"),
    ],
)
def test_store_happy_path_persists_blob_and_sidecar(
    tmp_path: Path,
    filename: str,
    data: bytes,
    expected_media_type: str,
    expected_extension: str,
) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act
    record = store.store(filename, data)

    # Assert
    assert record.id
    assert record.filename == filename
    assert record.media_type == expected_media_type
    assert record.size_bytes == len(data)

    blob_path = Path(record.file_path)
    assert blob_path.exists()
    assert blob_path.name == f"{record.id}{expected_extension}"
    assert blob_path.read_bytes() == data

    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    assert sidecar_path.exists()

    sidecar_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar_payload["id"] == record.id
    assert sidecar_payload["filename"] == filename
    assert sidecar_payload["media_type"] == expected_media_type
    assert sidecar_payload["size_bytes"] == len(data)
    assert sidecar_payload["format_version"] == 1
    # The blob path follows from the data directory and is never stored.
    assert "file_path" not in sidecar_payload
    assert "text_content" not in sidecar_payload
    assert validate_attachment_metadata_file(sidecar_path).diagnostics == ()

    loaded = store.get(record.id)
    assert loaded == record


@pytest.mark.asyncio
async def test_store_async_keeps_the_event_loop_responsive_during_the_blob_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AttachmentStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    write_blob = attachments_module.atomic_write_bytes

    def slow_write_blob(path: Path, data: bytes) -> None:
        entered.set()
        release.wait(timeout=5)
        write_blob(path, data)

    monkeypatch.setattr(attachments_module, "atomic_write_bytes", slow_write_blob)
    storing = asyncio.create_task(store.store_async("photo.jpg", b"\xff\xd8\xff\x00\x10"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not storing.done()
    finally:
        release.set()
    record = await storing

    assert store.get(record.id).media_type == "image/jpeg"


@pytest.mark.parametrize(
    ("filename", "data", "expected_filename"),
    [
        ("camera-upload", b"\xff\xd8\xff\x00\x10", "camera-upload.jpg"),
        ("voice-message", b"ID3\x04\x00mp3-data", "voice-message.mp3"),
        ("document", b"%PDF-1.7\n1 0 obj\n", "document.pdf"),
        ("notes", b"line one\nline two\n", "notes.txt"),
        ("...", b"\xff\xd8\xff\x00\x10", "attachment.jpg"),
    ],
)
def test_store_adds_canonical_extension_when_filename_has_none(
    tmp_path: Path,
    filename: str,
    data: bytes,
    expected_filename: str,
) -> None:
    store = AttachmentStore(tmp_path)

    record = store.store(filename, data)

    assert record.filename == expected_filename
    assert Path(record.file_path).suffix == Path(expected_filename).suffix


def test_store_preserves_existing_filename_extension(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)

    record = store.store("original.jpeg", b"\xff\xd8\xff\x00\x10")

    assert record.filename == "original.jpeg"
    assert Path(record.file_path).suffix == ".jpg"


@pytest.mark.parametrize(
    ("filename", "data", "expected_media_type"),
    [
        ("voice.ogg", b"OggS\x00\x02opus-data", "audio/ogg"),
        ("song.mp3", b"ID3\x04\x00mp3-data", "audio/mpeg"),
        ("raw.mp3", b"\xff\xfbmp3-frame-data", "audio/mpeg"),
        ("clip.wav", b"RIFF\x24\x00\x00\x00WAVEfmt ", "audio/wav"),
        ("track.flac", b"fLaC\x00\x00\x00\x22flac-data", "audio/flac"),
        ("audio.m4a", b"\x00\x00\x00\x18ftypM4A m4a-data", "audio/mp4"),
        ("movie.mp4", b"\x00\x00\x00\x18ftypisommp4-data", "video/mp4"),
        ("movie.mov", b"\x00\x00\x00\x14ftypqt  mov-data", "video/quicktime"),
        ("clip.webm", b"\x1a\x45\xdf\xa3webm-data", "video/webm"),
        ("old.avi", b"RIFF\x24\x00\x00\x00AVI avi-data", "video/x-msvideo"),
    ],
)
def test_store_accepts_audio_and_video_files(
    tmp_path: Path,
    filename: str,
    data: bytes,
    expected_media_type: str,
) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act
    record = store.store(filename, data)

    # Assert
    assert record.media_type == expected_media_type
    assert record.transcription is None


def test_set_transcription_persists_to_sidecar(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", b"OggS\x00\x02opus-data")

    # Act
    updated = store.set_transcription(record.id, "hello world")

    # Assert
    assert updated.transcription == "hello world"
    assert store.get(record.id).transcription == "hello world"

    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    sidecar_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar_payload["transcription"] == "hello world"


def test_set_transcription_keeps_the_unknown_fields_of_the_sidecar(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", b"OggS\x00\x02opus-data")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["duration_ms"] = 1200
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    store.set_transcription(record.id, "hello world")

    stored = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert stored == {**payload, "transcription": "hello world"}
    report = validate_attachment_metadata_file(sidecar_path)
    assert [(item.severity, item.path) for item in report.diagnostics] == [
        ("warning", "$.duration_ms")
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": 2},
        {"format_version": None},
        {"size_bytes": -1},
        {"media_type": "application/x-unknown"},
        {"media_type": []},
        {"media_type": {}},
    ],
    ids=[
        "newer-version",
        "no-version",
        "negative-size",
        "unstored-type",
        "array-media-type",
        "object-media-type",
    ],
)
def test_sidecar_that_fails_to_load_is_unavailable_and_never_rewritten(
    tmp_path: Path, change: dict[str, object]
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", b"OggS\x00\x02opus-data")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    payload = {**json.loads(sidecar_path.read_text(encoding="utf-8")), **change}
    payload = {key: value for key, value in payload.items() if value is not None}
    original = json.dumps(payload)
    sidecar_path.write_text(original, encoding="utf-8")

    with pytest.raises(AttachmentError):
        store.get(record.id)
    with pytest.raises(AttachmentError):
        store.set_transcription(record.id, "hello world")

    assert sidecar_path.read_text(encoding="utf-8") == original
    report = validate_attachment_metadata_file(sidecar_path)
    assert not report.ok
    if "media_type" in change:
        assert [(item.severity, item.path) for item in report.diagnostics] == [
            ("error", "$.media_type")
        ]


def test_set_transcription_rejects_empty_text(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", b"OggS\x00\x02opus-data")

    # Act / Assert
    with pytest.raises(AttachmentError):
        store.set_transcription(record.id, "   ")


def test_store_rejects_file_larger_than_max_size(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path, max_size_bytes=4)

    # Act / Assert
    with pytest.raises(AttachmentTooLargeError):
        store.store("too-large.txt", b"12345")


def test_ensure_within_limit_rejects_oversized_reported_size(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path, max_size_bytes=4)

    # Act / Assert
    with pytest.raises(AttachmentTooLargeError):
        store.ensure_within_limit(5)


def test_ensure_within_limit_allows_size_at_limit(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path, max_size_bytes=4)

    # Act / Assert — at-or-below the limit and an unknown (None) size both pass.
    store.ensure_within_limit(4)
    store.ensure_within_limit(None)


def test_store_rejects_blocked_mime_type(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentTypeNotAllowedError):
        store.store("payload.exe", b"MZ\x90\x00\x03\x00\x00\x00")


def test_get_missing_attachment_raises_not_found(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    missing_attachment_id = "00000000-0000-4000-8000-000000000000"

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError):
        store.get(missing_attachment_id)


def test_get_rejects_path_traversal_attachment_id(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError):
        store.get("../../etc/passwd")


def test_get_rejects_empty_attachment_id(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError):
        store.get("")


def test_get_rejects_non_uuid_attachment_id(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError):
        store.get("not-a-uuid")


def test_get_uses_canonical_blob_path_when_sidecar_path_is_stale(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"canonical path")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["file_path"] = str(tmp_path / "outside.txt")
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.get(record.id)

    assert loaded.file_path == record.file_path


def test_get_rejects_sidecar_id_mismatch(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"mismatch")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["id"] = "00000000-0000-4000-8000-000000000000"
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AttachmentError):
        store.get(record.id)


def test_get_rejects_non_utf8_sidecar_as_attachment_error(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"notes")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    sidecar_path.write_bytes(b"\xff\xfe")

    with pytest.raises(AttachmentError):
        store.get(record.id)


def test_get_rejects_missing_blob_with_existing_sidecar(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"missing blob")
    Path(record.file_path).unlink()

    with pytest.raises(AttachmentNotFoundError):
        store.get(record.id)


def test_get_accepts_uppercase_attachment_id(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"uppercase")

    loaded = store.get(record.id.upper())

    assert loaded == record


def test_delete_removes_blob_and_sidecar_and_missing_is_noop(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"to delete")
    blob_path = Path(record.file_path)
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    assert blob_path.exists()
    assert sidecar_path.exists()

    # Act
    store.delete(record.id)
    store.delete(record.id)

    # Assert
    assert not blob_path.exists()
    assert not sidecar_path.exists()


def test_delete_rejects_path_traversal_id_without_removing_existing_files(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"keep me")
    blob_path = Path(record.file_path)
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError):
        store.delete("../../etc/passwd")

    assert blob_path.exists()
    assert sidecar_path.exists()


def test_delete_missing_valid_uuid_is_noop(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act
    store.delete("00000000-0000-4000-8000-000000000001")

    # Assert
    assert not DataDirectoryLayout(tmp_path).attachments.exists()


def test_stored_at_uses_utc_iso_format_with_explicit_offset(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act
    record = store.store("notes.txt", b"timestamp check")
    parsed = datetime.fromisoformat(record.stored_at)

    # Assert
    assert record.stored_at.endswith("+00:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_short_attachment_ids_reserve_sidecars_across_extensions(tmp_path, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    store = AttachmentStore(tmp_path)
    first = store.store("first.txt", b"first")
    second = store.store("second.pdf", b"%PDF-1.7 second")
    assert first.id == "att_000000000001"
    assert second.id == "att_000000000002"
    assert Path(store.get(first.id).file_path).read_bytes() == b"first"
    assert Path(store.get(second.id).file_path).read_bytes() == b"%PDF-1.7 second"


@pytest.mark.parametrize(
    "data",
    [
        b"ID3 tags are read by the player.\n",
        b"ID3v2 notes\n",
        b"OggS container notes\n",
        b"fLaC is the FLAC magic.\n",
        b"GIF8 is how a GIF starts.\n",
        b"GIF89a version notes\n",
    ],
)
def test_text_starting_with_a_media_magic_word_is_text(data: bytes) -> None:
    assert sniff_media_type(data, "notes.md") == "text/plain"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"ID3\x03\x00\x00\x00\x00\x02\x01rest", "audio/mpeg"),
        (b"OggS\x00\x02" + b"\x00" * 21, "audio/ogg"),
        (b"fLaC\x80\x00\x00\x22" + b"\x00" * 34, "audio/flac"),
        (b"GIF87a\x10\x00\x10\x00\x00\x00\x00\x3b", "image/gif"),
        # Global colour tables (flag 0x80) of 2 and 256 entries precede the first block.
        (b"GIF89a\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 6 + b"\x2c", "image/gif"),
        (b"GIF89a\x01\x00\x01\x00\x87\x00\x00" + b"\xff" * 768 + b"\x21", "image/gif"),
    ],
)
def test_media_magic_words_with_real_headers_keep_their_type(data: bytes, expected: str) -> None:
    assert sniff_media_type(data, "file.bin") == expected


@pytest.mark.parametrize(
    "data",
    [
        # Header only: the first block is missing.
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00",
        # No colour table, and the byte after the header is no block marker.
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00\x00",
        # A marker inside the declared colour table does not count.
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x2c",
        # Colour table cut short before the first block.
        b"GIF89a\x01\x00\x01\x00\x87\x00\x00" + b"\xff" * 767,
    ],
)
def test_gif_signature_without_a_first_block_is_not_gif(data: bytes) -> None:
    assert sniff_media_type(data, "file.gif") != "image/gif"


@pytest.mark.parametrize("data", [b"BMW is a car maker\n", b"BM25 ranking notes\n"])
def test_bm_prefixed_text_is_stored_as_text(tmp_path: Path, data: bytes) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", data)
    assert record.media_type == "text/plain"
    assert Path(record.file_path).suffix == ".txt"
    assert Path(record.file_path).read_bytes() == data
    assert store.get(record.id).media_type == "text/plain"


def test_bmp_sniffing_requires_plausible_headers() -> None:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="BMP")
    data = output.getvalue()
    assert sniff_media_type(data, "arbitrary.txt") == "image/bmp"
    assert sniff_media_type(b"BM" + b"\x00" * 20, "x.bmp") != "image/bmp"
    for offset, replacement in [
        (6, b"BAD!"),
        (10, (9999).to_bytes(4, "little")),
        (14, (9999).to_bytes(4, "little")),
    ]:
        malformed = data[:offset] + replacement + data[offset + 4 :]
        assert sniff_media_type(malformed, "x.bmp") != "image/bmp"
    assert sniff_media_type(data[:54], "x.bmp") != "image/bmp"
