"""Tests for the shared task artifact store."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.model_tasks.artifacts import (
    StoredArtifact,
    TaskArtifactStore,
    ensure_output_dir,
    validate_task_artifact_metadata_file,
    write_generated_media_artifact,
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


def test_artifact_ids_never_reuse_a_reserved_sidecar_or_an_existing_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.utils import ids

    values = iter((1, 2, 2, 3))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    artifact_dir = tmp_path / "speech"
    artifact_dir.mkdir()
    # A blob without sidecar, as an interrupted write can leave it behind.
    orphan = artifact_dir / "aud_000000000001.mp3"
    orphan.write_bytes(b"orphan")
    store = _store(tmp_path)

    first = store.write(b"first", extension="mp3", media_type="audio/mpeg")
    # The id reserved by the first write is taken for every extension.
    second = store.write(b"second", extension="wav", media_type="audio/wav")

    assert first.id == "aud_000000000002"
    assert second.id == "aud_000000000003"
    assert orphan.read_bytes() == b"orphan"
    assert not (artifact_dir / "aud_000000000001.json").exists()
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
    )
    assert result.id == f"{prefix}_000000000002"
    assert original.read_bytes() == b"keep"
    assert result.file_path.read_bytes() == b"new"


@pytest.mark.parametrize("can_delete", [False, True])
def test_output_preflight_accepts_write_only_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, can_delete: bool
) -> None:
    original_open, original_os_open, original_unlink = Path.open, os.open, Path.unlink

    def open_write_only(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path.parent == tmp_path and mode != "xb":
            raise PermissionError("The output folder allows only exclusive writes")
        return original_open(path, mode, *args, **kwargs)

    def os_open_write_only(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if (Path(path) == tmp_path or Path(path).parent == tmp_path) and (
            not flags & os.O_WRONLY or flags & os.O_RDWR
        ):
            raise PermissionError("The output folder does not allow read access")
        return original_os_open(path, flags, *args, **kwargs)

    def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.parent == tmp_path and not can_delete:
            raise PermissionError("The output folder does not allow deletion")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_write_only)
        patch.setattr(os, "open", os_open_write_only)
        patch.setattr(Path, "unlink", unlink)
        directory = ensure_output_dir(tmp_path)
        artifact = write_generated_media_artifact(
            b"audio", output_dir=directory, extension="mp3", media_type="audio/mpeg"
        )

    assert artifact.file_path.read_bytes() == b"audio"
    probes = list(tmp_path.glob(".vb_*.tmp"))
    assert len(probes) == (0 if can_delete else 1)
    assert all(probe.read_bytes() == b"" for probe in probes)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "music"])
async def test_generated_output_is_rechecked_after_provider_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import asyncio
    from typing import Any, cast

    from core.model_tasks.artifacts import OutputWriteError
    from core.model_tasks.image import ImageService
    from core.model_tasks.image_types import ImageGenerationResult
    from core.model_tasks.music import MusicService
    from core.model_tasks.music_types import MusicGenerationResult
    from core.model_tasks.video import VideoService
    from core.model_tasks.video_types import VideoGenerationResult

    service = {
        "image": ImageService,
        "video": VideoService,
        "music": MusicService,
    }[kind](cast(Any, object()), cast(Any, object()))
    results = {
        "image": ImageGenerationResult(images=(b"image",), media_type="image/svg+xml", model="m"),
        "video": VideoGenerationResult(
            data=b"video", media_type="video/webm", model="m", job_id="j"
        ),
        "music": MusicGenerationResult(data=b"music", media_type="audio/ogg", model="m"),
    }
    entered = asyncio.Event()
    release = asyncio.Event()
    output = tmp_path / "output"
    authorized = True
    checked: list[Path] = []

    async def generate(_prompt: str, **_kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return results[kind]

    async def check(path: Path) -> None:
        checked.append(path)
        if not authorized:
            raise PermissionError("output authority changed")

    monkeypatch.setattr(service, "generate", generate)
    method = service.generate_artifacts if kind == "image" else service.generate_artifact
    task = asyncio.create_task(method("prompt", output_dir=output, before_output_write=check))
    await entered.wait()
    checked.clear()  # The preflight probe was already authorized.
    authorized = False
    release.set()
    with pytest.raises(OutputWriteError) as caught:
        await task
    assert caught.value.code == "output_write_failed"
    assert caught.value.retryable is False
    assert caught.value.saved == ()
    assert checked[0].parent == output
    assert checked[0].suffix == {"image": ".svg", "video": ".webm", "music": ".ogg"}[kind]
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_generated_output_guard_runs_after_worker_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading
    from typing import Any, cast

    from core.model_tasks import artifacts as artifact_module
    from core.model_tasks.artifacts import OutputWriteError
    from core.model_tasks.image import ImageService
    from core.model_tasks.image_types import ImageGenerationResult
    from core.utils.workers import BoundedWorkerPool

    pool = BoundedWorkerPool(name="guarded-artifacts-test", max_workers=1)
    monkeypatch.setattr(artifact_module, "_PERSISTENCE_WORKERS", pool)
    service = ImageService(cast(Any, object()), cast(Any, object()))
    provider_entered, release_provider, generated = (asyncio.Event() for _ in range(3))
    worker_entered = asyncio.Event()
    release_worker = threading.Event()
    loop = asyncio.get_running_loop()
    allowed = True
    checked: list[Path] = []

    async def generate(_prompt: str, **_kwargs: Any) -> ImageGenerationResult:
        provider_entered.set()
        await release_provider.wait()
        generated.set()
        return ImageGenerationResult(images=(b"image",), media_type="image/png", model="m")

    def occupy_worker() -> None:
        loop.call_soon_threadsafe(worker_entered.set)
        assert release_worker.wait(2)

    async def check(path: Path) -> None:
        checked.append(path)
        if not allowed:
            raise PermissionError("output authority changed")

    monkeypatch.setattr(service, "generate", generate)
    task = asyncio.create_task(
        service.generate_artifacts("prompt", output_dir=tmp_path, before_output_write=check)
    )
    blocker = None
    try:
        await provider_entered.wait()
        checked.clear()  # The preflight probe was already authorized.
        blocker = asyncio.create_task(pool.run(occupy_worker))
        await worker_entered.wait()
        release_provider.set()
        await generated.wait()
        assert checked == []
        allowed = False
        release_worker.set()
        await blocker
        with pytest.raises(OutputWriteError) as caught:
            await task
        assert caught.value.code == "output_write_failed"
        assert len(checked) == 1
        assert not checked[0].exists()
    finally:
        release_provider.set()
        release_worker.set()
        await asyncio.gather(task, *([blocker] if blocker else []), return_exceptions=True)
        pool.shutdown()


@pytest.mark.asyncio
async def test_guarded_preflight_rechecks_after_worker_admission_without_creating_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from core.model_tasks import artifacts as artifact_module
    from core.utils.workers import BoundedWorkerPool

    pool = BoundedWorkerPool(name="guarded-preflight-test", max_workers=1)
    monkeypatch.setattr(artifact_module, "_PERSISTENCE_WORKERS", pool)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    allowed = True
    checked: list[Path] = []
    directory = tmp_path / "new-folder"

    def occupy_worker() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)

    async def guard(path: Path) -> None:
        checked.append(path)
        if not allowed:
            raise PermissionError("output authority changed")

    blocker = asyncio.create_task(pool.run(occupy_worker))
    await entered.wait()
    task = asyncio.create_task(artifact_module.prepare_guarded_output_dir(directory, guard))
    try:
        await asyncio.sleep(0)
        assert checked == []
        allowed = False
        release.set()
        await blocker
        with pytest.raises(PermissionError):
            await task
        assert len(checked) == 1
        assert checked[0].parent == directory
        assert not directory.exists()
    finally:
        release.set()
        await asyncio.gather(blocker, task, return_exceptions=True)
        pool.shutdown()


@pytest.mark.asyncio
async def test_guarded_preflight_preserves_unusable_directory_error(tmp_path: Path) -> None:
    from core.model_tasks.artifacts import OutputDirectoryError, prepare_guarded_output_dir

    target = tmp_path / "file"
    target.write_bytes(b"file")

    async def allow(_path: Path) -> None:
        pass

    with pytest.raises(OutputDirectoryError):
        await prepare_guarded_output_dir(target, allow)
