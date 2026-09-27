"""Tests for the shared task artifact store."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from core.model_tasks.artifacts import (
    StoredArtifact,
    TaskArtifactStore,
    validate_task_artifact_metadata_file,
)
from core.utils.errors import TaskError


class _StubConfigurationError(TaskError):
    pass


def _store(tmp_path: Path) -> TaskArtifactStore:
    return TaskArtifactStore(tmp_path / "speech", kind="speech", error=_StubConfigurationError)


def test_write_persists_blob_and_versioned_sidecar(tmp_path: Path) -> None:
    store = _store(tmp_path)

    stored = store.write(b"audio", extension="mp3", media_type="audio/mpeg")

    assert stored.file_path == tmp_path / "speech" / f"{stored.id}.mp3"
    assert stored.file_path.read_bytes() == b"audio"
    sidecar_path = tmp_path / "speech" / f"{stored.id}.json"
    assert json.loads(sidecar_path.read_text(encoding="utf-8")) == {
        "format_version": 1,
        "id": stored.id,
        "filename": f"{stored.id}.mp3",
        "media_type": "audio/mpeg",
        "size_bytes": 5,
    }
    assert validate_task_artifact_metadata_file(sidecar_path).diagnostics == ()


def test_read_ignores_unknown_fields_and_refuses_a_newer_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    written = store.write(b"audio", extension="mp3", media_type="audio/mpeg")
    sidecar_path = tmp_path / "speech" / f"{written.id}.json"
    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar_path.write_text(json.dumps({**metadata, "voice": "alloy"}), encoding="utf-8")

    assert store.read(written.id) == written
    assert [
        item.path for item in validate_task_artifact_metadata_file(sidecar_path).diagnostics
    ] == ["$.voice"]

    sidecar_path.write_text(json.dumps({**metadata, "format_version": 2}), encoding="utf-8")
    with pytest.raises(_StubConfigurationError):
        store.read(written.id)
    assert not validate_task_artifact_metadata_file(sidecar_path).ok


def _rewrite(**changes: object) -> Callable[[Path, StoredArtifact], None]:
    def rewrite(sidecar: Path, _artifact: StoredArtifact) -> None:
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        sidecar.write_text(json.dumps({**metadata, **changes}), encoding="utf-8")

    return rewrite


@pytest.mark.parametrize(
    ("read_id", "corrupt", "message"),
    [
        pytest.param("../escape", None, "Invalid speech artifact id", id="unsafe-id"),
        pytest.param("a" * 32, None, "Speech artifact not found", id="missing"),
        pytest.param(
            None,
            lambda sidecar, _: sidecar.write_text("[]", encoding="utf-8"),
            "metadata is unreadable",
            id="non-object-metadata",
        ),
        pytest.param(
            None,
            lambda sidecar, _: sidecar.write_text("{not json", encoding="utf-8"),
            "metadata is unreadable",
            id="broken-json",
        ),
        pytest.param(
            None,
            lambda sidecar, _: sidecar.write_bytes(b"\xff\xfeinvalid"),
            "metadata is unreadable",
            id="invalid-utf8",
        ),
        pytest.param(
            None, _rewrite(size_bytes="not-an-int"), "metadata is unreadable", id="invalid-size"
        ),
        pytest.param(
            None, _rewrite(id="different-artifact"), "metadata is invalid", id="mismatched-id"
        ),
        # The sidecar cannot redirect a read to any other existing file.
        pytest.param(
            None, _rewrite(filename="../outside.mp3"), "metadata is invalid", id="traversal"
        ),
        pytest.param(
            None, _rewrite(filename="other-artifact.mp3"), "metadata is invalid", id="other-file"
        ),
        pytest.param(
            None,
            lambda _, artifact: artifact.file_path.unlink(),
            "artifact file not found",
            id="missing-blob",
        ),
    ],
)
def test_read_rejects_every_unusable_artifact(
    tmp_path: Path,
    read_id: str | None,
    corrupt: Callable[[Path, StoredArtifact], None] | None,
    message: str,
) -> None:
    store = _store(tmp_path)
    written = store.write(b"audio", extension="mp3", media_type="audio/mpeg")
    (tmp_path / "outside.mp3").write_bytes(b"not this artifact")
    (tmp_path / "speech" / "other-artifact.mp3").write_bytes(b"another artifact")
    if corrupt is not None:
        corrupt(tmp_path / "speech" / f"{written.id}.json", written)

    with pytest.raises(_StubConfigurationError, match=message):
        store.read(read_id or written.id)


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
