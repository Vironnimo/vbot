"""Tests for blob-backed attachment storage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.attachments.attachments import (
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

    # Act / Assert
    with pytest.raises(AttachmentNotFoundError, match="Attachment not found"):
        store.get("missing-id")


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
