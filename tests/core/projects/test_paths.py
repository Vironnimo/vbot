"""Tests for project cwd normalization, identity keys, and slugification."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.projects.paths as paths_module
from core.projects.paths import (
    cwd_exists,
    cwd_identity_key,
    normalize_cwd,
    slugify_project_id,
)


def test_normalize_cwd_resolves_an_absolute_path_without_trailing_separator(
    tmp_path: Path,
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()

    assert normalize_cwd(tmp_path) == Path(os.path.realpath(tmp_path))
    assert normalize_cwd(f"{tmp_path}{os.sep}") == normalize_cwd(str(tmp_path))
    assert normalize_cwd(sub / ".." / "sub") == normalize_cwd(sub)
    with pytest.raises(ValueError):
        normalize_cwd("   ")


def test_cwd_identity_key_resolves_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    assert cwd_identity_key(link) == cwd_identity_key(target)


@pytest.mark.parametrize(
    ("case_insensitive", "first", "second", "same"),
    [
        # A Windows filesystem is case-insensitive: two casings name the same repo.
        pytest.param(True, "C:/Repos/VBot", "c:/repos/vbot", True, id="windows"),
        pytest.param(False, "/srv/A", "/srv/a", False, id="posix"),
    ],
)
def test_cwd_identity_key_case_folds_only_on_windows(
    monkeypatch: pytest.MonkeyPatch, case_insensitive: bool, first: str, second: str, same: bool
) -> None:
    monkeypatch.setattr(paths_module, "_CWD_CASE_INSENSITIVE", case_insensitive)
    monkeypatch.setattr(paths_module, "normalize_cwd", lambda value: Path(str(value)))

    assert (cwd_identity_key(first) == cwd_identity_key(second)) is same


def test_cwd_exists_only_for_an_existing_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")

    assert cwd_exists(tmp_path) is True
    assert cwd_exists(tmp_path / "gone") is False
    assert cwd_exists(file_path) is False


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("vBot", "vbot"),
        ("Build Helper", "build-helper"),
        ("  Trim  Me  ", "trim-me"),
        ("Café Münchén", "cafe-munchen"),
        ("under_score", "under_score"),
        ("a/b:c", "a-b-c"),
        ("0starts-with-digit", "0starts-with-digit"),
        ("x" * 200, "x" * paths_module.MAX_PROJECT_ID_LENGTH),
    ],
)
def test_slugify_project_id_normalizes_names(display_name: str, expected: str) -> None:
    assert slugify_project_id(display_name) == expected


@pytest.mark.parametrize("display_name", ["", "   ", "***", "/// ---"])
def test_slugify_project_id_rejects_unslugifiable_names(display_name: str) -> None:
    with pytest.raises(ValueError):
        slugify_project_id(display_name)
