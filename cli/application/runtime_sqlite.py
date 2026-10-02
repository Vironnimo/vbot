"""Pinned SQLite library for private Windows CPython runtimes.

CPython's Windows builds ship an SQLite that still carries the WAL-reset
corruption bug (3.7.0 through 3.51.2), which confines vBot's SQLite databases to
the slower rollback journal (``core.database.required_journal_mode``).
Every prepared runtime therefore replaces ``DLLs/sqlite3.dll`` with the
official sqlite.org build pinned in ``scripts/windows/sqlite.lock.json``.
SQLite keeps its C ABI stable, so CPython's ``_sqlite3`` extension loads the
newer library unchanged. Only the standard library is used: release builds
import this module from a stdlib-only build interpreter.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

SQLITE_LOCK = Path("scripts/windows/sqlite.lock.json")
_LIBRARY = "sqlite3.dll"
_ARCHIVE_LIMIT_BYTES = 16 * 1024 * 1024


class RuntimeSQLiteError(RuntimeError):
    """The pinned SQLite library could not be verified or installed."""


def provision_runtime_sqlite(
    runtime: Path,
    source: Path,
    *,
    fetch: Callable[[str], bytes] | None = None,
) -> bool:
    """Install the SQLite library pinned by *source* into *runtime*.

    A library that already matches the pin is kept. Returns whether the file
    was replaced.
    """
    lock = _read_lock(source / SQLITE_LOCK)
    destination = runtime / "DLLs" / _LIBRARY
    if not destination.parent.is_dir():
        raise RuntimeSQLiteError("the runtime has no DLLs directory")
    if destination.is_file():
        with destination.open("rb") as installed:
            if hashlib.file_digest(installed, "sha256").hexdigest() == lock["library_sha256"]:
                return False
    archive = (fetch or _download)(lock["url"])
    if hashlib.sha3_256(archive).hexdigest() != lock["archive_sha3_256"]:
        raise RuntimeSQLiteError("the SQLite archive failed integrity verification")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            library = bundle.read(_LIBRARY)
    except (zipfile.BadZipFile, KeyError) as error:
        raise RuntimeSQLiteError("the SQLite archive has an unexpected layout") from error
    if _sha256(library) != lock["library_sha256"]:
        raise RuntimeSQLiteError("the SQLite library failed integrity verification")
    descriptor, temporary = tempfile.mkstemp(prefix=".sqlite3-", dir=destination.parent)
    staging = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(library)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)
    return True


def _read_lock(path: Path) -> dict[str, str]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeSQLiteError(f"the SQLite lock is unreadable: {path}") from error
    fields = ("version", "url", "archive_sha3_256", "library_sha256")
    if not isinstance(lock, dict) or any(not isinstance(lock.get(key), str) for key in fields):
        raise RuntimeSQLiteError(f"the SQLite lock is malformed: {path}")
    if not lock["url"].startswith("https://sqlite.org/"):
        raise RuntimeSQLiteError("the SQLite lock must point to sqlite.org")
    return {key: lock[key] for key in fields}


def _download(url: str) -> bytes:
    # Mirror the search-engine provisioning: no ambient proxies, normal TLS
    # verification and redirects, and a bounded streamed body.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as response:
        data = bytearray()
        while chunk := response.read(64 * 1024):
            data.extend(chunk)
            if len(data) > _ARCHIVE_LIMIT_BYTES:
                raise RuntimeSQLiteError("the SQLite archive exceeds its allowed size")
    return bytes(data)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def interpreter_problem(source: Path) -> str | None:
    """Return why this interpreter's SQLite differs from the packages', or None.

    Development, tests and measurements must run the databases as installations
    do. On Windows that means the pinned library itself; elsewhere a SQLite
    without the WAL-reset bug, as the Linux packages' standalone runtime has.
    """
    import sqlite3
    import sys

    if sys.platform == "win32":
        pinned = _read_lock(source / SQLITE_LOCK)["version"]
        if sqlite3.sqlite_version == pinned:
            return None
        return (
            f"This Python ({sys.base_prefix}) runs SQLite {sqlite3.sqlite_version}; the Windows "
            f"packages bundle SQLite {pinned}, and the databases' journal mode follows it. "
            "Install the pinned library into it from an elevated terminal: "
            f'"{sys.executable}" -m cli.application.runtime_sqlite'
        )
    from core.database import required_journal_mode

    if required_journal_mode(sqlite3.sqlite_version_info) == "wal":
        return None
    return (
        f"This Python ({sys.executable}) runs SQLite {sqlite3.sqlite_version}, which confines "
        "the databases to the rollback journal; the Linux packages run them with WAL. Use the "
        "packages' Python runtime (scripts/linux/python.lock.json) for development."
    )


def main() -> int:
    """Install the pinned SQLite library into this interpreter's Windows runtime."""
    import sys

    if sys.platform != "win32":
        print("Only Windows runtimes take the pinned SQLite library; see interpreter_problem().")
        return 2
    source = Path(__file__).resolve().parents[2]
    runtime = Path(sys.base_prefix)
    try:
        replaced = provision_runtime_sqlite(runtime, source)
    except PermissionError:
        print(f"{runtime} is not writable; run this command from an elevated terminal.")
        return 1
    except RuntimeSQLiteError as error:
        print(f"The pinned SQLite library could not be installed: {error}")
        return 1
    pinned = _read_lock(source / SQLITE_LOCK)["version"]
    state = "installed" if replaced else "already installed"
    print(f"SQLite {pinned} is {state} in {runtime / 'DLLs'}; new Python processes use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
