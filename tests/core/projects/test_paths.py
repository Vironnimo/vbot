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


def test_normalize_cwd_returns_absolute_resolved_path(tmp_path: Path) -> None:
    normalized = normalize_cwd(tmp_path)

    assert normalized.is_absolute()
    assert normalized == Path(os.path.realpath(tmp_path))


def test_normalize_cwd_strips_trailing_separator(tmp_path: Path) -> None:
    with_slash = f"{tmp_path}{os.sep}"

    assert normalize_cwd(with_slash) == normalize_cwd(str(tmp_path))


def test_normalize_cwd_collapses_dot_segments(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    noisy = sub / ".." / "sub"

    assert normalize_cwd(noisy) == normalize_cwd(sub)


def test_normalize_cwd_rejects_empty() -> None:
    with pytest.raises(ValueError):
        normalize_cwd("   ")


def test_cwd_identity_key_equal_for_trailing_slash_variants(tmp_path: Path) -> None:
    assert cwd_identity_key(str(tmp_path)) == cwd_identity_key(f"{tmp_path}{os.sep}")


def test_cwd_identity_key_resolves_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    assert cwd_identity_key(link) == cwd_identity_key(target)


def test_cwd_identity_key_case_folds_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows filesystem is case-insensitive: two casings name the same repo.
    monkeypatch.setattr(paths_module.os, "name", "nt")
    monkeypatch.setattr(paths_module.os.path, "realpath", lambda value: str(value), raising=True)

    assert cwd_identity_key("C:/Repos/VBot") == cwd_identity_key("c:/repos/vbot")


def test_cwd_identity_key_case_sensitive_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    # POSIX filesystem is case-sensitive: /srv/A and /srv/a are distinct repos.
    monkeypatch.setattr(paths_module.os, "name", "posix")
    monkeypatch.setattr(paths_module.os.path, "realpath", lambda value: str(value), raising=True)

    assert cwd_identity_key("/srv/A") != cwd_identity_key("/srv/a")


def test_cwd_exists_true_for_existing_directory(tmp_path: Path) -> None:
    assert cwd_exists(tmp_path) is True


def test_cwd_exists_false_for_missing_directory(tmp_path: Path) -> None:
    assert cwd_exists(tmp_path / "gone") is False


def test_cwd_exists_false_for_file(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")

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
    ],
)
def test_slugify_project_id_normalizes_names(display_name: str, expected: str) -> None:
    assert slugify_project_id(display_name) == expected


def test_slugify_project_id_truncates_to_max_length() -> None:
    slug = slugify_project_id("x" * 200)

    assert len(slug) == paths_module.MAX_PROJECT_ID_LENGTH


@pytest.mark.parametrize("display_name", ["", "   ", "***", "/// ---"])
def test_slugify_project_id_rejects_unslugifiable_names(display_name: str) -> None:
    with pytest.raises(ValueError):
        slugify_project_id(display_name)
