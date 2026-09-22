"""Bounded, portable Skill packages. Archive entries are never extracted directly."""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import stat
import tarfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import IO

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 10_000
MAX_SKILL_DOCUMENT_BYTES = 1024 * 1024
INSTALL_RECEIPT = ".vbot-install.json"
EXCLUDED_PARTS = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", INSTALL_RECEIPT})
_DEVICES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{i}" for i in "0123456789¹²³"),
        *(f"lpt{i}" for i in "0123456789¹²³"),
    }
)


class PackageError(ValueError):
    """An unsupported or unsafe package, before publication."""


@dataclass(frozen=True)
class PackageFile:
    content: bytes
    executable: bool = False


def package_path(value: str) -> str:
    """Validate one portable relative path, without collapsing suspicious segments."""
    if not value or len(value) > 1024 or "\\" in value:
        raise PackageError(f"Invalid package path: {value!r}")
    for part in value.split("/"):
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or part.split(".", 1)[0].rstrip(" ").casefold() in _DEVICES
        ):
            raise PackageError(f"Invalid package path: {value!r}")
    return value


def is_redirect(path: Path) -> bool:
    """Include Windows junctions/reparse points on every supported Python version."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def excluded(path: str) -> bool:
    return any(part.casefold() in EXCLUDED_PARTS for part in path.split("/"))


class _Collector:
    def __init__(self) -> None:
        self.files: dict[str, PackageFile] = {}
        self.paths: dict[str, tuple[str, bool]] = {}
        self.total = 0
        self.entries = 0

    def add(self, name: str, size: int, mode: int, stream: IO[bytes] | None) -> None:
        self.entries += 1
        if self.entries > MAX_PACKAGE_ENTRIES:
            raise PackageError(f"Package exceeds {MAX_PACKAGE_ENTRIES} entries.")
        name = package_path(name)
        if excluded(name):
            return
        is_dir = stream is None
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            key = unicodedata.normalize("NFC", path).casefold()
            directory = index < len(parts) or is_dir
            existing = self.paths.get(key)
            if existing is not None and (existing != (path, directory) or not directory):
                raise PackageError(f"Duplicate or conflicting package path: {name!r}")
            self.paths[key] = (path, directory)
        if is_dir:
            return
        if size < 0 or size > MAX_FILE_BYTES or self.total + size > MAX_PACKAGE_BYTES:
            raise PackageError("Package exceeds the file or total uncompressed size limit.")
        assert stream is not None
        content = stream.read(size + 1)
        if len(content) != size:
            raise PackageError(f"Package file size changed or is invalid: {name!r}")
        self.total += size
        self.files[name] = PackageFile(content, bool(mode & 0o111))


def read_directory(root: Path) -> dict[str, PackageFile]:
    """Copy only ordinary files; never follow links or recurse into generated trees."""
    collector = _Collector()
    if is_redirect(root):
        raise PackageError("Skill source must not be a symlink or junction.")

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if excluded(relative):
                    continue
                if is_redirect(path):
                    raise PackageError(f"Skill source contains a symlink or junction: {relative}")
                info = path.stat()
                if stat.S_ISDIR(info.st_mode):
                    collector.add(relative, 0, info.st_mode, None)
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink > 1:
                        raise PackageError(f"Skill source contains a hard link: {relative}")
                    with path.open("rb") as stream:
                        collector.add(relative, info.st_size, info.st_mode, stream)
                else:
                    raise PackageError(f"Skill source contains a special file: {relative}")

    visit(root)
    return collector.files


def _tar_payload(data: bytes) -> bytes:
    # Bound the expanded stream before tarfile parses PAX/long-name metadata, whose
    # declared sizes otherwise bypass per-file checks. Include headers in this bound.
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(MAX_PACKAGE_BYTES + 1)
    elif data.startswith(b"BZh"):
        with bz2.BZ2File(io.BytesIO(data)) as stream:
            data = stream.read(MAX_PACKAGE_BYTES + 1)
    elif data.startswith(b"\xfd7zXZ\x00"):
        decoder = lzma.LZMADecompressor(memlimit=MAX_PACKAGE_BYTES)
        data = decoder.decompress(data, max_length=MAX_PACKAGE_BYTES + 1)
        if len(data) <= MAX_PACKAGE_BYTES and not decoder.eof:
            raise PackageError("Truncated or oversized compressed TAR archive.")
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError("TAR archive exceeds the total uncompressed size limit.")
    return data


def read_archive(data: bytes) -> dict[str, PackageFile]:
    """Read ZIP/.skill or TAR packages with bounded bytes, entries and file types."""
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise PackageError("Skill archive exceeds the 64 MiB download limit.")
    collector = _Collector()
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for item in archive.infolist():
                    # ZipInfo.filename normalizes backslashes on Windows and truncates NULs.
                    # Validate the original spelling before those transformations can hide it.
                    name = item.orig_filename.rstrip("/") if item.is_dir() else item.orig_filename
                    mode = item.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise PackageError(f"Archive contains a link or special file: {name}")
                    if item.flag_bits & 1:
                        raise PackageError("Encrypted Skill archives are unsupported.")
                    if item.is_dir():
                        collector.add(name, 0, mode, None)
                    else:
                        with archive.open(item) as stream:
                            collector.add(name, item.file_size, mode, stream)
        else:
            with tarfile.open(fileobj=io.BytesIO(_tar_payload(data)), mode="r|") as archive:
                for member in archive:
                    name = member.name
                    # TAR commonly spells the archive root and ordinary paths with './'.
                    if name in {".", "./"} and member.isdir():
                        continue
                    if name.startswith("./"):
                        name = name[2:]
                    if member.isdir():
                        collector.add(name.rstrip("/"), 0, member.mode, None)
                    elif member.isfile() and not member.issparse():
                        tar_stream = archive.extractfile(member)
                        assert tar_stream is not None
                        with tar_stream:
                            collector.add(name, member.size, member.mode, tar_stream)
                    else:
                        raise PackageError(f"Archive contains a link or special file: {name}")
    except (
        zipfile.BadZipFile,
        tarfile.TarError,
        RuntimeError,
        EOFError,
        NotImplementedError,
        OSError,
        lzma.LZMAError,
        zlib.error,
    ) as error:
        raise PackageError(
            "Cannot read Skill archive. Use a ZIP/.skill or TAR archive containing SKILL.md; "
            "for a catalog web page, follow its repository or download link."
        ) from error
    if not collector.files:
        raise PackageError("Skill archive contains no files.")
    return collector.files


def unwrap_archive(files: dict[str, PackageFile]) -> dict[str, PackageFile]:
    """Remove a sole enclosing directory, as used by .skill and repository archives."""
    roots = {name.split("/", 1)[0] for name in files}
    if len(roots) == 1 and all("/" in name for name in files):
        return {name.split("/", 1)[1]: value for name, value in files.items()}
    return files


def package_roots(files: dict[str, PackageFile]) -> list[str]:
    """Find outermost Skills; a Skill's nested example packages remain its resources."""
    candidates = sorted(
        name.removesuffix("SKILL.md").rstrip("/")
        for name in files
        if name == "SKILL.md" or name.endswith("/SKILL.md")
    )
    roots: list[str] = []
    for path in candidates:
        if not any(not root or path.startswith(root + "/") for root in roots):
            roots.append(path)
    return roots
