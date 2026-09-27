"""Tests for blob-backed attachment storage."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from collections.abc import Callable
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
    # A fixed timestamp keeps the bytes, and the test ids derived from them, identical
    # across collections; xdist workers collect independently and must agree.
    entry = zipfile.ZipInfo("[Content_Types].xml", date_time=(2020, 1, 1, 0, 0, 0))
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(entry, content_types_xml, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


_DOCX_CONTENT_TYPES = (
    b'<?xml version="1.0"?><Types><Override '
    b'ContentType="application/vnd.openxmlformats-officedocument.'
    b'wordprocessingml.document.main+xml"/></Types>'
)


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
        # Text that merely starts with a media magic word stays text.
        ("notes.md", b"ID3 tags are read by the player.\n", "text/plain"),
        ("notes.md", b"ID3v2 notes\n", "text/plain"),
        ("notes.md", b"OggS container notes\n", "text/plain"),
        ("notes.md", b"fLaC is the FLAC magic.\n", "text/plain"),
        ("notes.md", b"GIF8 is how a GIF starts.\n", "text/plain"),
        ("notes.md", b"GIF89a version notes\n", "text/plain"),
        # The same magic words with plausible headers keep their media type.
        ("file.bin", b"ID3\x03\x00\x00\x00\x00\x02\x01rest", "audio/mpeg"),
        ("file.bin", b"OggS\x00\x02" + b"\x00" * 21, "audio/ogg"),
        ("file.bin", b"fLaC\x80\x00\x00\x22" + b"\x00" * 34, "audio/flac"),
        ("file.bin", b"GIF87a\x10\x00\x10\x00\x00\x00\x00\x3b", "image/gif"),
        # Global colour tables (flag 0x80) of 2 and 256 entries precede the first block.
        ("file.bin", b"GIF89a\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 6 + b"\x2c", "image/gif"),
        ("file.bin", b"GIF89a\x01\x00\x01\x00\x87\x00\x00" + b"\xff" * 768 + b"\x21", "image/gif"),
        # A small, well-formed [Content_Types].xml classifies as docx. One that
        # decompresses past the sniff cap is a zip bomb, not an Office file, even
        # though it carries the docx marker; the bounded read keeps memory flat.
        ("report.docx", _build_ooxml_payload(_DOCX_CONTENT_TYPES), _DOCX_MEDIA_TYPE),
        (
            "bomb.docx",
            _build_ooxml_payload(
                b"wordprocessingml.document" + b" " * (_MAX_OOXML_CONTENT_TYPES_BYTES + 1)
            ),
            "application/octet-stream",
        ),
    ],
)
def test_sniff_media_type_classifies_by_content(
    filename: str,
    data: bytes,
    expected_media_type: str,
) -> None:
    assert sniff_media_type(data, filename) == expected_media_type


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
    assert record.stored_at.endswith("+00:00")
    assert datetime.fromisoformat(record.stored_at).utcoffset() == timedelta(0)

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
    ("filename", "data", "expected_filename", "blob_suffix"),
    [
        ("camera-upload", b"\xff\xd8\xff\x00\x10", "camera-upload.jpg", ".jpg"),
        ("voice-message", b"ID3\x04\x00mp3-data", "voice-message.mp3", ".mp3"),
        ("document", b"%PDF-1.7\n1 0 obj\n", "document.pdf", ".pdf"),
        ("notes", b"line one\nline two\n", "notes.txt", ".txt"),
        ("...", b"\xff\xd8\xff\x00\x10", "attachment.jpg", ".jpg"),
        # An existing extension is kept; the blob still uses the canonical one.
        ("original.jpeg", b"\xff\xd8\xff\x00\x10", "original.jpeg", ".jpg"),
    ],
)
def test_store_names_the_attachment_and_its_blob(
    tmp_path: Path,
    filename: str,
    data: bytes,
    expected_filename: str,
    blob_suffix: str,
) -> None:
    store = AttachmentStore(tmp_path)

    record = store.store(filename, data)

    assert record.filename == expected_filename
    assert Path(record.file_path).suffix == blob_suffix


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


def test_set_transcription_persists_and_keeps_unknown_sidecar_fields(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", b"OggS\x00\x02opus-data")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["duration_ms"] = 1200
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    updated = store.set_transcription(record.id, "hello world")

    assert updated.transcription == "hello world"
    assert store.get(record.id).transcription == "hello world"
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


def test_size_limit_applies_to_stored_bytes_and_reported_sizes(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_size_bytes=4)

    with pytest.raises(AttachmentTooLargeError):
        store.store("too-large.txt", b"12345")
    with pytest.raises(AttachmentTooLargeError):
        store.ensure_within_limit(5)
    # At the limit and an unknown (None) size both pass.
    store.ensure_within_limit(4)
    store.ensure_within_limit(None)
    assert not DataDirectoryLayout(tmp_path).attachments.exists()


def test_store_rejects_blocked_mime_type(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentTypeNotAllowedError):
        store.store("payload.exe", b"MZ\x90\x00\x03\x00\x00\x00")


@pytest.mark.parametrize(
    "attachment_id",
    ["00000000-0000-4000-8000-000000000000", "../../etc/passwd", "", "not-a-uuid"],
    ids=["missing", "path-traversal", "empty", "not-a-uuid"],
)
def test_get_unknown_or_malformed_id_is_not_found(tmp_path: Path, attachment_id: str) -> None:
    store = AttachmentStore(tmp_path)

    with pytest.raises(AttachmentNotFoundError):
        store.get(attachment_id)


def test_get_uses_canonical_blob_path_when_sidecar_path_is_stale(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"canonical path")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["file_path"] = str(tmp_path / "outside.txt")
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.get(record.id)

    assert loaded.file_path == record.file_path


def _other_id(sidecar: bytes) -> bytes:
    payload = json.loads(sidecar)
    payload["id"] = "00000000-0000-4000-8000-000000000000"
    return json.dumps(payload).encode()


def _not_utf8(_sidecar: bytes) -> bytes:
    return b"\xff\xfe"


@pytest.mark.parametrize("corrupt", [_other_id, _not_utf8], ids=["id-mismatch", "not-utf8"])
def test_get_refuses_a_corrupt_sidecar(tmp_path: Path, corrupt: Callable[[bytes], bytes]) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"notes")
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    sidecar_path.write_bytes(corrupt(sidecar_path.read_bytes()))

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
    store.delete("00000000-0000-4000-8000-000000000001")
    assert not DataDirectoryLayout(tmp_path).attachments.exists()
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
