"""Build identity of the running code across the ways vBot is deployed."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from core.utils.version import BuildIdentity, detect_build_identity

VERSION = "0.4.4"
RELEASE = "1" * 40
MAIN = "2" * 40
TAG_OBJECT = "3" * 40


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _source(root: Path) -> Path:
    _write(root / "pyproject.toml", f'[project]\nname = "vbot"\nversion = "{VERSION}"\n')
    return root


def _package(tmp_path: Path, manifest: Mapping[str, object]) -> Path:
    install = tmp_path / "install"
    app = _source(install / "versions" / "v1" / "app")
    _write(install / "versions" / "v1" / "release.json", json.dumps(manifest))
    return app


def _official_package(tmp_path: Path) -> Path:
    manifest = {"version": VERSION, "revision": RELEASE, "channel": "release", "files": {}}
    return _package(tmp_path, manifest)


def _main_package(tmp_path: Path) -> Path:
    return _package(tmp_path, {"version": VERSION, "revision": MAIN, "channel": "main"})


def _branch_checkout(tmp_path: Path) -> Path:
    root = _source(tmp_path / "checkout")
    _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(root / ".git" / "refs" / "heads" / "main", MAIN + "\n")
    return root


def _linked_worktree(tmp_path: Path) -> Path:
    common = tmp_path / "checkout" / ".git"
    _write(common / "packed-refs", f"# pack-refs with: peeled\n{MAIN} refs/heads/task\n")
    private = common / "worktrees" / "task"
    _write(private / "HEAD", "ref: refs/heads/task\n")
    _write(private / "commondir", "../..\n")
    root = _source(tmp_path / "task")
    _write(root / ".git", f"gitdir: {private}\n")
    return root


def _release_checkout(tmp_path: Path) -> Path:
    root = _source(tmp_path / "checkout")
    _write(root / ".git" / "HEAD", RELEASE + "\n")
    # An annotated release tag resolves through its peeled commit.
    _write(root / ".git" / "packed-refs", f"{TAG_OBJECT} refs/tags/v{VERSION}\n^{RELEASE}\n")
    return root


def _detached_checkout(tmp_path: Path) -> Path:
    root = _source(tmp_path / "checkout")
    _write(root / ".git" / "HEAD", MAIN + "\n")
    _write(root / ".git" / "refs" / "tags" / f"v{VERSION}", RELEASE + "\n")
    return root


def _plain_source(tmp_path: Path) -> Path:
    return _source(tmp_path / "plain")


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        (_official_package, BuildIdentity(VERSION, RELEASE, release=True)),
        (_main_package, BuildIdentity(VERSION, MAIN, branch="main")),
        (_branch_checkout, BuildIdentity(VERSION, MAIN, branch="main")),
        (_linked_worktree, BuildIdentity(VERSION, MAIN, branch="task")),
        (_release_checkout, BuildIdentity(VERSION, RELEASE, release=True)),
        (_detached_checkout, BuildIdentity(VERSION, MAIN)),
        (_plain_source, BuildIdentity(VERSION)),
    ],
)
def test_build_identity_describes_the_running_build(
    tmp_path: Path, layout: Callable[[Path], Path], expected: BuildIdentity
) -> None:
    assert detect_build_identity(layout(tmp_path)) == expected
