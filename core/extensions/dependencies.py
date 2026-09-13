"""Runtime compatibility records for user-managed Extension dependencies.

This module deliberately has no dependency on :mod:`cli`: the server uses it
at startup, while the packaged application layer creates the records.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("extensions.dependencies")
_SCHEMA_VERSION = 1
_MAX_RECORD_BYTES = 1024 * 1024
_NORMALIZE = re.compile(r"[-_.]+")


class DependencyError(ValueError):
    """A managed Extension dependency record is invalid or incompatible."""


@dataclass(frozen=True)
class RuntimeDependencies:
    """The immutable runtime facts that Extension dependencies must match."""

    version_root: Path
    fingerprint: str
    distributions: dict[str, str]


def normalize_distribution(name: str) -> str:
    """Return the canonical package comparison key used by Python packaging."""
    if not isinstance(name, str) or not name or len(name) > 200:
        raise DependencyError("Invalid runtime distribution name")
    return _NORMALIZE.sub("-", name).lower()


def runtime_dependencies(version_root: Path) -> RuntimeDependencies:
    """Read the shipped inventory and immutable Python ABI identity for a version."""
    root = version_root.resolve()
    _safe_tree(root)
    inventory = _read_object(root / "runtime" / "vbot-runtime-inventory.json")
    if inventory.get("schema_version") != _SCHEMA_VERSION or not isinstance(
        inventory.get("packages"), list
    ):
        raise DependencyError("Runtime package inventory is invalid")
    distributions: dict[str, str] = {}
    for item in inventory["packages"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version"), str)
        ):
            raise DependencyError("Runtime package inventory is invalid")
        name = normalize_distribution(item["name"])
        if name in distributions or not item["version"] or len(item["version"]) > 200:
            raise DependencyError("Runtime package inventory has duplicate or invalid packages")
        distributions[name] = item["version"]
    release = _read_object(root / "release.json", limit=32 * 1024**2)
    files = release.get("files")
    if not isinstance(files, dict):
        raise DependencyError("Release manifest is invalid")
    dlls = [
        (name, value)
        for name, value in files.items()
        if isinstance(name, str)
        and name.startswith("runtime/")
        and name.casefold().endswith(".dll")
        and re.fullmatch(r"python\d{2,}t?\.dll", Path(name).name.casefold())
    ]
    if len(dlls) != 1:
        raise DependencyError("Release does not identify one Python runtime DLL")
    dll_name, dll_digest = dlls[0]
    if not isinstance(dll_digest, str) or len(dll_digest) != 64:
        raise DependencyError("Release Python runtime identity is invalid")
    payload = {
        "abi": Path(dll_name).name.casefold(),
        "python_dll_sha256": dll_digest.lower(),
        "packages": sorted(distributions.items()),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return RuntimeDependencies(root, fingerprint, distributions)


def active_record(data_dir: Path, runtime: RuntimeDependencies) -> dict[str, Any] | None:
    """Return the matching active record, refusing a malformed managed record."""
    base = _managed_root(data_dir, runtime.fingerprint)
    path = base / "active.json"
    if not path.exists():
        return None
    record = _read_object(path)
    if (
        record.get("schema_version") != _SCHEMA_VERSION
        or record.get("compatibility") != runtime.fingerprint
    ):
        raise DependencyError("Extension dependency record is incompatible")
    if (
        not isinstance(record.get("requirements"), str)
        or len(record["requirements"].encode()) > _MAX_RECORD_BYTES
    ):
        raise DependencyError("Extension dependency recipe is invalid")
    relative = record.get("site_relative")
    if not isinstance(relative, str):
        raise DependencyError("Extension dependency site is invalid")
    site = _contained(base, relative)
    if site.name != "site" or not site.is_dir():
        raise DependencyError("Extension dependency site is unavailable")
    _safe_tree(site)
    return record


def activate(data_dir: Path) -> None:
    """Append compatible managed dependencies without processing ``.pth`` files.

    A source checkout has neither a release manifest nor the runtime inventory,
    and remains untouched. Invalid optional dependencies are logged and leave
    unrelated Extensions available.
    """
    try:
        executable = Path(sys.executable).resolve()
        runtime_root = executable.parent
        version_root = runtime_root.parent
        if (
            not (version_root / "release.json").is_file()
            or not (runtime_root / "vbot-runtime-inventory.json").is_file()
        ):
            return
        runtime = runtime_dependencies(version_root)
        record = active_record(data_dir, runtime)
        if record is None:
            return
        site = _contained(_managed_root(data_dir, runtime.fingerprint), record["site_relative"])
        location = str(site)
        if location not in sys.path:
            # Do not use site.addsitedir: it would execute user-controlled .pth.
            sys.path.append(location)
    except (DependencyError, OSError, UnicodeError) as exc:
        _LOGGER.warning("Managed Extension dependencies are unavailable: %s", exc)


def _managed_root(data_dir: Path, fingerprint: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DependencyError("Invalid runtime compatibility identity")
    return _contained(data_dir, f"extension-dependencies/{fingerprint}")


def _read_object(path: Path, *, limit: int = _MAX_RECORD_BYTES) -> dict[str, Any]:
    _safe_tree(path.parent)
    try:
        if path.is_symlink() or path.stat().st_size > limit:
            raise DependencyError("Managed dependency record is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyError("Cannot read managed dependency record") from exc
    if not isinstance(value, dict):
        raise DependencyError("Managed dependency record must be an object")
    return value


def _contained(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise DependencyError("Managed dependency path is unsafe")
    path = root / candidate.as_posix()
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            raise DependencyError("Managed dependency path escapes its root")
    except OSError as exc:
        raise DependencyError("Managed dependency path is unavailable") from exc
    _safe_tree(path.parent)
    return path


def _safe_tree(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise DependencyError("Managed dependency paths must not contain links")
        current = current.parent
