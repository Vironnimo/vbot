"""Authenticated, bounded release extraction into immutable version directories."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from cli.application.state import (
    CHANNEL_DOWNLOADS,
    RELEASE_TAG_DOWNLOADS,
    ApplicationError,
    Installation,
    contained,
    current_platform,
    normalize_release_version,
    package_name,
    read_json,
    safe_id,
)
from core.access.config import AccessConfig
from core.utils.atomic import atomic_write_text
from core.utils.file_status import is_link_status
from core.utils.ids import is_reserved_name

if TYPE_CHECKING:
    import httpx

MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_PAYLOAD_BYTES = 12 * 1024**3
MAX_FILES = 100_000
#: The persisted Access Mode and request-authentication contract this updater admits.
SERVER_ACCESS_PROTOCOL = 1
#: Shared startup and rollback contract, present from the first supported target.
VERSION_SWITCH_PROTOCOL = 1
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
# The numeric release part of a version (`0.4.25` of `0.4.25` or `1.1.0.dev3`).
_RELEASE_NUMBERS = re.compile(r"[0-9]+(?:\.[0-9]+)*")


class OlderReleaseError(ApplicationError):
    """The update channel offers a version that is not newer than the active one."""


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


def _concurrently[Item, Result](
    function: Callable[[Item], Result], items: Iterable[Item]
) -> list[Result]:
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
    # Device names such as CON, COM0 or COM¹, also with an extension or trailing
    # spaces ("nul .txt"), and characters Windows cannot store.
    for part in path.parts:
        if is_reserved_name(part):
            raise ApplicationError(f"Reserved Windows filename in release: {part!r}")
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


def require_access_compatible_release(release: dict[str, Any], access: AccessConfig | None) -> None:
    """Refuse a new package that cannot guarantee the installation's login boundary."""
    if access is None or access.mode != "authenticated":
        return
    protocol = release.get("server_access_protocol")
    if type(protocol) is not int or protocol != SERVER_ACCESS_PROTOCOL:
        raise ApplicationError(
            "This package does not declare compatible server authentication support. "
            "It cannot replace a server in authenticated access mode. "
            "Choose a package that supports authenticated access."
        )


def require_version_switch_release(release: dict[str, Any]) -> None:
    """Admit only targets that implement the shared version-switch contract."""
    protocol = release.get("version_switch_protocol")
    if type(protocol) is not int or protocol != VERSION_SWITCH_PROTOCOL:
        raise ApplicationError(
            "This version does not support compatible version switching. "
            "Choose a release that declares version-switch support."
        )


def _require_selected_release(release: dict[str, Any], version: str) -> None:
    require_version_switch_release(release)
    if release.get("channel") != "release" or release.get("version") != version:
        raise ApplicationError(f"The package does not contain the selected release {version}.")


def _require_matching_identity(release: dict[str, Any], identity: dict[str, Any]) -> None:
    if any(
        release.get(name) != identity.get(name)
        for name in ("version_id", "version", "revision", "channel")
    ):
        raise ApplicationError("The signed package does not match its release identity.")


def validate_release(
    root: Path,
    *,
    shape: str | None = None,
    platform: str | None = None,
    remove_bytecode_caches: bool = False,
    access: AccessConfig | None = None,
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
    require_access_compatible_release(release, access)
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
                # Cloud-file placeholders are reparse points too, but ordinary files.
                if is_link_status(info):
                    raise ApplicationError("Release payload contains a link")
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


def stage_package(
    install: Installation,
    archive: Path,
    *,
    local: bool = False,
    release_version: str | None = None,
) -> str:
    """Install the package *archive* as a version and return its version id.

    A directory instead names a version this installation already holds, the
    prepared update ``update activate`` selects: it is verified in place.
    """
    if release_version is not None:
        release_version = normalize_release_version(release_version)
    archive = archive.expanduser().resolve()
    if archive.is_dir():
        return _verify_installed_version(install, archive, release_version=release_version)
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
            platform=current_platform(),
            access=install.access,
        )
        if release_version is not None:
            _require_selected_release(manifest, release_version)
        selected_channel = "release" if release_version is not None else install.channel
        if not local and manifest.get("channel") != selected_channel:
            raise ApplicationError(
                f"Release package does not match the selected update channel '{selected_channel}'."
            )
        if not local:
            identity_path = archive.parent / RELEASE_IDENTITY_ASSET
            try:
                identity = read_json(identity_path, limit=MAX_IDENTITY_BYTES)
            except ApplicationError as exc:
                if release_version is not None or not isinstance(exc.__cause__, FileNotFoundError):
                    raise
            else:
                _require_matching_identity(manifest, identity)
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


def _verify_installed_version(
    install: Installation, version: Path, *, release_version: str | None = None
) -> str:
    """Verify an installed version exactly as staging its package would."""

    if version.parent != contained(install.root, "versions").resolve():
        raise ApplicationError("The selected application package is not an installed version")
    manifest = validate_release(
        version,
        shape=install.install_shape,
        platform=current_platform(),
        access=install.access,
        remove_bytecode_caches=True,
    )
    if release_version is not None:
        _require_selected_release(manifest, release_version)
    version_id = str(manifest["version_id"])
    if install.version(version_id).resolve() != version:
        raise ApplicationError("The installed version does not match its release identity")
    return version_id


