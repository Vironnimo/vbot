"""The running vBot version and build identity from their sources of truth."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_package_version
from pathlib import Path
from typing import Any

PACKAGE_NAME = "vbot"
UNKNOWN_VBOT_VERSION = "0.0.0+unknown"
# The checkout root; kept local so the helper imports nothing heavy.
_VBOT_ROOT = Path(__file__).resolve().parents[2]
# Git names a commit by 40 (SHA-1) or 64 (SHA-256) lowercase hex digits.
_REVISION_LENGTHS = frozenset({40, 64})
_HEX_DIGITS = frozenset("0123456789abcdef")
_SYMBOLIC_REF_DEPTH = 5


@dataclass(frozen=True)
class BuildIdentity:
    """Which vBot build this process runs.

    ``revision`` is the full source commit when known. ``release`` marks an
    official release build; any other build names the Git ``branch`` it was
    built from when that is known.
    """

    version: str
    revision: str | None = None
    branch: str | None = None
    release: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "branch": self.branch,
            "release": self.release,
        }


def detect_vbot_version(root: Path = _VBOT_ROOT) -> str:
    """Resolve the running vBot version from its single source of truth.

    The version lives once, in ``pyproject.toml`` -> ``project.version``. Read
    that file directly when it sits next to the running code (development
    checkouts and packaged versions both carry it): it is the *live* value, so a
    version bump in a checkout flows through without a reinstall. Installed
    package metadata is only a fallback for a pure wheel install where the
    source tree is absent; it is a snapshot frozen at install
    time and would otherwise drift behind an edited ``pyproject.toml``.
    """
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
        if isinstance(version, str) and version:
            return version
    except OSError, KeyError, tomllib.TOMLDecodeError:
        pass
    try:
        return _installed_package_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VBOT_VERSION


def detect_build_identity(root: Path = _VBOT_ROOT) -> BuildIdentity:
    """Identify the build whose code runs from ``root``.

    Read it once at startup: the code a process runs never changes, while a
    checkout's HEAD moves with every later commit. A packaged version
    describes itself in its ``release.json``; a Git checkout (development or
    Worktree) in its HEAD. A branch checkout names its
    branch; a detached HEAD is a release when the release tag ``v<version>``
    points at it. Anything unreadable leaves the version alone.
    """
    version = detect_vbot_version(root)
    return (
        _packaged_identity(root, version)
        or _checkout_identity(root, version)
        or BuildIdentity(version)
    )


def _packaged_identity(root: Path, version: str) -> BuildIdentity | None:
    # Payload layout: <install>/versions/<version-id>/app is the running code.
    if root.name != "app" or root.parent.parent.name != "versions":
        return None
    manifest = _read_json_object(root.parent / "release.json")
    if manifest is None:
        return None
    revision = _revision(manifest.get("revision"))
    # The builder records the channel it published to; main builds come from main.
    if manifest.get("channel") == "main":
        return BuildIdentity(version, revision, branch="main")
    return BuildIdentity(version, revision, release=True)


def _checkout_identity(root: Path, version: str) -> BuildIdentity | None:
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    common_dir = git_dir
    common_link = _read_text(git_dir / "commondir")
    if common_link:
        common_dir = git_dir / common_link
    head = _read_text(git_dir / "HEAD")
    if head is None:
        return BuildIdentity(version)
    if head.startswith("ref:"):
        ref = head.removeprefix("ref:").strip()
        branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
        return BuildIdentity(version, _resolve_ref(git_dir, common_dir, ref), branch=branch)
    revision = _revision(head)
    tagged = _resolve_ref(git_dir, common_dir, f"refs/tags/v{version}")
    return BuildIdentity(version, revision, release=revision is not None and revision == tagged)


def _git_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    # A linked Worktree's `.git` file points at its private Git directory.
    link = _read_text(dot_git)
    if link is None or not link.startswith("gitdir:"):
        return None
    git_dir = Path(link.removeprefix("gitdir:").strip())
    return git_dir if git_dir.is_absolute() else root / git_dir


def _resolve_ref(git_dir: Path, common_dir: Path, ref: str, depth: int = 0) -> str | None:
    """Resolve a ref to its commit through loose ref files, then ``packed-refs``."""
    if depth > _SYMBOLIC_REF_DEPTH or not ref.startswith("refs/") or ".." in ref.split("/"):
        return None
    for directory in dict.fromkeys((git_dir, common_dir)):
        content = _read_text(directory / ref)
        if content is None:
            continue
        if content.startswith("ref:"):
            target = content.removeprefix("ref:").strip()
            return _resolve_ref(git_dir, common_dir, target, depth + 1)
        return _revision(content)
    packed = _read_text(common_dir / "packed-refs") or ""
    lines = packed.splitlines()
    for index, line in enumerate(lines):
        object_id, _, name = line.partition(" ")
        if name != ref:
            continue
        # An annotated tag is followed by its peeled commit.
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if following.startswith("^"):
            return _revision(following[1:])
        return _revision(object_id)
    return None


def _revision(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) in _REVISION_LENGTHS and set(value) <= _HEX_DIGITS:
        return value
    return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return None


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            value = json.load(handle)
    except OSError, ValueError:
        return None
    return value if isinstance(value, dict) else None
