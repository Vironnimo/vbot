"""Locate and verify bundled ripgrep without installing it during a Run."""

from __future__ import annotations

import hashlib
import json
import platform
import stat
import time
from functools import lru_cache
from os import stat_result
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parents[2] / "resources"


def target_platform() -> str:
    machine = platform.machine().lower()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "armv7l": "armv7",
    }.get(machine)
    targets = {
        ("Windows", "x86_64"): "x86_64-pc-windows-msvc",
        ("Windows", "aarch64"): "aarch64-pc-windows-msvc",
        ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
        ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("Linux", "armv7"): "armv7-unknown-linux-gnueabihf",
    }
    target = targets.get((platform.system(), architecture or ""))
    if target is None:
        raise ValueError(f"No bundled search engine for {platform.system()} {machine}.")
    return target


def binary_spec(
    root: Path = RESOURCE_ROOT, target: str | None = None
) -> tuple[Path, dict[str, str]]:
    lock = root / "ripgrep.lock.json"
    identity = _identity(lock.stat())
    read = _locked_spec if identity is not None else _locked_spec.__wrapped__
    path, artifact = read(lock, target or target_platform(), identity)
    return path, artifact.copy()


type _FileIdentity = tuple[int, int, int, int] | None


def _identity(info: stat_result) -> _FileIdentity:
    # A coarse filesystem timestamp can cover several writes of the same size.
    # Until its tick is past, verify the current bytes on every call.
    if max(info.st_mtime_ns, info.st_ctime_ns) >= time.time_ns() - 2_000_000_000:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@lru_cache(maxsize=16)
def _locked_spec(lock: Path, selected: str, identity: _FileIdentity) -> tuple[Path, dict[str, str]]:
    """Read a release's lock once, reloading if a checkout or package replaces it."""
    del identity
    manifest = json.loads(lock.read_text(encoding="utf-8"))
    artifact = manifest["artifacts"][selected]
    name = "rg.exe" if "windows" in selected else "rg"
    path = lock.parent / "native" / "ripgrep" / manifest["version"] / selected / name
    return path, artifact


@lru_cache(maxsize=16)
def _verified(path: Path, identity: _FileIdentity, expected: str) -> bool:
    del identity
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == expected


def require_binary(root: Path = RESOURCE_ROOT) -> Path:
    try:
        path, artifact = binary_spec(root)
        info = path.lstat()
        identity = _identity(info)
        verify = _verified if identity is not None else _verified.__wrapped__
        if not stat.S_ISREG(info.st_mode) or not verify(path, identity, artifact["binary_sha256"]):
            raise ValueError("Bundled ripgrep failed integrity verification.")
        return path
    except (OSError, KeyError, ValueError) as error:
        raise ValueError(
            "The bundled search engine is unavailable. Repair the vBot installation; "
            "in a development checkout run python -m cli.search_runtime. " + str(error)
        ) from error
