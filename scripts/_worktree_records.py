"""Worktree marker values and read-only Git status projections."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

WORKTREE_FILE_NAME = ".vbot-worktree"


DATA_DIR_KEY = "data_dir"


MANAGED_BRANCH_KEY = "managed_branch"


SERVER_PORT_KEY = "server_port"


UNKNOWN_VALUE = "unknown"


def _read_worktree_marker(marker_path: Path) -> dict[str, object] | None:
    """Read a worktree marker JSON object."""
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    """Read the currently checked-out branch in a worktree."""
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
