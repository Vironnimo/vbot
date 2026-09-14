"""Authenticated, bounded release extraction into immutable version directories."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from cli.application.state import ApplicationError, Installation, contained, read_json, safe_id

MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_PAYLOAD_BYTES = 12 * 1024**3
MAX_FILES = 100_000


def version_label(manifest: dict[str, Any]) -> str:
    """Distinguish source builds sharing a release version without internal IDs."""
    version = manifest.get("version")
    label = version if isinstance(version, str) and version else "unknown version"
    label = "".join(character for character in label[:100] if character.isprintable())
    revision = manifest.get("revision")
    if (
        isinstance(revision, str)
        and len(revision) == 40
        and all(character in "0123456789abcdef" for character in revision)
    ):
        label += f" ({revision[:8]})"
    return label


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def relative_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in name.split("/"))
    ):
        raise ApplicationError("Unsafe release archive path")
    for part in path.parts:
        if part.split(".")[0].lower() in {
            "con",
            "prn",
            "aux",
            "nul",
            *[f"com{i}" for i in range(1, 10)],
            *[f"lpt{i}" for i in range(1, 10)],
        }:
            raise ApplicationError("Reserved Windows filename in release")
    return path.as_posix()


def verify_signature(archive: Path, signature: bytes, public_key: str) -> None:
    """Sign the archive SHA256 digest; verification never loads an untrusted archive."""
    if not public_key:
        raise ApplicationError("This installation has no trusted release signing key")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(
            base64.b64decode(signature.strip(), validate=True), bytes.fromhex(digest(archive))
        )
    except (ValueError, InvalidSignature) as exc:
        raise ApplicationError("Release signature verification failed") from exc


def validate_release(
    root: Path, *, shape: str | None = None, platform: str | None = None
) -> dict[str, Any]:
    contained(root, "release.json")
    release = read_json(root / "release.json", limit=32 * 1024**2)
    if release.get("schema_version") != 1 or type(release.get("bootstrap_protocol")) is not int:
        raise ApplicationError("Unsupported release manifest")
    if release["bootstrap_protocol"] != 1:
        raise ApplicationError("Release requires an incompatible application bootstrap")
    safe_id(release.get("version_id"))
    if shape is not None and release.get("install_shape") != shape:
        raise ApplicationError("Release does not match the installed application shape")
    if platform is not None and release.get("platform") != platform:
        raise ApplicationError("Release is built for a different platform")
    files = release.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise ApplicationError("Release file inventory is missing or invalid")
    actual: dict[str, Path] = {}
    pending = [root]
    # Inspect each entry once without following links. Re-resolving every ancestor
    # for every file dominated verification time on Windows; hashes remain mandatory.
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ApplicationError("Release payload contains a link or reparse point")
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    actual[path.relative_to(root).as_posix()] = path
                else:
                    raise ApplicationError("Release payload contains special files")
    if set(actual) != set(files) | {"release.json"}:
        raise ApplicationError("Release file inventory does not match its payload")
    folded: set[str] = set()
    for name, expected in files.items():
        if not isinstance(name, str) or not name.startswith(("app/", "runtime/")):
            raise ApplicationError("Unexpected release payload root")
        relative_path(name)
        if name.casefold() in folded:
            raise ApplicationError("Case-colliding release filenames")
        folded.add(name.casefold())
        if not isinstance(expected, str) or len(expected) != 64 or digest(actual[name]) != expected:
            raise ApplicationError(f"Release file verification failed: {name}")
    required = {"app/cli/main.py", "app/pyproject.toml"}
    if release.get("install_shape") != "desktop-client":
        required |= {"app/server/main.py", "app/webui/dist/index.html"}
    if not required <= set(files):
        raise ApplicationError("Release is missing required application entrypoints")
    return release


def stage_package(install: Installation, archive: Path, *, local: bool = False) -> str:
    archive = archive.expanduser().resolve()
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ApplicationError("Release archive exceeds the size limit")
    if not local:
        signature_path = archive.with_suffix(archive.suffix + ".sig")
        if not signature_path.is_file() or signature_path.stat().st_size > 1024:
            raise ApplicationError("Release signature is missing or invalid")
        verify_signature(archive, signature_path.read_bytes(), install.release_public_key)
    staging = contained(install.root, "staging")
    staging.mkdir(parents=True, exist_ok=True)
    import tempfile

    temporary = Path(tempfile.mkdtemp(prefix="release-", dir=staging))
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if (
                len(entries) > MAX_FILES
                or sum(item.file_size for item in entries) > MAX_PAYLOAD_BYTES
            ):
                raise ApplicationError("Release payload exceeds extraction limits")
            seen: set[str] = set()
            for item in entries:
                name = relative_path(item.filename.rstrip("/") if item.is_dir() else item.filename)
                if name.casefold() in seen:
                    raise ApplicationError("Duplicate release archive entry")
                seen.add(name.casefold())
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ApplicationError("Release archive contains special files")
                if item.flag_bits & 1:
                    raise ApplicationError("Encrypted release entries are not supported")
                target = contained(temporary, name)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if os.name != "nt" and mode & 0o111:
                    target.chmod(0o755)
        manifest = validate_release(
            temporary,
            shape=install.install_shape,
            platform="windows-x86_64" if os.name == "nt" else None,
        )
        version_id = manifest["version_id"]
        destination = install.version(version_id)
        if destination.exists():
            old = validate_release(destination, shape=install.install_shape)
            if old != manifest:
                raise ApplicationError("A different payload already uses this release identity")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.rename(destination)
        return str(version_id)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ApplicationError("Could not stage application package") from exc
    finally:
        if temporary.exists():
            # This path was created exclusively under our validated staging root.
            contained(staging, temporary.name)
            shutil.rmtree(temporary)


def download_release(
    install: Installation,
    operation_id: str,
    *,
    progress: Callable[[str, str | None], None] | None = None,
) -> Path:
    if not install.release_public_key:
        raise ApplicationError("Official updates require a configured release signing key")
    directory = contained(install.root, f"downloads/{safe_id(operation_id)}")
    directory.mkdir(parents=True, exist_ok=True)
    expected = f"vbot-windows-x86_64-{install.install_shape}.zip"
    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
        response = client.get(
            install.release_url, headers={"Accept": "application/vnd.github+json"}
        )
        response.raise_for_status()
        release = response.json()
        if progress:
            progress(
                "Downloading the application package",
                version_label({"version": release.get("tag_name")}),
            )
        assets = {item["name"]: item["browser_download_url"] for item in release.get("assets", [])}
        for name in (expected, expected + ".sig"):
            url = assets.get(name)
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ApplicationError("No matching signed application package in this release")
            limit = 1024 if name.endswith(".sig") else MAX_ARCHIVE_BYTES
            target = directory / name
            count = 0
            with client.stream("GET", url) as stream, target.open("wb") as output:
                stream.raise_for_status()
                if stream.url.scheme != "https":
                    raise ApplicationError("Release download redirected to an insecure URL")
                for chunk in stream.iter_bytes():
                    count += len(chunk)
                    if count > limit:
                        raise ApplicationError("Release download exceeds the size limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    return directory / expected
