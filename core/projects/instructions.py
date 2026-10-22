"""Which repository instruction files a Project gives its Agents.

Two conventions, both owned here so the System Prompt, the ``project`` Tool,
the Projects view and Chat's folder instructions apply the same names:

- The **main instruction file** is the first existing file of
  :data:`MAIN_INSTRUCTION_FILES` in the repository root. ``AGENTS.md`` is the
  tool-neutral convention; the others are the files Claude Code, Gemini CLI and
  GitHub Copilot read, which usually repeat it, so only one of them loads. The
  choice is made whenever the Working Project frame renders, so a repository
  that gains an ``AGENTS.md`` switches on its own.
- **Folder instructions** are the files of :data:`FOLDER_INSTRUCTION_FILES`
  inside subfolders of the repository. They govern work in their folder and
  below, and Chat hands one to the Agent when it first works there
  (``chat/request-building.md`` -> Folder instructions).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.utils.file_status import is_dir_strict, is_file_strict

MAIN_INSTRUCTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
)
FOLDER_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
# Folders of other projects' code or of version control: an instruction file
# there is about developing that package, not about this repository.
_FOREIGN_FOLDERS = frozenset({".git", "node_modules", "site-packages", "__pycache__"})
_CASE_INSENSITIVE = os.name == "nt"


def main_instruction_file(root: Path) -> str | None:
    """Return the repository-relative main instruction file of *root*, or ``None``.

    A name whose status cannot be checked counts as present: it is unavailable,
    never absent, so a file behind a failing check is not silently replaced by
    the next convention. The prompt reader reports the failed read.
    """
    for name in MAIN_INSTRUCTION_FILES:
        if _file_present(root / name):
            return name
    return None


def resolve_instruction_files(
    root: Path, *, automatic: bool, additional: Sequence[str]
) -> list[str]:
    """Return the instruction files a Project loads, in order, each once.

    The main instruction file comes first when *automatic*, then the
    *additional* entries as stored (relative to *root* or absolute). An entry
    naming the same file as an earlier one is dropped.
    """
    main = main_instruction_file(root) if automatic else None
    result: list[str] = []
    seen: set[str] = set()
    for name in [main, *additional] if main is not None else additional:
        key = _identity(root / name)
        if key not in seen:
            seen.add(key)
            result.append(name)
    return result


@dataclass(frozen=True)
class FolderInstructions:
    """Finds the folder instruction files that govern paths inside one repository.

    *root* is the Project's normalized repository path; *loaded* names the
    instruction files the System Prompt already carries, which never count as
    folder instructions.
    """

    root: Path
    loaded: frozenset[str] = frozenset()

    @classmethod
    def for_project(cls, root: Path, loaded: Iterable[str]) -> FolderInstructions:
        return cls(root, frozenset(_identity(root / name) for name in loaded))

    def files_for(self, paths: Sequence[Path], *, seen: Iterable[Path] = ()) -> list[Path]:
        """Return the folder instruction files governing *paths*, outermost first.

        For each absolute path inside the repository, its folder (the path itself
        when it is a directory) and every folder above it up to, but without, the
        repository root contribute their first existing file of
        :data:`FOLDER_INSTRUCTION_FILES`. Paths outside the repository contribute
        nothing, and neither do folders inside a package or version-control folder.
        A file in *seen* or among *paths* is left out: the Agent already has it.
        So is every :data:`MAIN_INSTRUCTION_FILES` location (``.claude/CLAUDE.md``):
        it is a repository-wide file, never the instructions of its folder.
        Blocking: checks the filesystem.
        """
        root_key = _identity(self.root)
        depth = len(Path(os.path.normpath(self.root)).parts)
        found: list[Path] = []
        known = (
            set(self.loaded)
            | {_identity(self.root / name) for name in MAIN_INSTRUCTION_FILES}
            | {_identity(path) for path in (*seen, *paths)}
        )
        checked: set[str] = set()
        for path in paths:
            folders: list[Path] = []
            folder = path if _directory(path) else path.parent
            while (key := _identity(folder)) != root_key and _inside(key, root_key):
                if key in checked:
                    break
                checked.add(key)
                folders.append(folder)
                folder = folder.parent
            for folder in reversed(folders):
                if not _FOREIGN_FOLDERS.isdisjoint(folder.parts[depth:]):
                    break
                file = _folder_file(folder)
                if file is not None and (file_key := _identity(file)) not in known:
                    known.add(file_key)
                    found.append(file)
        return found


def _folder_file(folder: Path) -> Path | None:
    for name in FOLDER_INSTRUCTION_FILES:
        if _file_present(folder / name):
            return folder / name
    return None


def _file_present(path: Path) -> bool:
    try:
        return is_file_strict(path)
    except OSError:
        return True


def _directory(path: Path) -> bool:
    try:
        return is_dir_strict(path)
    except OSError:
        return False


def _identity(path: Path) -> str:
    """The comparison key of an absolute path: case-folded on Windows only."""
    text = os.path.normpath(path)
    return text.casefold() if _CASE_INSENSITIVE else text


def _inside(key: str, root_key: str) -> bool:
    return key.startswith(root_key.rstrip(os.sep) + os.sep)
