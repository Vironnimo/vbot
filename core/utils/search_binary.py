"""Locate and verify the private search executable; never install during a Run."""

from __future__ import annotations

import hashlib
import json
import platform
from functools import lru_cache
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
    manifest = json.loads((root / "ripgrep.lock.json").read_text(encoding="utf-8"))
    selected = target or target_platform()
    artifact = manifest["artifacts"][selected]
    name = "rg.exe" if "windows" in selected else "rg"
    path = root / "native" / "ripgrep" / manifest["version"] / selected / name
    return path, artifact


@lru_cache(maxsize=16)
def _verified(path: str, modified: int, size: int, expected: str) -> bool:
    del modified, size
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == expected


def require_binary(root: Path = RESOURCE_ROOT) -> Path:
    try:
        path, artifact = binary_spec(root)
        info = path.stat()
        if path.is_symlink() or not _verified(
            str(path), info.st_mtime_ns, info.st_size, artifact["binary_sha256"]
        ):
            raise ValueError("The private ripgrep executable failed integrity verification.")
        return path
    except (OSError, KeyError, ValueError) as error:
        raise ValueError(
            "The bundled search engine is unavailable. Repair the vBot installation; "
            "in a source checkout run python -m cli.search_runtime. " + str(error)
        ) from error
