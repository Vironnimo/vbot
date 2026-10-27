"""Checkout identity, subprocess boundary and port-allocation lock for worktree tooling."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from core.utils.file_lock import acquire_file_lock, release_file_lock

VALID_WORKTREE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PORT_ALLOCATION_LOCK_NAME = "vbot-worktree-port.lock"
WINDOWS_DETACHED_CREATION_FLAGS = 0x00000008 | 0x00000200


def print_ok(**fields: str | int | bool | Path) -> None:
    """Print structured success output as key-value lines."""
    for key, value in fields.items():
        rendered = str(value) if isinstance(value, Path) else value
        print(f"{key}: {rendered}")


def print_error(reason: str) -> None:
    """Print structured error output."""
    print(f"error: {reason}")


def validate_worktree_name(name: str) -> str | None:
    """Return an error message when a worktree name is unsafe."""
    if name.rstrip(".").casefold() == "dev":
        return "worktree name 'dev' is reserved for the primary checkout's data directory"
    if VALID_WORKTREE_NAME_PATTERN.fullmatch(name) and not name.endswith("."):
        return None

    return (
        "worktree name must start with a letter or number and contain only "
        "letters, numbers, dots, underscores, and hyphens; it must not end with a dot"
    )


def _npm_command() -> str:
    return shutil.which("npm") or "npm"


def canonical_path(path: Path) -> str:
    """Return a stable identity for a repository or worktree path."""
    return os.path.normcase(str(path.resolve()))


def _resolve_project_root(script_root: Path) -> Path:
    """Resolve the canonical repository root across linked git worktrees."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=script_root,
            check=False,
        )
    except OSError:
        return script_root

    if result.returncode != 0:
        return script_root

    git_common_dir = result.stdout.strip()
    if not git_common_dir:
        return script_root

    return Path(git_common_dir).resolve().parent


@dataclass
class WorktreeContext:
    """Explicit repository and executing-checkout identity shared by one command."""

    project_root: Path
    worktrees_dir: Path
    data_root: Path
    checkout_root: Path

    @classmethod
    def from_checkout(cls, checkout_root: Path) -> WorktreeContext:
        project_root = _resolve_project_root(checkout_root)
        return cls(
            project_root,
            project_root / ".worktrees",
            project_root.parent / f"{project_root.name}-data",
            checkout_root,
        )

    def spawn_detached(self, command: list[str], output: int | IO[bytes]) -> None:
        """Start *command* in the primary checkout so it outlives this CLI and its Ctrl+C group."""
        if os.name == "nt":
            subprocess.Popen(
                command,
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                creationflags=WINDOWS_DETACHED_CREATION_FLAGS,
            )
        else:
            subprocess.Popen(
                command,
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )

    @contextmanager
    def port_allocation_lock(self) -> Iterator[None]:
        """Serialize port selection until the owning marker is durable."""
        lock_path = self.git_common_dir() / PORT_ALLOCATION_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            acquire_file_lock(lock_file.fileno(), blocking=True)
            try:
                yield
            finally:
                release_file_lock(lock_file.fileno())

    def git_common_dir(self) -> Path:
        """Resolve the shared Git directory from either the main tree or a worktree."""
        dot_git = self.project_root / ".git"
        if dot_git.is_dir():
            return dot_git
        try:
            marker = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return dot_git
        prefix = "gitdir:"
        if not marker.lower().startswith(prefix):
            return dot_git
        git_dir = Path(marker[len(prefix) :].strip())
        if not git_dir.is_absolute():
            git_dir = (self.project_root / git_dir).resolve()
        if git_dir.parent.name == "worktrees":
            return git_dir.parent.parent
        return git_dir

    def run_command(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
    ) -> tuple[int, str]:
        """Run a command and return returncode and stderr text."""
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=cwd or self.project_root,
                check=False,
            )
        except OSError as exc:
            return 1, str(exc)
        return result.returncode, result.stderr.strip()
