"""Real Git repositories for source-update, customization and installation tests.

Creating the repositories takes eight Git processes; configuration is written to the
config files directly. ``conftest.py`` builds them once per test session; each test copies
them instead.
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


# Automatic maintenance after commit, push or fetch runs detached and creates and
# removes ``objects/maintenance.lock`` while the templates are being copied.
_NO_BACKGROUND_MAINTENANCE = {"maintenance.auto": "false", "gc.auto": "0"}


def _append_config(config_file: Path, values: dict[str, str]) -> None:
    """Append ``section[.subsection].key`` values to a Git config file."""
    sections: dict[str, list[str]] = {}
    for name, value in values.items():
        section, _, key = name.rpartition(".")
        head, dot, subsection = section.partition(".")
        header = f'[{head} "{subsection}"]' if dot else f"[{head}]"
        sections.setdefault(header, []).append(f"\t{key} = {value}")
    with config_file.open("a", encoding="utf-8", newline="\n") as handle:
        for header, lines in sections.items():
            handle.write("\n".join([header, *lines]) + "\n")


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
    _append_config(remote / "config", _NO_BACKGROUND_MAINTENANCE)
    _append_config(
        checkout / ".git" / "config",
        {
            **_NO_BACKGROUND_MAINTENANCE,
            "user.name": "Source Test",
            "user.email": "source@example.invalid",
        },
    )
    for name, content in TEMPLATE_FILES.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(content, encoding="utf-8")
    git(checkout, "add", ".")
    git(checkout, "commit", "--quiet", "-m", "base")
    shutil.copytree(checkout, root / "plain")
    # Relative remote paths keep every copy of the three repositories self-contained.
    _append_config(
        checkout / ".git" / "config",
        {
            "remote.origin.url": f"../{remote.name}",
            "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
        },
    )
    git(checkout, "push", "--quiet", "-u", "origin", "main")
    publisher_config = {
        **_NO_BACKGROUND_MAINTENANCE,
        "user.name": "Publisher",
        "user.email": "publisher@example.invalid",
    }
    git(
        root,
        "clone",
        "--quiet",
        "--template=",
        *(f"--config={name}={value}" for name, value in publisher_config.items()),
        remote.name,
        publisher.name,
    )
    # Clone records the remote's absolute path.
    git(publisher, "remote", "set-url", "origin", f"../{remote.name}")
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