def download_release(
    install: Installation,
    operation_id: str,
    *,
    progress: Callable[[str, str | None], None] | None = None,
    release_version: str | None = None,
) -> Path | None:
    """Download the channel's package, or an explicitly selected published release.

    Reads the channel's release identity, then the package and its signature
    from the release it names, all from release downloads; it never queries the
    GitHub API, whose anonymous request limit shared IP addresses exhaust.
    Returns ``None`` without downloading when the channel publishes the version
    that is already active, and raises ``OlderReleaseError`` without downloading
    when it publishes one that is not newer (``_not_newer``).
    An explicit version uses its release tag independently of the update channel
    and can go back, but only to a release with version-switch support.
    """
    if release_version is not None:
        release_version = normalize_release_version(release_version)
    if not install.release_public_key:
        raise ApplicationError("Official updates require a configured release signing key")
    # Imported here: the CLI parser loads this module through `install`, and help
    # must not load httpx.
    import httpx

    directory = contained(install.root, f"downloads/{safe_id(operation_id)}")
    expected = package_name(install.install_shape)
    selected_channel = "release" if release_version is not None else install.channel
    selected_base = (
        f"{RELEASE_TAG_DOWNLOADS}/v{release_version}"
        if release_version is not None
        else install.download_base
    )
    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
        identity = _release_identity(client, f"{selected_base}/{RELEASE_IDENTITY_ASSET}")
        if identity.get("channel") != selected_channel:
            raise ApplicationError(
                f"Release identity does not match the selected update channel '{selected_channel}'."
            )
        if release_version is not None:
            _require_selected_release(identity, release_version)
        label = version_label(identity)
        if identity["version_id"] == install.version().name:
            if release_version is not None:
                active = read_json(install.version() / "release.json", limit=32 * 1024**2)
                _require_selected_release(active, release_version)
                _require_matching_identity(active, identity)
            if progress:
                progress("The published version is already installed", label)
            return None
        active = read_json(install.version() / "release.json", limit=32 * 1024**2)
        if release_version is None and _not_newer(identity, active):
            raise OlderReleaseError(_older_release_message(install, identity, active))
        base = (
            selected_base
            if release_version is not None
            else install.asset_base(identity.get("version"))
        )
        if progress:
            progress("Downloading the application package", label)
        directory.mkdir(parents=True, exist_ok=True)
        # Keep the bounded wire document compact: pretty-printing an accepted
        # identity could exceed the same limit when staging reads it back.
        atomic_write_text(
            directory / RELEASE_IDENTITY_ASSET,
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
        )
        for name in (expected, expected + ".sig"):
            limit = 1024 if name.endswith(".sig") else MAX_ARCHIVE_BYTES
            _download(client, f"{base}/{name}", directory / name, limit)
    return directory / expected


def _release_numbers(manifest: dict[str, Any]) -> tuple[int, ...] | None:
    version = manifest.get("version")
    match = _RELEASE_NUMBERS.match(version) if isinstance(version, str) else None
    if match is None:
        return None
    numbers = [int(part) for part in match.group().split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def _not_newer(offered: dict[str, Any], active: dict[str, Any]) -> bool:
    """Whether the channel's *offered* build would take the *active* one back.

    Versions order by their numeric release part. A version's main builds start
    with the commit that bumps the version for its release, so a release build
    of the version a main build carries is the earlier build (the very same
    build has the same version id and never gets here). Versions that do not
    parse are not ordered.
    """
    offered_numbers = _release_numbers(offered)
    active_numbers = _release_numbers(active)
    if offered_numbers is None or active_numbers is None:
        return False
    if offered_numbers != active_numbers:
        return offered_numbers < active_numbers
    return offered.get("channel") == "release" and active.get("channel") == "main"


def _older_release_message(
    install: Installation, offered: dict[str, Any], active: dict[str, Any]
) -> str:
    active_channel = active.get("channel")
    back = (
        f"Switch back with vbot application channel {active_channel} to keep receiving updates. "
        if active_channel in CHANNEL_DOWNLOADS and active_channel != install.channel
        else "Wait for a newer version. "
    )
    version = offered.get("version")
    deliberate = (
        f"To select this release explicitly, run vbot update {version}."
        if install.channel == "release"
        and isinstance(version, str)
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        else "To select a published release explicitly, see vbot update --help."
    )
    return (
        f"The {install.channel} channel offers vBot {version_label(offered)}, which is not newer "
        f"than the active vBot {version_label(active)}. An update without an explicit version "
        f"does not go back to an older version. {back}{deliberate}"
    )


def _release_identity(client: httpx.Client, url: str) -> dict[str, Any]:
    """Read the version identity every release publishes beside its packages."""
    content = bytearray()
    with client.stream("GET", url) as response:
        if response.status_code == 404:
            raise ApplicationError(
                f"The channel's release publishes no {RELEASE_IDENTITY_ASSET} version identity, "
                "so vbot update cannot install it"
            )
        response.raise_for_status()
        if response.url.scheme != "https":
            raise ApplicationError("Release download redirected to an insecure URL")
        for chunk in response.iter_bytes(chunk_size=MAX_IDENTITY_BYTES + 1):
            if len(content) + len(chunk) > MAX_IDENTITY_BYTES:
                raise ApplicationError("Release identity exceeds the size limit")
            content.extend(chunk)
    try:
        value = json.loads(content)
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
