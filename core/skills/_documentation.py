"""Materialize Extension usage guides into the ordinary bundled vbot-docs package."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from core.extensions import ExtensionRecord
from core.skills._packages import (
    MAX_FILE_BYTES,
    MAX_PACKAGE_BYTES,
    PackageError,
    PackageFile,
    excluded,
    package_path,
    read_directory,
)
from core.utils.file_status import is_file_strict, is_link_status, stat_or_none
from core.utils.tree_move import remove_tree

_INDEX = "references/extension-index.md"
_GUIDES = "references/extensions"


def compose_vbot_docs(
    source: Path, cache_root: Path, records: Sequence[ExtensionRecord], *, bootstrap: bool = False
) -> Path | None:
    """Build an immutable, read-only-by-policy Skill package before registry publication.

    Only this bundled package is composed. Extension code and undeclared files
    are never copied. Old generations survive reloads so existing readers can
    finish. Bootstrap verifies any retained generation before admitting readers.
    """
    if not is_file_strict(source / "SKILL.md"):
        if bootstrap:
            clear_documentation_cache(cache_root)
        return None
    files = read_directory(source)
    occupied = {unicodedata.normalize("NFC", name).casefold() for name in files}
    total_bytes = sum(len(file.content) for file in files.values())
    lines = [
        "# Extension usage guides",
        "",
        "Read the guide for your task with the skill Tool, name `vbot-docs`, and "
        "the file path displayed in its link.",
        "Disabled or failed Extensions keep their guides for setup and troubleshooting; "
        "their commands can be unavailable until the Extension loads successfully.",
        "",
    ]
    names: set[str] = set()
    for record in sorted(records, key=lambda item: item.name):
        if record.status == "overridden" or record.name in names:
            continue
        names.add(record.name)
        manifest = record.manifest
        lines.extend([f"## {_plain(record.name)}", "", f"Status: {record.status}.", ""])
        if manifest is not None and manifest.description:
            lines.extend([_plain(manifest.description), ""])
        if manifest is None or not manifest.documentation:
            lines.extend(["No usage guides declared.", ""])
            continue
        for document in manifest.documentation:
            lines.append(f"- **{_plain(document.title)}**: {_plain(document.description)}")
            target = f"{_GUIDES}/{record.name}/{document.path}"
            try:
                package_path(target)
                if excluded(target):
                    raise PackageError("The document uses an excluded Skill resource path")
                content = _read_document(record.root_path, document.path)
                key = unicodedata.normalize("NFC", target).casefold()
                if any(
                    key == existing
                    or key.startswith(existing + "/")
                    or existing.startswith(key + "/")
                    for existing in occupied
                ):
                    raise PackageError("The document conflicts with a bundled reference")
                if total_bytes + len(content) > MAX_PACKAGE_BYTES:
                    raise PackageError("The documentation package exceeds its size limit")
                files[target] = PackageFile(content)
                occupied.add(key)
                total_bytes += len(content)
            except (OSError, ValueError) as error:
                lines.append(f"  Unavailable: {_read_failure(error)}.")
            else:
                # The label is the exact Skill file_path; the href also works
                # when a human opens the Markdown index from its own directory.
                href = quote(target.removeprefix("references/"), safe="/")
                lines.append(f"  [{_path_label(target)}]({href})")
        lines.append("")
    if not names:
        lines.extend(["No Extensions discovered.", ""])
    files[_INDEX] = PackageFile("\n".join(lines).encode("utf-8"))
    package = _publish(cache_root, files, bootstrap=bootstrap)
    if bootstrap:
        clear_documentation_cache(cache_root, keep=package.parent)
    return package


def _plain(value: str) -> str:
    """Keep manifest labels on one line and escape Markdown delimiters."""
    # Filesystem-derived Extension names can contain surrogate-escaped bytes on
    # POSIX even though manifest text has already passed UTF-8 validation.
    value = value.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", " ".join(value.split()))


def _path_label(value: str) -> str:
    """An exact copyable Skill path, including spaces and Markdown punctuation."""
    delimiter = "`" * (1 + max((len(part) for part in re.findall(r"`+", value)), default=0))
    return f"{delimiter}{value}{delimiter}"


def _read_document(root: Path, relative: str) -> bytes:
    package_path(relative)
    candidate = root
    for part in ("", *relative.split("/")):
        if part:
            candidate /= part
        info = candidate.lstat()
        if is_link_status(info):
            raise PackageError("The document crosses a symbolic link or junction")
    if not stat.S_ISREG(info.st_mode):
        raise PackageError("The document is not a regular file")
    with candidate.open("rb") as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise PackageError("The document exceeds its size limit")
    # Decode before publication: ordinary Skill reads promise UTF-8 text.
    content.decode("utf-8")
    return content


def _read_failure(error: OSError | ValueError) -> str:
    if isinstance(error, UnicodeError):
        return "the document is not UTF-8 text"
    if isinstance(error, FileNotFoundError):
        return "the document is missing"
    if isinstance(error, PermissionError):
        return "access to the document was denied"
    if isinstance(error, PackageError):
        # Our own messages contain no source content or absolute host paths.
        return str(error)
    return "the document could not be read"


def _cache_directory(cache_root: Path) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    if is_link_status(cache_root.lstat()) or not stat.S_ISDIR(cache_root.stat().st_mode):
        raise OSError("The Skill documentation cache is not a regular directory")


def clear_documentation_cache(cache_root: Path, *, keep: Path | None = None) -> None:
    """Discard only our generated trees at bootstrap, before Skills have readers."""
    if stat_or_none(cache_root, follow_symlinks=False) is None:
        return
    _cache_directory(cache_root)
    for child in cache_root.iterdir():
        if child == keep:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", child.name) or child.name.startswith(".staging-"):
            remove_tree(child, within=cache_root)


def _publish(cache_root: Path, files: dict[str, PackageFile], *, bootstrap: bool) -> Path:
    digest = hashlib.sha256()
    for path, file in sorted(files.items()):
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(bytes([file.executable]))
        digest.update(len(file.content).to_bytes(8, "big"))
        digest.update(file.content)
    _cache_directory(cache_root)
    destination = cache_root / digest.hexdigest()
    package = destination / "vbot-docs"
    status = stat_or_none(destination, follow_symlinks=False)
    if status is not None:
        regular = not is_link_status(status) and stat.S_ISDIR(status.st_mode)
        if not bootstrap:
            if not regular:
                raise OSError("The Skill documentation generation is not a regular directory")
            return package
        if regular:
            try:
                if read_directory(package, reject_excluded=True) == files:
                    return package
            except PackageError, OSError:
                pass
        # No readers exist at bootstrap. Never trust a directory name alone.
        remove_tree(destination, within=cache_root)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=cache_root))
    try:
        for relative, file in files.items():
            target = staging / "vbot-docs" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.content)
            if file.executable:
                target.chmod(target.stat().st_mode | stat.S_IXUSR)
        try:
            os.rename(staging, destination)
        except OSError:
            # Another scan can have published these exact bytes while we wrote.
            status = stat_or_none(destination, follow_symlinks=False)
            if status is None or is_link_status(status) or not stat.S_ISDIR(status.st_mode):
                raise
    finally:
        remove_tree(staging, within=cache_root)
    return package
