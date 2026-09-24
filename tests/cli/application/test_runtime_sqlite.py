"""Tests for the pinned SQLite library of private Windows runtimes."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from cli.application.runtime_sqlite import (
    SQLITE_LOCK,
    RuntimeSQLiteError,
    provision_runtime_sqlite,
)
from core.sessions.schema import is_wal_reset_vulnerable

_REPO_ROOT = Path(__file__).parents[3]
_LIBRARY = b"pinned sqlite library"


def _archive(library: bytes = _LIBRARY) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("sqlite3.dll", library)
        bundle.writestr("sqlite3.def", "EXPORTS\n")
    return buffer.getvalue()


def _source(tmp_path: Path, archive: bytes, library: bytes = _LIBRARY) -> Path:
    source = tmp_path / "source"
    lock = source / SQLITE_LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps(
            {
                "version": "3.53.4",
                "url": "https://sqlite.org/2026/sqlite-dll-win-x64-3530400.zip",
                "archive_sha3_256": hashlib.sha3_256(archive).hexdigest(),
                "library_sha256": hashlib.sha256(library).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return source


def _runtime(tmp_path: Path, library: bytes = b"cpython sqlite library") -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "DLLs").mkdir(parents=True)
    (runtime / "DLLs" / "sqlite3.dll").write_bytes(library)
    return runtime


def _unexpected_fetch(url: str) -> bytes:
    raise AssertionError(f"unexpected download: {url}")


def test_replaces_the_runtime_library_with_the_verified_pin(tmp_path: Path) -> None:
    archive = _archive()
    source = _source(tmp_path, archive)
    runtime = _runtime(tmp_path)
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        return archive

    assert provision_runtime_sqlite(runtime, source, fetch=fetch) is True

    assert (runtime / "DLLs" / "sqlite3.dll").read_bytes() == _LIBRARY
    assert requested == ["https://sqlite.org/2026/sqlite-dll-win-x64-3530400.zip"]
    assert sorted(path.name for path in (runtime / "DLLs").iterdir()) == ["sqlite3.dll"]


def test_keeps_an_already_pinned_library_without_downloading(tmp_path: Path) -> None:
    source = _source(tmp_path, _archive())
    runtime = _runtime(tmp_path, _LIBRARY)

    assert provision_runtime_sqlite(runtime, source, fetch=_unexpected_fetch) is False
    assert (runtime / "DLLs" / "sqlite3.dll").read_bytes() == _LIBRARY


def test_source_without_a_pin_leaves_the_runtime_unchanged(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source = tmp_path / "older-source"
    source.mkdir()

    assert provision_runtime_sqlite(runtime, source, fetch=_unexpected_fetch) is False
    assert (runtime / "DLLs" / "sqlite3.dll").read_bytes() == b"cpython sqlite library"


@pytest.mark.parametrize("tampered", ["archive", "library"])
def test_rejects_tampered_downloads_and_keeps_the_existing_library(
    tmp_path: Path, tampered: str
) -> None:
    archive = _archive()
    source = _source(tmp_path, archive)
    runtime = _runtime(tmp_path)
    if tampered == "archive":
        served = archive + b"\0"
    else:
        served = _archive(b"different library")
        lock_path = source / SQLITE_LOCK
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["archive_sha3_256"] = hashlib.sha3_256(served).hexdigest()
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(RuntimeSQLiteError):
        provision_runtime_sqlite(runtime, source, fetch=lambda _url: served)

    assert (runtime / "DLLs" / "sqlite3.dll").read_bytes() == b"cpython sqlite library"
    assert sorted(path.name for path in (runtime / "DLLs").iterdir()) == ["sqlite3.dll"]


def test_committed_pin_is_an_official_build_without_the_wal_reset_bug() -> None:
    lock = json.loads((_REPO_ROOT / SQLITE_LOCK).read_text(encoding="utf-8"))
    version = tuple(int(part) for part in lock["version"].split("."))

    major, minor, patch = version
    assert lock["url"].startswith("https://sqlite.org/")
    assert lock["url"].endswith(f"-{major}{minor:02d}{patch:02d}00.zip")
    assert not is_wal_reset_vulnerable(version)
