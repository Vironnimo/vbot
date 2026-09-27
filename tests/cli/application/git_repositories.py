"""Real Git repositories for source-update, customization and installation tests.

Creating the repositories takes about fifteen Git processes. ``conftest.py`` builds
them once per test session; each test copies them instead.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TEMPLATE_FILES = {
    "tracked.txt": "one\n",
    "pyproject.toml": '[project]\nname = "fixture"\nversion = "1.2.3"\n',
    ".gitignore": ".vbot-install.json\n.venv/\n",
    "scripts/windows/requirements-server.lock": "# test-owned lock\n",
    "scripts/windows/requirements-desktop-client.lock": "# test-owned lock\n",
}


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=directory, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class Templates:
    root: Path
    revision: str
    """The single commit of every template repository."""


@dataclass(frozen=True)
class SourceRepositories:
    remote: Path
    checkout: Path
    """On ``main``, tracking ``origin/main`` of ``remote``."""
    publisher: Path
    """A second clone of ``remote`` that publishes upstream commits."""


def build_templates(root: Path) -> Templates:
    remote, checkout, publisher = root / "remote.git", root / "checkout", root / "publisher"
    git(root, "init", "--quiet", "--bare", "--initial-branch=main", "--template=", remote.name)
    git(root, "init", "--quiet", "--initial-branch=main", "--template=", checkout.name)
    git(checkout, "config", "user.name", "Source Test")
    git(checkout, "config", "user.email", "source@example.invalid")
    for name, content in TEMPLATE_FILES.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(content, encoding="utf-8")
    git(checkout, "add", ".")
    git(checkout, "commit", "--quiet", "-m", "base")
    shutil.copytree(checkout, root / "plain")
    # Relative remote paths keep every copy of the three repositories self-contained.
    git(checkout, "remote", "add", "origin", f"../{remote.name}")
    git(checkout, "push", "--quiet", "-u", "origin", "main")
    git(root, "clone", "--quiet", "--template=", remote.name, publisher.name)
    git(publisher, "remote", "set-url", "origin", f"../{remote.name}")
    git(publisher, "config", "user.name", "Publisher")
    git(publisher, "config", "user.email", "publisher@example.invalid")
    return Templates(root, git(checkout, "rev-parse", "HEAD"))


def copy_source_repositories(templates: Templates, destination: Path) -> SourceRepositories:
    for name in ("remote.git", "checkout", "publisher"):
        shutil.copytree(templates.root / name, destination / name)
    return SourceRepositories(
        destination / "remote.git", destination / "checkout", destination / "publisher"
    )


def copy_plain_repository(templates: Templates, destination: Path) -> str:
    """Copy a committed repository without a remote to *destination*; return its HEAD."""

    shutil.copytree(templates.root / "plain", destination)
    return templates.revision
