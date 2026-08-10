"""Tests for shared atomic-write staging."""

import os
import stat
from pathlib import Path

import pytest

from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_bytes, atomic_write_text, temporary_path


def test_temporary_path_uses_canonical_atomic_directory(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"

    temporary = temporary_path(tmp_path, target)

    assert temporary.parent == DataDirectoryLayout(tmp_path).atomic_temporary


def test_atomic_write_cleans_staging_file_after_success(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    layout = DataDirectoryLayout(tmp_path)

    atomic_write_text(target, "{}\n", data_dir=tmp_path)

    assert target.read_text(encoding="utf-8") == "{}\n"
    assert list(layout.atomic_temporary.iterdir()) == []


@pytest.mark.parametrize("write_text", [False, True])
def test_atomic_write_fsyncs_data_before_replace_and_directories_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_text: bool,
) -> None:
    target = tmp_path / "settings.json"
    events: list[str] = []
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("directory_fsync" if stat.S_ISDIR(mode) else "file_fsync")

    def record_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr("core.utils.atomic.os.fsync", record_fsync)
    monkeypatch.setattr("core.utils.atomic.os.replace", record_replace)

    if write_text:
        atomic_write_text(target, "new\n", data_dir=tmp_path)
    else:
        atomic_write_bytes(target, b"new\n", data_dir=tmp_path)

    assert events[:2] == ["file_fsync", "replace"]
    assert all(event == "directory_fsync" for event in events[2:])
    assert len(events[2:]) == (2 if os.name == "posix" else 0)


def test_atomic_write_cleans_staging_file_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("old\n", encoding="utf-8")
    layout = DataDirectoryLayout(tmp_path)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("core.utils.atomic.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "new\n", data_dir=tmp_path)

    assert target.read_text(encoding="utf-8") == "old\n"
    assert list(layout.atomic_temporary.iterdir()) == []
