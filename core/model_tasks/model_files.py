"""Pinned model files of local engines: fetch, verify and adopt them.

A :class:`PinnedModel` names one Hugging Face repository revision and every
file a local engine loads from it, each with its SHA-256 and size. Installing
the model fetches exactly those files into one directory on this machine, so
a ready model runs offline and never changes underneath vBot.

:func:`fetch_model_files` runs in a worker thread. Per file it keeps a file
that is already complete, otherwise adopts a matching copy from the local
Hugging Face cache (a hard link where possible), otherwise downloads it over
HTTPS, resuming a partial download. Every file is checked against its
SHA-256 before it takes its final name. Progress counts the bytes of files
already present, adopted, or downloaded so far.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import httpx

from core.utils.tls import shared_ssl_context

_CHUNK_BYTES = 1024 * 1024
_ATTEMPTS = 4
_RETRY_PAUSE_S = 2.0
# Space kept free beyond the files themselves, for the rest of the machine.
_SPACE_MARGIN_BYTES = 512 * 1024 * 1024
_HUB_URL = "https://huggingface.co"


@dataclass(frozen=True)
class ModelFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class PinnedModel:
    """One repository revision and the files a local engine loads from it."""

    repo: str
    revision: str
    files: tuple[ModelFile, ...]

    @property
    def download_bytes(self) -> int:
        return sum(item.size for item in self.files)


class ModelFilesError(Exception):
    """Fetching failed; ``code`` is the stable reason the WebUI translates.

    ``download_failed``, ``checksum_mismatch`` or ``insufficient_space``.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelFilesCancelledError(Exception):
    """The installation was cancelled while files were being fetched."""


def open_client() -> httpx.Client:
    """The HTTP client downloads use; tests replace it with a fake transport."""
    return httpx.Client(
        verify=shared_ssl_context(),
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=120.0),
    )


def fetch_model_files(
    model: PinnedModel,
    directory: Path,
    *,
    progress: Callable[[int], None],
    cancelled: threading.Event,
    reuse: Sequence[Path] = (),
) -> None:
    """Make *directory* hold every file of *model*, verified; blocking.

    *progress* receives the bytes completed so far, out of
    ``model.download_bytes``. Missing files are adopted when a matching copy
    exists under one of the *reuse* directories (such as an earlier revision
    of this model) or in the Hugging Face cache, and downloaded otherwise.
    Raises :class:`ModelFilesError` or :class:`ModelFilesCancelledError`;
    files already verified stay in place.
    """
    directory.mkdir(parents=True, exist_ok=True)
    done = 0
    progress(0)
    missing: list[ModelFile] = []
    for item in model.files:
        target = directory / item.path
        if _complete(target, item, cancelled, partial(_offset, progress, done)):
            done += item.size
            progress(done)
        else:
            missing.append(item)
    if not missing:
        return
    needed = sum(item.size for item in missing) + _SPACE_MARGIN_BYTES
    if shutil.disk_usage(directory).free < needed:
        raise ModelFilesError("insufficient_space")
    with open_client() as client:
        for item in missing:
            target = directory / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            report = partial(_offset, progress, done)
            if not _adopt(model, item, target, reuse, cancelled, report):
                _download(client, model, item, target, cancelled, report)
            done += item.size
            progress(done)


def _offset(progress: Callable[[int], None], done: int, current: int) -> None:
    progress(done + current)


def _complete(
    target: Path,
    item: ModelFile,
    cancelled: threading.Event,
    report: Callable[[int], None],
) -> bool:
    try:
        if not target.is_file() or target.stat().st_size != item.size:
            return False
    except OSError:
        return False
    return _sha256(target, cancelled, report) == item.sha256


def _sha256(path: Path, cancelled: threading.Event, report: Callable[[int], None]) -> str:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            if cancelled.is_set():
                raise ModelFilesCancelledError
            digest.update(chunk)
            count += len(chunk)
            report(count)
    return digest.hexdigest()


def hub_cache() -> Path:
    """The Hugging Face cache this machine's tools download into."""
    if cache := os.environ.get("HF_HUB_CACHE"):
        return Path(cache)
    if home := os.environ.get("HF_HOME"):
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _cached_candidates(model: PinnedModel, item: ModelFile) -> Iterator[Path]:
    """Files in the Hugging Face cache that may hold *item*'s exact bytes.

    The cache names large (LFS) blobs by their SHA-256; small files are found
    through the repository's snapshot folders. Each candidate is still hashed.
    """
    repository = hub_cache() / ("models--" + model.repo.replace("/", "--"))
    yield repository / "blobs" / item.sha256
    try:
        snapshots = sorted((repository / "snapshots").iterdir())
    except OSError:
        return
    for snapshot in snapshots:
        yield snapshot / item.path


def _adopt(
    model: PinnedModel,
    item: ModelFile,
    target: Path,
    reuse: Sequence[Path],
    cancelled: threading.Event,
    report: Callable[[int], None],
) -> bool:
    partial = target.with_name(target.name + ".part")
    candidates = (*(folder / item.path for folder in reuse), *_cached_candidates(model, item))
    for candidate in candidates:
        try:
            if not candidate.is_file() or candidate.stat().st_size != item.size:
                continue
            partial.unlink(missing_ok=True)
            try:
                os.link(candidate.resolve(), partial)
            except OSError:
                # The system copy routine clones blocks where the file system can
                # (CopyFile2 on Windows, reflink or copy_file_range on Linux).
                candidate.copy(partial)
            if _sha256(partial, cancelled, report) == item.sha256:
                os.replace(partial, target)
                return True
        except OSError:
            pass
        partial.unlink(missing_ok=True)
    return False


def _download(
    client: httpx.Client,
    model: PinnedModel,
    item: ModelFile,
    target: Path,
    cancelled: threading.Event,
    report: Callable[[int], None],
) -> None:
    """Download one file to ``<name>.part``, resuming it, then verify and rename."""
    partial = target.with_name(target.name + ".part")
    url = f"{_HUB_URL}/{model.repo}/resolve/{model.revision}/{item.path}"
    last = _ATTEMPTS - 1
    for attempt in range(_ATTEMPTS):
        # A pause before each retry; cancelling the installation ends it at once.
        if attempt and cancelled.wait(_RETRY_PAUSE_S * attempt):
            raise ModelFilesCancelledError
        digest = hashlib.sha256()
        offset = 0
        if partial.is_file() and 0 < partial.stat().st_size <= item.size:
            # An earlier attempt or installation left these bytes; continue after them.
            with partial.open("rb") as handle:
                while chunk := handle.read(_CHUNK_BYTES):
                    if cancelled.is_set():
                        raise ModelFilesCancelledError
                    digest.update(chunk)
                    offset += len(chunk)
        if offset < item.size:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with client.stream("GET", url, headers=headers) as response:
                    status = response.status_code
                    if status == 429 or status >= 500:
                        if attempt < last:
                            continue
                        raise ModelFilesError("download_failed")
                    if status not in (200, 206):
                        raise ModelFilesError("download_failed")
                    if offset and status == 200:
                        # The server ignored the range: start over.
                        digest, offset = hashlib.sha256(), 0
                    with partial.open("ab" if offset else "wb") as handle:
                        for chunk in response.iter_bytes(_CHUNK_BYTES):
                            if cancelled.is_set():
                                raise ModelFilesCancelledError
                            handle.write(chunk)
                            digest.update(chunk)
                            offset += len(chunk)
                            if offset > item.size:
                                break
                            report(offset)
            except httpx.HTTPError:
                if attempt < last:
                    continue
                raise ModelFilesError("download_failed") from None
            if offset < item.size:
                # The response ended early; resume after what arrived.
                if attempt < last:
                    continue
                raise ModelFilesError("download_failed")
        if offset != item.size or digest.hexdigest() != item.sha256:
            partial.unlink(missing_ok=True)
            raise ModelFilesError("checksum_mismatch")
        os.replace(partial, target)
        return
    raise ModelFilesError("download_failed")
