"""Attachment upload and download endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, override

import pytest
from fastapi import HTTPException, Request  # type: ignore[import-not-found]
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.attachments import AttachmentStore
from server.app import (
    MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES,
    _parse_upload_file_with_limit,
    create_app,
)
from tests.server.app_test_support import ServerStubRuntime

_JPEG = b"\xff\xd8\xff\xe0" + (b"\x00" * 32)


class _RecordingAttachmentStore(AttachmentStore):
    def __init__(self, data_dir: Path, *, max_size_bytes: int) -> None:
        super().__init__(data_dir, max_size_bytes=max_size_bytes)
        self.stored: list[str] = []

    @override
    def store(self, filename: str, data: bytes) -> Any:
        self.stored.append(filename)
        return super().store(filename, data)


def test_upload_returns_metadata_serves_the_original_and_blocks_executables(
    tmp_path: Path,
) -> None:
    text = b"hello from text file\nsecond line"
    executable = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00"

    with _client(tmp_path) as client:
        photo = client.post("/api/upload", files={"file": ("photo.jpg", _JPEG, "image/jpeg")})
        note = client.post("/api/upload", files={"file": ("note.txt", text, "text/plain")})
        blocked = client.post(
            "/api/upload", files={"file": ("payload.exe", executable, "application/octet-stream")}
        )
        served = client.get(f"/api/attachments/{photo.json()['attachment_id']}")
        unknown = client.get("/api/attachments/missing")

    photo_body, note_body = photo.json(), note.json()
    assert photo_body["attachment_id"] and note_body["attachment_id"]
    assert {key: photo_body[key] for key in ("filename", "media_type", "size_bytes")} == {
        "filename": "photo.jpg",
        "media_type": "image/jpeg",
        "size_bytes": len(_JPEG),
    }
    assert (note_body["filename"], note_body["size_bytes"]) == ("note.txt", len(text))
    assert note_body["media_type"].startswith("text/")
    # Uploads return metadata only, never extracted text.
    assert "text_content" not in photo_body and "text_content" not in note_body
    assert served.headers["content-type"].startswith("image/jpeg")
    assert served.headers["content-disposition"] == 'inline; filename="photo.jpg"'
    assert served.content == _JPEG
    assert unknown.status_code == 404
    assert blocked.status_code == 415


def test_upload_limit_rejects_before_storing(tmp_path: Path) -> None:
    store = _RecordingAttachmentStore(tmp_path / "data", max_size_bytes=3)

    with _client(tmp_path, store) as client:
        exact = client.post("/api/upload", files={"file": ("exact.txt", b"abc", "text/plain")})
        too_large = client.post("/api/upload", files={"file": ("large.txt", b"abcd", "text/plain")})
        # A declared length beyond limit plus multipart overhead is refused unread.
        declared_too_large = client.post(
            "/api/upload",
            content=b"",
            headers={
                "content-type": "multipart/form-data; boundary=vbot",
                "content-length": str(3 + MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES + 1),
            },
        )

    assert (exact.status_code, exact.json()["size_bytes"]) == (200, 3)
    assert too_large.status_code == declared_too_large.status_code == 413
    assert store.stored == ["exact.txt"]


@pytest.mark.asyncio
async def test_upload_stops_consuming_chunked_body_after_file_limit() -> None:
    # TestClient buffers request bodies, so consumption is observed on the parser.
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
        await _parse_upload_file_with_limit(request, max_size_bytes=3, upload_kind="Attachment")

    assert exc_info.value.status_code == 413
    assert closing_boundary_consumed is False


@pytest.mark.parametrize(
    "metadata_change",
    [None, {"media_type": []}, {"media_type": {}}],
    ids=["invalid-json", "array-media-type", "object-media-type"],
)
def test_get_attachment_with_corrupt_metadata_is_unavailable_not_internal_error(
    tmp_path: Path, metadata_change: dict[str, object] | None
) -> None:
    store = AttachmentStore(tmp_path / "data", max_size_bytes=1024)

    with _client(tmp_path, store) as client:
        upload = client.post("/api/upload", files={"file": ("photo.jpg", _JPEG, "image/jpeg")})
        attachment_id = upload.json()["attachment_id"]
        path = store._sidecar_path(attachment_id)  # noqa: SLF001
        text = (
            "{not json"
            if metadata_change is None
            else json.dumps({**json.loads(path.read_text(encoding="utf-8")), **metadata_change})
        )
        path.write_text(text, encoding="utf-8")
        response = client.get(f"/api/attachments/{attachment_id}")

    assert response.status_code == 404
    assert "sidecar" not in response.text.lower()


def _client(tmp_path: Path, store: AttachmentStore | None = None) -> TestClient:
    data_dir = tmp_path / "data"
    attachment_store = store or AttachmentStore(data_dir, max_size_bytes=20_971_520)
    runtime = ServerStubRuntime(data_dir, attachment_store=attachment_store)
    return TestClient(create_app(runtime=runtime))
