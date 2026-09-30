"""Seed a new worktree's git-ignored dependencies from the primary checkout.

A fresh checkout lacks the WebUI's ``node_modules`` and the verified search
engine. Installing them costs minutes on Windows, where writing thousands of
small files dominates; copying the primary checkout's matching copy takes
seconds. Each seed is best-effort: the caller installs normally whenever a
seed is refused or fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

WEBUI_PACKAGES_DIR_NAME = "node_modules"
# npm records the tree it installed here; npm itself trusts it as the
# description of what ``node_modules`` holds.
INSTALLED_PACKAGES_FILE_NAME = ".package-lock.json"
# Tool caches Vite and Vitest keep inside node_modules; they belong to one checkout.
CHECKOUT_CACHE_DIR_NAMES = (".vite", ".vite-temp")
NATIVE_RESOURCES_RELATIVE_PATH = Path("resources") / "native"
# robocopy exit codes 0-7 report success (copied, extra or mismatched files);
# 8 and above report failures.
ROBOCOPY_FAILURE_EXIT_CODE = 8


def installed_packages_match_lock(node_modules: Path, lock_path: Path) -> bool:
    """Return whether *node_modules* holds exactly the tree *lock_path* locks.

    Every installed package must equal its locked entry; a locked package may
    be missing only when it is optional (platform packages for other hosts).
    """
    try:
        installed = json.loads(
            (node_modules / INSTALLED_PACKAGES_FILE_NAME).read_text(encoding="utf-8")
        )["packages"]
        locked = json.loads(lock_path.read_text(encoding="utf-8"))["packages"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(installed, dict) or not isinstance(locked, dict):
        return False
    # The root entry "" describes the project itself, not an installed package.
    locked_packages = {key: value for key, value in locked.items() if key}
    if any(locked_packages.get(key) != value for key, value in installed.items()):
        return False
    return all(
        isinstance(value, dict) and value.get("optional") is True
        for key, value in locked_packages.items()
        if key not in installed
    )


def _copy_tree(source: Path, destination: Path) -> bool:
    """Copy a directory tree without the top-level checkout caches."""
    if sys.platform == "win32":
        # Multithreaded robocopy is several times faster than copytree here.
        command = [
            "robocopy",
            str(source),
            str(destination),
            "/E",
            "/MT:16",
            "/R:1",
            "/W:1",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
            "/NP",
            "/XD",
            *(str(source / name) for name in CHECKOUT_CACHE_DIR_NAMES),
        ]
        try:
            result = subprocess.run(command, capture_output=True, check=False)
        except OSError:
            return False
        return result.returncode < ROBOCOPY_FAILURE_EXIT_CODE

    def ignore_caches(directory: str, names: list[str]) -> list[str]:
        if Path(directory) != source:
            return []
        return [name for name in names if name in CHECKOUT_CACHE_DIR_NAMES]

    try:
        shutil.copytree(source, destination, symlinks=True, ignore=ignore_caches)
    except (OSError, shutil.Error):
        return False
    return True


def seed_webui_packages(primary_root: Path, worktree_path: Path) -> bool:
    """Copy the primary checkout's ``node_modules`` when it matches the worktree's lock.

    Returns whether the worktree now holds a matching tree. A refused or failed
    copy leaves no ``node_modules`` behind, so the caller's install starts clean.
    """
    source = primary_root / "webui" / WEBUI_PACKAGES_DIR_NAME
    worktree_webui = worktree_path / "webui"
    destination = worktree_webui / WEBUI_PACKAGES_DIR_NAME
    lock_path = worktree_webui / "package-lock.json"
    if destination.exists() or not installed_packages_match_lock(source, lock_path):
        return False
    # Checked again on the copy: the primary checkout may reinstall meanwhile.
    if _copy_tree(source, destination) and installed_packages_match_lock(destination, lock_path):
        return True
    shutil.rmtree(destination, ignore_errors=True)
    return False


def seed_native_resources(primary_root: Path, worktree_path: Path) -> None:
    """Copy the primary checkout's downloaded native executables.

    Their provisioner verifies each pinned digest afterwards and downloads
    again on a mismatch, so a stale or foreign copy costs nothing but the copy.
    """
    source = primary_root / NATIVE_RESOURCES_RELATIVE_PATH
    if not source.is_dir():
        return
    with suppress(OSError, shutil.Error):
        shutil.copytree(source, worktree_path / NATIVE_RESOURCES_RELATIVE_PATH, dirs_exist_ok=True)
