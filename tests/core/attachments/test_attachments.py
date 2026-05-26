"""Tests for blob-backed attachment storage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.attachments.attachments import (
    AttachmentError,
    AttachmentNotFoundError,
    AttachmentStore,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
)


@pytest.mark.parametrize(
    ("filename", "data", "expected_media_type", "expected_text_content"),
    [
        ("photo.jpg", b"\xff\xd8\xff\x00\x10", "image/jpeg", None),
        ("diagram.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00", "image/png", None),
        ("report.pdf", b"%PDF-1.7\n1 0 obj\n", "application/pdf", None),
        ("notes.txt", b"line one\nline two\n", "text/plain", "line one\nline two\n"),
    ],
)
def test_store_happy_path_persists_blob_and_sidecar(
    tmp_path: Path,
    filename: str,
    data: bytes,
    expected_media_type: str,
    expected_text_content: str | None,
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
    assert record.text_content == expected_text_content

    blob_path = Path(record.file_path)
    assert blob_path.exists()
    assert blob_path.read_bytes() == data

    sidecar_path = tmp_path / "attachments" / f"{record.id}.json"
    assert sidecar_path.exists()

    sidecar_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar_payload["id"] == record.id
    assert sidecar_payload["filename"] == filename
    assert sidecar_payload["media_type"] == expected_media_type
    assert sidecar_payload["size_bytes"] == len(data)
    assert sidecar_payload["text_content"] == expected_text_content

    loaded = store.get(record.id)
    assert loaded == record


def test_store_rejects_file_larger_than_max_size(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path, max_size_bytes=4)

    # Act / Assert
    with pytest.raises(AttachmentTooLargeError, match="exceeds limit"):
        store.store("too-large.txt", b"12345")


def test_store_rejects_blocked_mime_type(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentTypeNotAllowedError, match="Attachment type not allowed"):
        store.store("payload.exe", b"MZ\x90\x00\x03\x00\x00\x00")


def test_get_missing_attachment_raises_not_found(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    missing_attachment_id = "00000000-0000-4000-8000-000000000000"

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError, match="Attachment not found"):
        store.get(missing_attachment_id)


def test_get_rejects_path_traversal_attachment_id(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError, match="Invalid attachment id"):
        store.get("../../etc/passwd")


def test_get_rejects_empty_attachment_id(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError, match="Invalid attachment id"):
        store.get("")


def test_get_rejects_non_uuid_attachment_id(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError, match="Invalid attachment id"):
        store.get("not-a-uuid")


def test_get_uses_canonical_blob_path_when_sidecar_path_is_stale(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"canonical path")
    sidecar_path = tmp_path / "attachments" / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["file_path"] = str(tmp_path / "outside.txt")
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.get(record.id)

    assert loaded.file_path == record.file_path


def test_get_rejects_sidecar_id_mismatch(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"mismatch")
    sidecar_path = tmp_path / "attachments" / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["id"] = "00000000-0000-4000-8000-000000000000"
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AttachmentError, match="metadata id mismatch"):
        store.get(record.id)


def test_get_rejects_missing_blob_with_existing_sidecar(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("notes.txt", b"missing blob")
    Path(record.file_path).unlink()

    with pytest.raises(AttachmentNotFoundError, match="Attachment blob not found"):
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
    sidecar_path = tmp_path / "attachments" / f"{record.id}.json"
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
    sidecar_path = tmp_path / "attachments" / f"{record.id}.json"

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError, match="Invalid attachment id"):
        store.delete("../../etc/passwd")

    assert blob_path.exists()
    assert sidecar_path.exists()


def test_delete_missing_valid_uuid_is_noop(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)

    # Act
    store.delete("00000000-0000-4000-8000-000000000001")

    # Assert
    assert not (tmp_path / "attachments").exists()


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
