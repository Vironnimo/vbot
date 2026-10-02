"""Authenticated, bounded release extraction into immutable version directories."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

import httpx

from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    current_platform,
    package_name,
    read_json,
    safe_id,
)

MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_PAYLOAD_BYTES = 12 * 1024**3
MAX_FILES = 100_000
#: Published beside the packages of a release; names the version they contain so an
#: update can skip a download when that version is already active.
RELEASE_IDENTITY_ASSET = "vbot-release.json"
#: Every installed updater refuses a larger identity; ``scripts/release_assets.py``
#: keeps the published one, asset digests included, within it.
MAX_IDENTITY_BYTES = 4096
# CPython's bytecode cache files and the temporaries of its atomic cache writes.
_BYTECODE_CACHE = re.compile(r"[^/]+\.pyc(?:\.[0-9]+)?")
#: Concurrent readers for payload files. Windows scans every newly written file
#: on its first open; overlapping those scans cuts hashing a fresh 11k-file
#: version from about 45 s to 7 s. Reads and hashing release the GIL.
FILE_WORKERS = 16
_LOGGER = logging.getLogger("vbot.application.packages")
_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


def version_label(manifest: dict[str, Any]) -> str:
    """Distinguish builds sharing a release version, such as main builds, by revision."""
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


def _concurrently(function: Callable[[_Item], _Result], items: Iterable[_Item]) -> list[_Result]:
    """Apply ``function`` to payload files concurrently; the first failure is raised."""
    items = list(items)
    if len(items) < 2:
        return [function(item) for item in items]
    pool = ThreadPoolExecutor(max_workers=min(FILE_WORKERS, len(items)))
    try:
        return list(pool.map(function, items))
    finally:
        # After a failure, queued files are skipped instead of processed.
        pool.shutdown(cancel_futures=True)


def digest_files(paths: Iterable[Path]) -> list[str]:
    """Return the SHA256 digests of ``paths`` in order."""
    return _concurrently(digest, paths)


def copy_files(pairs: Iterable[tuple[Path, Path]]) -> None:
    """Copy each ``(source, destination)`` file with its metadata; parents must exist."""
    _concurrently(lambda pair: shutil.copy2(*pair), pairs)


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


def _shown(names: set[str]) -> str:
    listed = ", ".join(
        "".join(character if character.isprintable() else "?" for character in name[:160])
        for name in sorted(names)[:3]
    )
    return listed + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def _remove_bytecode_caches(root: Path, names: list[str]) -> None:
    """Delete caches a plain interpreter wrote; -B hosts would still load them."""
    for name in names:
        try:
            (root / name).unlink()
        except OSError as exc:
            raise ApplicationError(
                f"Could not remove the unverified bytecode cache {name} from the installed "
                "version; close programs that use this vBot version and retry"
            ) from exc
    for directory in {(root / name).parent for name in names}:
        with contextlib.suppress(OSError):
            directory.rmdir()
    _LOGGER.warning(
        "Removed %d unverified bytecode cache files from %s, first %s",
        len(names),
        root,
        min(names),
    )


def validate_release(
    root: Path,
    *,
    shape: str | None = None,
    platform: str | None = None,
    remove_bytecode_caches: bool = False,
) -> dict[str, Any]:
    """Verify a release's exact payload against its manifest inventory.

    Installed versions opt into removing `__pycache__` bytecode absent from the
    inventory, which running their python.exe directly writes; payloads being
    built or staged stay exact.
    """
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
    caches: list[str] = []
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
                    name = path.relative_to(root).as_posix()
                    if (
                        remove_bytecode_caches
                        and name not in files
                        and path.parent.name == "__pycache__"
                        and _BYTECODE_CACHE.fullmatch(entry.name)
                    ):
                        caches.append(name)
                    else:
                        actual[name] = path
                else:
                    raise ApplicationError("Release payload contains special files")
    if caches:
        _remove_bytecode_caches(root, caches)
    expected_names = set(files) | {"release.json"}
    if set(actual) != expected_names:
        mismatch = [
            f"{label}: {_shown(names)}"
            for label, names in (
                ("unexpected", set(actual) - expected_names),
                ("missing", expected_names - set(actual)),
            )
            if names
        ]
        raise ApplicationError(
            f"Release file inventory does not match its payload ({'; '.join(mismatch)})"
        )
    folded: set[str] = set()
    for name, expected in files.items():
        if not isinstance(name, str) or not name.startswith(("app/", "runtime/")):
            raise ApplicationError("Unexpected release payload root")
        relative_path(name)
        if name.casefold() in folded:
            raise ApplicationError("Case-colliding release filenames")
        folded.add(name.casefold())
        if not isinstance(expected, str) or len(expected) != 64:
            raise ApplicationError(f"Release file verification failed: {name}")
    digests = digest_files(actual[name] for name in files)
    for name, actual_digest in zip(files, digests, strict=True):
        if actual_digest != files[name]:
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
            temporary, shape=install.install_shape, platform=current_platform()
        )
        version_id = manifest["version_id"]
        destination = install.version(version_id)
        if destination.exists():
            old = validate_release(
                destination, shape=install.install_shape, remove_bytecode_caches=True
            )
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
) -> Path | None:
    """Download the signed package of the installation's channel.

    Reads the channel's release identity, then the package and its signature,
    all from the channel's release downloads; it never queries the GitHub API,
    whose anonymous request limit shared IP addresses exhaust. Returns ``None``
    without downloading when the channel publishes the version that is already
    active.
    """
    if not install.release_public_key:
        raise ApplicationError("Official updates require a configured release signing key")
    directory = contained(install.root, f"downloads/{safe_id(operation_id)}")
    expected = package_name(install.install_shape)
    base = install.download_base
    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
        identity = _release_identity(client, f"{base}/{RELEASE_IDENTITY_ASSET}")
        label = version_label(identity)
        if identity["version_id"] == install.version().name:
            if progress:
                progress("The published version is already installed", label)
            return None
        if progress:
            progress("Downloading the application package", label)
        directory.mkdir(parents=True, exist_ok=True)
        for name in (expected, expected + ".sig"):
            limit = 1024 if name.endswith(".sig") else MAX_ARCHIVE_BYTES
            _download(client, f"{base}/{name}", directory / name, limit)
    return directory / expected


def _release_identity(client: httpx.Client, url: str) -> dict[str, Any]:
    """Read the version identity every release publishes beside its packages."""
    response = client.get(url)
    if response.status_code == 404:
        raise ApplicationError(
            f"The channel's release publishes no {RELEASE_IDENTITY_ASSET} version identity, "
            "so vbot update cannot install it"
        )
    response.raise_for_status()
    if response.url.scheme != "https":
        raise ApplicationError("Release download redirected to an insecure URL")
    if len(response.content) > MAX_IDENTITY_BYTES:
        raise ApplicationError("Release identity exceeds the size limit")
    try:
        value = response.json()
    except ValueError as exc:
        raise ApplicationError("Release identity is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ApplicationError("Unsupported release identity")
    safe_id(value.get("version_id"))
    return value


def _download(client: httpx.Client, url: str, target: Path, limit: int) -> None:
    count = 0
    with client.stream("GET", url) as stream:
        if stream.status_code == 404:
            raise ApplicationError(
                f"No matching signed application package in this release ({target.name})"
            )
        stream.raise_for_status()
        if stream.url.scheme != "https":
            raise ApplicationError("Release download redirected to an insecure URL")
        with target.open("wb") as output:
            for chunk in stream.iter_bytes():
                count += len(chunk)
                if count > limit:
                    raise ApplicationError("Release download exceeds the size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
