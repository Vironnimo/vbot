"""Tests for attachment upload and download endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.attachments import AttachmentStore
from core.runs import ChatRunManager
from server.app import create_app

MAX_ATTACHMENT_SIZE_BYTES = 20_971_520


class _AttachmentRuntime:
    def __init__(self, data_dir: Path) -> None:
        self.storage = type("Storage", (), {"data_dir": data_dir})()
        self.attachment_store = AttachmentStore(
            data_dir,
            max_size_bytes=MAX_ATTACHMENT_SIZE_BYTES,
        )
        self.chat_runs = ChatRunManager()

    def start(self) -> None:
        self.storage.data_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        return None


class _RejectingAttachmentStore(AttachmentStore):
    def store(self, filename: str, data: bytes) -> Any:
        raise AssertionError("attachment store should not receive oversize uploads")


def test_upload_valid_jpeg_returns_attachment_metadata(tmp_path: Path) -> None:
    payload = _jpeg_payload()

    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("photo.jpg", payload, "image/jpeg")},
        )

    body = response.json()
    assert response.status_code == 200
    assert isinstance(body["attachment_id"], str)
    assert body["attachment_id"]
    assert body["filename"] == "photo.jpg"
    assert body["media_type"] == "image/jpeg"
    assert body["size_bytes"] == len(payload)
    assert body["text_content"] is None


def test_upload_text_file_returns_embedded_text_content(tmp_path: Path) -> None:
    payload = b"hello from text file\nsecond line"

    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("note.txt", payload, "text/plain")},
        )

    body = response.json()
    assert response.status_code == 200
    assert isinstance(body["attachment_id"], str)
    assert body["attachment_id"]
    assert body["filename"] == "note.txt"
    assert body["media_type"].startswith("text/")
    assert body["size_bytes"] == len(payload)
    assert body["text_content"] == "hello from text file\nsecond line"


def test_upload_rejects_payload_over_20_mib_limit(tmp_path: Path) -> None:
    payload = b"a" * (MAX_ATTACHMENT_SIZE_BYTES + 1)

    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("too-large.bin", payload, "application/octet-stream")},
        )

    assert response.status_code == 413


def test_upload_rejects_payload_before_attachment_store_call(tmp_path: Path) -> None:
    runtime = _AttachmentRuntime(tmp_path / "data")
    runtime.attachment_store = _RejectingAttachmentStore(tmp_path / "data", max_size_bytes=3)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("too-large.txt", b"abcd", "text/plain")},
        )

    assert response.status_code == 413


def test_upload_rejects_blocked_mime_type(tmp_path: Path) -> None:
    payload = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00"

    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("payload.exe", payload, "application/octet-stream")},
        )

    assert response.status_code == 415


def test_get_attachment_streams_existing_blob_with_media_type(tmp_path: Path) -> None:
    payload = _jpeg_payload()

    with _create_client(tmp_path) as client:
        upload_response = client.post(
            "/api/upload",
            files={"file": ("photo.jpg", payload, "image/jpeg")},
        )
        attachment_id = upload_response.json()["attachment_id"]
        response = client.get(f"/api/attachments/{attachment_id}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == payload


def test_get_attachment_returns_not_found_for_unknown_id(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.get("/api/attachments/missing")

    assert response.status_code == 404


def _create_client(tmp_path: Path) -> TestClient:
    runtime = _AttachmentRuntime(tmp_path / "data")
    app = create_app(runtime=cast(Any, runtime))
    return TestClient(app)


def _jpeg_payload() -> bytes:
    return b"\xff\xd8\xff\xe0" + (b"\x00" * 32)
