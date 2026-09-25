"""Worktree marker values and read-only Git status projections."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

WORKTREE_FILE_NAME = ".vbot-worktree"


DATA_DIR_KEY = "data_dir"


DATA_OWNER_KEY = "data_owner"


DATA_OWNER_FILE_NAME = ".vbot-worktree-owner.json"


MANAGED_BRANCH_KEY = "managed_branch"


SERVER_PORT_KEY = "server_port"


UNKNOWN_VALUE = "unknown"


BRANCH_REF_PREFIX = "refs/heads/"


def _read_worktree_marker(marker_path: Path) -> dict[str, object] | None:
    """Read a worktree marker JSON object."""
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    return data


def _read_settings_port(data_dir: Path | None) -> int | None:
    """Read the configured server port from a data directory."""
    if data_dir is None:
        return None

    settings_path = data_dir / "settings.json"
    if not settings_path.exists():
        return None

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(settings, dict):
        return None

    port = settings.get(SERVER_PORT_KEY)
    if isinstance(port, int):
        return port
    return None


def _marker_data_dir(marker_data: dict[str, object] | None) -> tuple[str, Path | None]:
    """Return display and resolved data-dir values from marker data."""
    if marker_data is None:
        return UNKNOWN_VALUE, None

    raw_data_dir = marker_data.get(DATA_DIR_KEY)
    if not isinstance(raw_data_dir, str) or not raw_data_dir:
        return UNKNOWN_VALUE, None

    return raw_data_dir, Path(raw_data_dir).expanduser()


def _marker_managed_branch(marker_data: dict[str, object] | None) -> str:
    """Return a stable display value for marker managed-branch state."""
    if marker_data is None:
        return UNKNOWN_VALUE

    managed_branch = marker_data.get(MANAGED_BRANCH_KEY)
    if isinstance(managed_branch, bool):
        return str(managed_branch).lower()
    return UNKNOWN_VALUE


def _list_uncommitted_paths(worktree_path: Path) -> list[str]:
    """List porcelain status lines for uncommitted files in a worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []

    if result.returncode != 0:
        return []

    return [line for line in result.stdout.splitlines() if line.strip()]


def _read_worktree_branch_name(worktree_path: Path) -> str | None:
    """Read the currently checked-out branch in a worktree.

    Returns ``None`` when *worktree_path* has no ``.git`` entry: ``git -C``
    would otherwise climb to the enclosing repository and report its branch.
    """
    if not (worktree_path / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _read_worktree_registrations(repo_root: Path) -> list[dict[str, str]] | None:
    """Return Git's worktree registrations as attribute maps, or ``None`` if unknown.

    Each map holds the ``git worktree list --porcelain`` attributes of one
    registration (``worktree``, ``HEAD``, ``branch``, ``prunable``, ...), which
    Git keeps reporting for a registered checkout whose files are gone.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    registrations: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for field in result.stdout.split("\0"):
        if not field:
            # An empty field ends a registration record.
            if current:
                registrations.append(current)
                current = {}
            continue
        key, _, value = field.partition(" ")
        current[key] = value
    if current:
        registrations.append(current)
    return registrations


def _find_worktree_registration(
    registrations: list[dict[str, str]], worktree_path: Path
) -> dict[str, str] | None:
    """Return the registration whose path is *worktree_path*, if any."""
    target = os.path.normcase(str(worktree_path.resolve()))
    for registration in registrations:
        registered = registration.get("worktree")
        if registered and os.path.normcase(str(Path(registered).resolve())) == target:
            return registration
    return None


def _read_registered_branch_name(repo_root: Path, worktree_path: Path) -> str | None:
    """Read the branch Git records for *worktree_path* without entering it.

    Unlike ``_read_worktree_branch_name`` this also answers for a registered
    worktree whose checkout is gone, and never falls through to *repo_root*'s
    own branch.
    """
    registrations = _read_worktree_registrations(repo_root)
    if registrations is None:
        return None
    registration = _find_worktree_registration(registrations, worktree_path)
    if registration is None:
        return None
    branch_ref = registration.get("branch", "")
    if not branch_ref.startswith(BRANCH_REF_PREFIX):
        return None
    return branch_ref.removeprefix(BRANCH_REF_PREFIX) or None
