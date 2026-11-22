"""Readable originals for attachments delivered to one immutable Session generation."""

from __future__ import annotations

import errno
import shutil
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_stream
from core.utils.file_status import is_link_status, stat_or_none
from core.utils.ids import is_safe_id

if TYPE_CHECKING:
    from core.attachments import AttachmentRecord


def session_attachment_dir(data_dir: Path, generation_id: str) -> Path:
    if not is_safe_id(generation_id):
        raise ValueError("Invalid Session generation for attachment delivery")
    folder = DataDirectoryLayout(data_dir).session_attachments / generation_id
    if (
        folder.resolve()
        != DataDirectoryLayout(data_dir.resolve()).session_attachments / generation_id
    ):
        raise OSError("Session attachment folder is redirected")
    return folder


def _check_directory(path: Path) -> None:
    status = path.lstat()
    if is_link_status(status) or not stat.S_ISDIR(status.st_mode):
        raise OSError("Session attachment storage requires real directories")


def publish(data_dir: Path, generation_id: str, record: AttachmentRecord) -> Path:
    """Copy a validated blob once. Concurrent readers never see a partial file."""
    folder = session_attachment_dir(data_dir, generation_id)
    source = Path(record.file_path)
    target = folder / source.name
    for directory in (folder.parent.parent, folder.parent, folder):
        try:
            _check_directory(directory)
        except FileNotFoundError:
            directory.mkdir(mode=0o700, exist_ok=True)
            _check_directory(directory)
    status = stat_or_none(target, follow_symlinks=False)
    if status is not None:
        if is_link_status(status) or not stat.S_ISREG(status.st_mode):
            raise OSError("Session attachment path is not a regular file")
        if status.st_size == record.size_bytes:
            return target
    with source.open("rb") as blob:
        atomic_write_stream(target, lambda output: shutil.copyfileobj(blob, output), mode=0o600)
    return target


def session_files_for_ids(data_dir: Path, attachment_ids: set[str]) -> dict[str, list[Path]]:
    """Index candidate copies in one pass without following links or removing files."""
    files: dict[str, list[Path]] = {}
    root = DataDirectoryLayout(data_dir).session_attachments
    try:
        _check_directory(root.parent)
        _check_directory(root)
    except FileNotFoundError:
        return files
    for folder in root.iterdir():
        status = folder.lstat()
        if (
            is_link_status(status)
            or not stat.S_ISDIR(status.st_mode)
            or not is_safe_id(folder.name)
        ):
            continue
        for path in folder.iterdir():
            if path.stem not in attachment_ids:
                continue
            entry = path.lstat()
            if stat.S_ISREG(entry.st_mode) and not is_link_status(entry):
                files.setdefault(path.stem, []).append(path)
    return files


def remove_session_files(paths: Iterable[Path]) -> None:
    """Remove the copies of one validated original and leave other files alone."""
    for path in paths:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise
