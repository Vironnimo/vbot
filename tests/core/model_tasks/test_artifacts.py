"""Tests for the shared task artifact store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.model_tasks.artifacts import TaskArtifactStore
from core.utils.errors import TaskError


class _StubConfigurationError(TaskError):
    pass


def _store(tmp_path: Path) -> TaskArtifactStore:
    return TaskArtifactStore(tmp_path / "speech", kind="speech", error=_StubConfigurationError)


def test_write_persists_blob_and_sidecar_with_extra_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)

    stored = store.write(
        b"audio", extension="mp3", media_type="audio/mpeg", extra_metadata={"index": 2}
    )

    assert stored.file_path == tmp_path / "speech" / f"{stored.id}.mp3"
    assert stored.file_path.read_bytes() == b"audio"
    sidecar = json.loads((tmp_path / "speech" / f"{stored.id}.json").read_text(encoding="utf-8"))
    assert sidecar == {
        "id": stored.id,
        "filename": f"{stored.id}.mp3",
        "media_type": "audio/mpeg",
        "size_bytes": 5,
        "index": 2,
    }


def test_read_round_trips_written_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    written = store.write(b"audio-bytes", extension="wav", media_type="audio/wav")

    loaded = store.read(written.id)

    assert loaded.id == written.id
    assert loaded.filename == written.filename
    assert loaded.media_type == "audio/wav"
    assert loaded.size_bytes == len(b"audio-bytes")
    assert loaded.file_path == written.file_path


def test_read_rejects_invalid_artifact_id(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(_StubConfigurationError, match="Invalid speech artifact id"):
        store.read("../escape")


def test_read_rejects_missing_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(_StubConfigurationError, match="Speech artifact not found"):
        store.read("a" * 32)


def test_read_rejects_unreadable_and_invalid_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact_dir = tmp_path / "speech"
    artifact_dir.mkdir(parents=True)
    broken_id = "b" * 32
    (artifact_dir / f"{broken_id}.json").write_text("{not json", encoding="utf-8")
    invalid_id = "c" * 32
    (artifact_dir / f"{invalid_id}.json").write_text(json.dumps({"filename": 7}), encoding="utf-8")

    with pytest.raises(_StubConfigurationError, match="metadata is unreadable"):
        store.read(broken_id)
    with pytest.raises(_StubConfigurationError, match="metadata is invalid"):
        store.read(invalid_id)


def test_read_rejects_missing_blob_and_recovers_size_from_stat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    written = store.write(b"abc", extension="mp3", media_type="audio/mpeg")

    sidecar_path = tmp_path / "speech" / f"{written.id}.json"
    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
    metadata["size_bytes"] = "not-an-int"
    sidecar_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert store.read(written.id).size_bytes == 3

    written.file_path.unlink()
    with pytest.raises(_StubConfigurationError, match="Speech artifact file not found"):
        store.read(written.id)
