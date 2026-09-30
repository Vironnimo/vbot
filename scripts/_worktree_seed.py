"""Seed a new checkout's git-ignored dependencies from another checkout.

A fresh checkout lacks the WebUI's ``node_modules``, the verified search engine and
the type checker's cache. Installing or rebuilding them costs minutes on Windows,
where writing thousands of small files dominates; copying another checkout's
matching copy takes seconds. Each seed is best-effort: the caller installs normally
whenever a seed is refused or fails.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from scripts._webui_packages import installed_differences

WEBUI_PACKAGES_DIR_NAME = "node_modules"
# Tool caches Vite and Vitest keep inside node_modules; they belong to one checkout.
CHECKOUT_CACHE_DIR_NAMES = (".vite", ".vite-temp")
NATIVE_RESOURCES_RELATIVE_PATH = Path("resources") / "native"
TYPE_CHECK_CACHE_DIR_NAME = ".mypy_cache"
# robocopy exit codes 0-7 report success (copied, extra or mismatched files);
# 8 and above report failures.
ROBOCOPY_FAILURE_EXIT_CODE = 8


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
    if destination.exists() or installed_differences(source, lock_path):
        return False
    # Checked again on the copy: the primary checkout may reinstall meanwhile.
    if _copy_tree(source, destination) and not installed_differences(destination, lock_path):
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


def seed_type_check_cache(source_root: Path, target_root: Path) -> bool:
    """Copy a checkout's mypy cache into a checkout without one; return whether it did.

    mypy checks each cached module against the current file's content, so a copy
    from another checkout only saves the work of modules that are alike.
    """
    source = source_root / TYPE_CHECK_CACHE_DIR_NAME
    destination = target_root / TYPE_CHECK_CACHE_DIR_NAME
    if not source.is_dir() or destination.exists():
        return False
    if _copy_tree(source, destination):
        return True
    shutil.rmtree(destination, ignore_errors=True)
    return False
