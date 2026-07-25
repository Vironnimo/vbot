"""Tests for shared atomic-write staging."""

from pathlib import Path

import pytest

from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_text, temporary_path


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
