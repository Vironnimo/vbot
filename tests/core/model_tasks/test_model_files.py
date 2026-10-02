"""Pinned model files: download, resume, cache adoption and verification over a fake Hub."""

from __future__ import annotations

import hashlib
import threading
from collections import namedtuple
from pathlib import Path

import httpx
import pytest

from core.model_tasks import model_files
from core.model_tasks.model_files import (
    ModelFile,
    ModelFilesCancelledError,
    ModelFilesError,
    PinnedModel,
    fetch_model_files,
)

REVISION = "0" * 40
WEIGHTS = bytes(range(256)) * 40
CONFIG = b'{"model": "example"}'


def pinned(**files: bytes) -> PinnedModel:
    return PinnedModel(
        "example/model",
        REVISION,
        tuple(
            ModelFile(path, hashlib.sha256(content).hexdigest(), len(content))
            for path, content in files.items()
        ),
    )


class Hub:
    """Serves *contents* at Hub resolve URLs, honouring byte ranges unless told not to."""

    def __init__(
        self, contents: dict[str, bytes], *, status: int = 200, ranges: bool = True
    ) -> None:
        self.contents = contents
        self.status = status
        self.ranges = ranges
        self.requests: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split(f"/resolve/{REVISION}/", 1)[1]
        self.requests.append((path, request.headers.get("Range")))
        if self.status != 200:
            return httpx.Response(self.status)
        content = self.contents[path]
        if self.ranges and (header := request.headers.get("Range")):
            start = int(header.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=content[start:])
        return httpx.Response(200, content=content)


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf-cache"))
    monkeypatch.setattr(model_files, "_RETRY_PAUSE_S", 0.0)

    def serve(server: Hub) -> Hub:
        monkeypatch.setattr(
            model_files,
            "open_client",
            lambda: httpx.Client(transport=httpx.MockTransport(server)),
        )
        return server

    return serve


def fetch(model: PinnedModel, directory: Path) -> list[int]:
    reported: list[int] = []
    fetch_model_files(model, directory, progress=reported.append, cancelled=threading.Event())
    return reported


def test_fetch_downloads_missing_files_and_keeps_complete_ones(tmp_path: Path, hub) -> None:
    model = pinned(**{"onnx/model.bin": WEIGHTS, "config.json": CONFIG})
    target = tmp_path / "model"
    target.mkdir()
    (target / "config.json").write_bytes(CONFIG)
    server = hub(Hub({"onnx/model.bin": WEIGHTS}))

    reported = fetch(model, target)

    assert [path for path, _range in server.requests] == ["onnx/model.bin"]
    assert (target / "onnx" / "model.bin").read_bytes() == WEIGHTS
    assert not list(target.rglob("*.part"))
    assert reported == sorted(reported) and reported[-1] == model.download_bytes


def test_fetch_adopts_verified_copies_from_the_hugging_face_cache(tmp_path: Path, hub) -> None:
    tokenizer = b"tokens" * 10
    model = pinned(**{"model.bin": WEIGHTS, "config.json": CONFIG, "tokenizer.json": tokenizer})
    repository = tmp_path / "hf-cache" / "models--example--model"
    # Large files are cache blobs named by their SHA-256; small ones sit in snapshots.
    (repository / "blobs").mkdir(parents=True)
    (repository / "blobs" / model.files[0].sha256).write_bytes(WEIGHTS)
    snapshot = repository / "snapshots" / "older-revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_bytes(CONFIG)
    # Same size, other bytes: never adopted.
    (snapshot / "tokenizer.json").write_bytes(b"x" * len(tokenizer))
    server = hub(Hub({"tokenizer.json": tokenizer}))

    fetch(model, tmp_path / "model")

    assert [path for path, _range in server.requests] == ["tokenizer.json"]
    for item, content in zip(model.files, (WEIGHTS, CONFIG, tokenizer), strict=True):
        assert (tmp_path / "model" / item.path).read_bytes() == content


@pytest.mark.parametrize("ranges", [True, False], ids=["resumed", "range-ignored"])
def test_fetch_continues_a_partial_download(tmp_path: Path, hub, ranges: bool) -> None:
    model = pinned(**{"model.bin": WEIGHTS})
    target = tmp_path / "model"
    target.mkdir()
    (target / "model.bin.part").write_bytes(WEIGHTS[:1000])
    server = hub(Hub({"model.bin": WEIGHTS}, ranges=ranges))

    fetch(model, target)

    assert server.requests == [("model.bin", "bytes=1000-")]
    assert (target / "model.bin").read_bytes() == WEIGHTS


Failure = namedtuple("Failure", "served status free error requests")


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            Failure(b"x" * len(WEIGHTS), 200, None, "checksum_mismatch", 1), id="checksum"
        ),
        pytest.param(Failure(WEIGHTS, 503, None, "download_failed", 4), id="unavailable"),
        pytest.param(Failure(WEIGHTS, 404, None, "download_failed", 1), id="missing"),
        pytest.param(Failure(WEIGHTS, 200, 1024, "insufficient_space", 0), id="disk-full"),
    ],
)
def test_fetch_failure_leaves_no_file_under_its_final_name(
    tmp_path: Path, hub, monkeypatch: pytest.MonkeyPatch, case: Failure
) -> None:
    model = pinned(**{"model.bin": WEIGHTS})
    server = hub(Hub({"model.bin": case.served}, status=case.status))
    if case.free is not None:
        usage = model_files.shutil.disk_usage(tmp_path)
        monkeypatch.setattr(
            model_files.shutil, "disk_usage", lambda _path: usage._replace(free=case.free)
        )

    with pytest.raises(ModelFilesError) as failure:
        fetch(model, tmp_path / "model")

    assert failure.value.code == case.error
    assert len(server.requests) == case.requests
    assert not (tmp_path / "model" / "model.bin").exists()


def test_cancelled_fetch_stops_and_keeps_the_partial_file(tmp_path: Path, hub) -> None:
    model = pinned(**{"model.bin": WEIGHTS * 400})
    hub(Hub({"model.bin": WEIGHTS * 400}))
    cancelled = threading.Event()

    def progress(completed: int) -> None:
        if completed:
            cancelled.set()

    with pytest.raises(ModelFilesCancelledError):
        fetch_model_files(model, tmp_path / "model", progress=progress, cancelled=cancelled)

    assert (tmp_path / "model" / "model.bin.part").is_file()
    assert not (tmp_path / "model" / "model.bin").exists()
