"""Tests for image HTTP artifact endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.image import ImageArtifact, ImageConfigurationError
from core.runs import ChatRunManager
from server.app import create_app


def test_image_artifact_endpoint_streams_existing_blob(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\nimage-bytes"
    runtime = _ImageRuntime(tmp_path / "data", payload=payload)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.get(f"/api/images/artifacts/{'a' * 32}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == payload


def test_image_artifact_endpoint_maps_expected_errors(tmp_path: Path) -> None:
    runtime = _ImageRuntime(tmp_path / "data", fail=True)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.get("/api/images/artifacts/not-an-id")

    assert response.status_code == 409
    assert response.json()["detail"] == "Invalid image artifact id"


class _ImageRuntime:
    def __init__(self, data_dir: Path, *, payload: bytes = b"", fail: bool = False) -> None:
        self.storage = type("Storage", (), {"data_dir": data_dir})()
        self.chat_runs = ChatRunManager()
        self.image = _FailingImage() if fail else _Image(data_dir, payload)

    def start(self) -> None:
        self.storage.data_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        return None


class _Image:
    def __init__(self, data_dir: Path, payload: bytes) -> None:
        self._file_path = data_dir / "images" / "artifact.png"
        self._payload = payload

    def get_artifact(self, artifact_id: str) -> ImageArtifact:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_bytes(self._payload)
        return ImageArtifact(
            id=artifact_id,
            filename="artifact.png",
            media_type="image/png",
            size_bytes=len(self._payload),
            file_path=self._file_path,
        )


class _FailingImage:
    def get_artifact(self, _artifact_id: str) -> ImageArtifact:
        raise ImageConfigurationError("Invalid image artifact id")
