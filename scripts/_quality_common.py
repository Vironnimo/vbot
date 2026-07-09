"""Shared helpers for the backend and frontend quality-gate runners.

``scripts/quality.py`` and ``scripts/quality-frontend.py`` run the same
format -> lint -> type-check -> test pipeline and share the snapshot/hash
bookkeeping, path dedup, and output-shaping helpers gathered here. The few
functions that differ only in a file-suffix set, a base directory, or a
file-vs-directory predicate are parameterized so each runner passes its own.
"""

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def configure_console_encoding() -> None:
    """Degrade un-encodable characters to ``?`` instead of crashing the runner.

    Tool output is decoded as UTF-8, but Windows consoles often use a legacy code
    page that cannot encode every character (e.g. vitest's check marks).
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def deduplicate_paths(paths: list[str], is_file: Callable[[str], bool]) -> list[str]:
    """Remove file paths already covered by a broader directory path.

    If both ``core/utils/`` and ``core/utils/config.py`` are given, keep only
    ``core/utils/``. *is_file* decides which entries are files rather than
    directories (the two runners recognize files by different suffix rules).
    """
    dirs = [p for p in paths if not is_file(p)]
    files = [p for p in paths if is_file(p)]

    result = list(dirs)
    for file_path in files:
        if not any(file_path.startswith(directory + "/") for directory in dirs):
            result.append(file_path)
    return result


def hash_file(path: Path) -> str:
    """Return a stable content hash for *path*."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def display_path(path: Path) -> str:
    """Return a stable project-relative path for console output."""
    if path.is_relative_to(PROJECT_ROOT):
        return path.relative_to(PROJECT_ROOT).as_posix()
    return path.as_posix()


def iter_snapshot_files(directory: Path, suffixes: set[str], ignored_dirs: set[str]) -> list[Path]:
    """Return files with a *suffixes* extension under *directory*, skipping *ignored_dirs*."""
    files: list[Path] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return files

    for entry in entries:
        if entry.is_dir():
            if entry.name in ignored_dirs:
                continue
            files.extend(iter_snapshot_files(entry, suffixes, ignored_dirs))
            continue
        if entry.suffix in suffixes:
            files.append(entry)
    return files


def snapshot_target_files(
    paths: list[str], base_dir: Path, suffixes: set[str], ignored_dirs: set[str]
) -> dict[str, str]:
    """Return content hashes for fixable files under the given targets.

    Relative targets resolve against *base_dir*; only files whose suffix is in
    *suffixes* are hashed, and directories named in *ignored_dirs* are skipped.
    """
    snapshot: dict[str, str] = {}

    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = base_dir / raw_path
        candidate = candidate.resolve()

        if not candidate.exists():
            continue
        if candidate.is_file():
            if candidate.suffix in suffixes:
                snapshot[display_path(candidate)] = hash_file(candidate)
            continue

        for file_path in iter_snapshot_files(candidate, suffixes, ignored_dirs):
            snapshot[display_path(file_path)] = hash_file(file_path)

    return snapshot


def changed_snapshot_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return sorted file paths whose content changed between two snapshots."""
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


def describe_fix_result(returncode: int, elapsed: float, changed_files: list[str]) -> str:
    """Return the status text for an auto-fix step.

    A fix step reports the *action* it took, never a verdict: it applied fixes
    (``FIXED``), it had nothing to change (``NO CHANGES``), or it left unfixable
    issues for the follow-up gate step (``UNCHANGED``). "PASS" is reserved for the
    validation and test steps so a run that changed nothing never reads as passed.
    """
    if changed_files:
        file_word = "file" if len(changed_files) == 1 else "files"
        return f"FIXED ({elapsed:.1f}s, {len(changed_files)} {file_word})"
    if returncode == 0:
        return f"NO CHANGES ({elapsed:.1f}s)"
    return f"UNCHANGED ({elapsed:.1f}s, no automatic fixes applied)"


def collapse_blank_lines(lines: list[str]) -> list[str]:
    """Collapse repeated blank lines while preserving section breaks."""
    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = line.strip() == ""
        if is_blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = is_blank
    return collapsed
