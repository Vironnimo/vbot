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

    with pytest.raises(_StubConfigurationError):
        store.read("../escape")


def test_read_rejects_missing_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(_StubConfigurationError):
        store.read("a" * 32)


def test_read_rejects_unreadable_and_invalid_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact_dir = tmp_path / "speech"
    artifact_dir.mkdir(parents=True)
    broken_id = "b" * 32
    (artifact_dir / f"{broken_id}.json").write_text("{not json", encoding="utf-8")
    invalid_id = "c" * 32
    (artifact_dir / f"{invalid_id}.json").write_text(json.dumps({"filename": 7}), encoding="utf-8")

    with pytest.raises(_StubConfigurationError):
        store.read(broken_id)
    with pytest.raises(_StubConfigurationError):
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
    with pytest.raises(_StubConfigurationError):
        store.read(written.id)


def test_short_artifact_ids_reserve_sidecars_across_extensions(tmp_path, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    store = _store(tmp_path)
    first = store.write(b"first", extension="mp3", media_type="audio/mpeg")
    second = store.write(b"second", extension="wav", media_type="audio/wav")
    assert first.id == "aud_000000000001"
    assert second.id == "aud_000000000002"
    assert store.read(first.id).file_path.read_bytes() == b"first"
    assert store.read(second.id).file_path.read_bytes() == b"second"


@pytest.mark.parametrize(("media_type", "prefix"), [("video/mp4", "vid"), ("audio/mpeg", "mus")])
def test_generated_media_ids_never_overwrite_colliding_files(
    tmp_path, monkeypatch, media_type, prefix
):
    from core.model_tasks.artifacts import write_generated_media_artifact
    from core.utils import ids

    original = tmp_path / f"{prefix}_000000000001.mp4"
    original.write_bytes(b"keep")
    values = iter((1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    result = write_generated_media_artifact(
        b"new",
        output_dir=tmp_path,
        extension="mp4",
        media_type=media_type,
        error=_StubConfigurationError,
    )
    assert result.id == f"{prefix}_000000000002"
    assert original.read_bytes() == b"keep"
    assert result.file_path.read_bytes() == b"new"
