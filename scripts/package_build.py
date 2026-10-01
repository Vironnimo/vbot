"""Release packaging shared by the Windows and Linux package builders.

A package is one version directory (``app/`` sources, ``runtime/`` private
CPython with dependencies, ``release.json`` inventory) zipped with paths
relative to that directory, optionally signed, plus the ``vbot-release.json``
identity that tells updaters which version a release publishes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.application.payload import PayloadError
from core.utils.processes import subprocess_creation_flags

BuildError = PayloadError

CHANNELS = ("release", "main")
INVENTORY_NAME = "vbot-runtime-inventory.json"
RELEASE_IDENTITY_NAME = "vbot-release.json"
#: Tracked sources whose state a signed package must match exactly.
RELEASE_SOURCE_PATHS = (
    "core",
    "server",
    "cli",
    "desktop",
    "resources",
    "pyproject.toml",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "scripts/package_build.py",
    "scripts/build_windows.py",
    "scripts/build_linux.py",
    "scripts/windows",
    "scripts/linux",
)


def version_id(version: str, revision: str) -> str:
    clean_version = re.sub(r"[^a-z0-9]+", "_", version.lower()).strip("_")
    clean_revision = re.sub(r"[^a-z0-9]", "", revision.lower())[:12]
    value = f"v{clean_version}_{clean_revision}"
    if not clean_version or not clean_revision or len(value) > 128:
        raise BuildError("version and revision must form a safe application version id")
    return value


def dependency_inventory(site: Path) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    if not site.is_dir():
        return packages
    for metadata in sorted(site.glob("*.dist-info"), key=lambda item: item.name.casefold()):
        name = version = None
        metadata_file = metadata / "METADATA"
        if metadata_file.is_file():
            for line in metadata_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Version: "):
                    version = line[9:].strip()
                if name and version:
                    break
        if name and version:
            packages.append({"name": name, "version": version})
    return packages


def write_inventory(runtime: Path, site: Path) -> None:
    (runtime / INVENTORY_NAME).write_text(
        json.dumps({"schema_version": 1, "packages": dependency_inventory(site)}, indent=2) + "\n",
        encoding="utf-8",
    )


def verify_release_source(source: Path, revision: str) -> None:
    """Bind a signed artifact to the exact clean tracked source revision."""
    head = _git(source, "rev-parse", "HEAD")
    if head.returncode or revision.lower() != head.stdout.strip().lower():
        raise BuildError("release revision must equal the source checkout HEAD")
    status = _git(
        source, "status", "--porcelain", "--untracked-files=all", "--", *RELEASE_SOURCE_PATHS
    )
    if status.returncode or status.stdout.strip():
        raise BuildError("release mode requires clean tracked application sources")


def _git(source: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=subprocess_creation_flags(),
    )


def remove_bytecode_caches(root: Path) -> None:
    """Drop bytecode a build step wrote; packaged hosts never write or need it."""
    for directory in root.rglob("__pycache__"):
        if directory.is_dir():
            shutil.rmtree(directory)
    for suffix in ("*.pyc", "*.pyo"):
        for path in root.rglob(suffix):
            path.unlink()


def file_hashes(version_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    seen: set[str] = set()
    for base in (version_root / "app", version_root / "runtime"):
        for path in sorted(base.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if path.is_symlink():
                raise BuildError(f"payload contains a link: {path.relative_to(version_root)}")
            if not path.is_file():
                continue
            relative = path.relative_to(version_root).as_posix()
            folded = relative.casefold()
            if folded in seen:
                raise BuildError(f"case-colliding payload path: {relative}")
            seen.add(folded)
            values[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def write_manifest(
    version_root: Path,
    *,
    version: str,
    revision: str,
    platform: str,
    shape: str,
    channel: str,
) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise BuildError(f"unknown update channel: {channel}")
    manifest = {
        "schema_version": 1,
        "bootstrap_protocol": 1,
        "version_id": version_root.name,
        "version": version,
        "revision": revision,
        "platform": platform,
        "install_shape": shape,
        "channel": channel,
        "files": file_hashes(version_root),
    }
    (version_root / "release.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def write_archive(version_root: Path, archive: Path) -> None:
    """Zip a version with paths relative to it; Unix modes survive for executables."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(version_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if path.is_file():
                bundle.write(path, path.relative_to(version_root).as_posix())


def write_release_identity(artifacts: Path, manifest: dict[str, Any]) -> None:
    identity = {
        "schema_version": 1,
        **{name: manifest[name] for name in ("version_id", "version", "revision", "channel")},
    }
    (artifacts / RELEASE_IDENTITY_NAME).write_text(
        json.dumps(identity, indent=2) + "\n", encoding="utf-8"
    )


def sign_archive(
    archive: Path,
    key_environment: str,
    *,
    fallback: Callable[[Path, Path, str], str] | None = None,
) -> str:
    """Write ``<archive>.sig`` over the raw SHA256 digest; return the public key."""
    encoded_key = os.environ.get(key_environment)
    if not encoded_key:
        raise BuildError(f"release signing requires the key environment {key_environment}")
    signature = archive.with_suffix(archive.suffix + ".sig")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        if fallback is None:
            raise BuildError("release signing requires the cryptography package") from None
        public_key = fallback(archive, signature, key_environment)
    else:
        try:
            key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded_key, validate=True))
        except ValueError as exc:
            raise BuildError(
                "release signing key must be a base64 raw Ed25519 private key"
            ) from exc
        digest = hashlib.sha256(archive.read_bytes()).digest()
        signature.write_text(
            base64.b64encode(key.sign(digest)).decode("ascii") + "\n", encoding="ascii"
        )
        public_key = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode("ascii")
    if not public_key:
        raise BuildError("release signing key must be a base64 raw Ed25519 private key")
    return public_key
