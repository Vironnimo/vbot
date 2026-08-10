"""Tests for attachment upload and download endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request  # type: ignore[import-not-found]
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.attachments import AttachmentStore
from core.runs import ChatRunManager
from server.app import (
    MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES,
    _parse_upload_file_with_limit,
    create_app,
)

MAX_ATTACHMENT_SIZE_BYTES = 20_971_520


class _AttachmentRuntime:
    def __init__(self, data_dir: Path) -> None:
        self.storage = type("Storage", (), {"data_dir": data_dir})()
        self.attachment_store = AttachmentStore(
            data_dir,
            max_size_bytes=MAX_ATTACHMENT_SIZE_BYTES,
        )
        self.chat_runs = ChatRunManager()
        self.chat_run_manager = self.chat_runs
        self.chat_loop = object()
        self.streaming_chat_loop = object()
        self.command_dispatcher = object()

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
    assert "text_content" not in body


def test_upload_text_file_returns_attachment_metadata_only(tmp_path: Path) -> None:
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
    assert "text_content" not in body


def test_upload_rejects_payload_over_20_mib_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.app as server_app

    payload = b"a" * (MAX_ATTACHMENT_SIZE_BYTES + 1)
    read_calls = 0
    original_reader = server_app._read_upload_file_with_limit

    async def track_spooled_file_read(*args: Any, **kwargs: Any) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return await original_reader(*args, **kwargs)

    monkeypatch.setattr(server_app, "_read_upload_file_with_limit", track_spooled_file_read)

    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("too-large.bin", payload, "application/octet-stream")},
        )

    assert response.status_code == 413
    assert read_calls == 0


def test_upload_rejects_definitely_oversized_content_length_before_parsing(
    tmp_path: Path,
) -> None:
    runtime = _AttachmentRuntime(tmp_path / "data")
    runtime.attachment_store = _RejectingAttachmentStore(tmp_path / "data", max_size_bytes=3)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/upload",
            content=b"",
            headers={
                "content-type": "multipart/form-data; boundary=vbot",
                "content-length": str(3 + MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES + 1),
            },
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_upload_stops_consuming_chunked_body_after_file_limit() -> None:
    closing_boundary_consumed = False
    chunks = [
        (
            b"--vbot\r\n"
            b'Content-Disposition: form-data; name="file"; filename="large.bin"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n"
        ),
        b"a" * 128,
        b"\r\n--vbot--\r\n",
    ]

    async def receive() -> dict[str, Any]:
        nonlocal closing_boundary_consumed
        body = chunks.pop(0)
        if not chunks:
            closing_boundary_consumed = True
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/upload",
            "headers": [(b"content-type", b"multipart/form-data; boundary=vbot")],
        },
        receive,
    )
    with pytest.raises(HTTPException) as exc_info:
        await _parse_upload_file_with_limit(
            request,
            max_size_bytes=3,
            upload_kind="Attachment",
        )

    assert exc_info.value.status_code == 413
    assert closing_boundary_consumed is False


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


def test_upload_accepts_file_exactly_at_configured_limit(tmp_path: Path) -> None:
    runtime = _AttachmentRuntime(tmp_path / "data")
    runtime.attachment_store = AttachmentStore(tmp_path / "data", max_size_bytes=3)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("exact.txt", b"abc", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["size_bytes"] == 3


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
    assert response.headers["content-disposition"] == 'inline; filename="photo.jpg"'
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
