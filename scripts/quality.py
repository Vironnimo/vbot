#!/usr/bin/env python
"""Quality gate runner --- formats, auto-fixes, then checks code quality.

Usage:
    python scripts/quality.py [paths...]

Paths can be files or directories. If no paths are given, the full project
is checked. File paths (e.g. ``core/utils/config.py``) are translated to
their corresponding test paths (``tests/core/utils/test_config.py``) for
pytest; ruff and mypy receive them directly.
"""

import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_FILE_SUFFIXES = {".py"}
SNAPSHOT_IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
PYTEST_NOISE_LINE_PATTERNS = [
    re.compile(r"^=+ test session starts =+$"),
    re.compile(r"^platform "),
    re.compile(r"^cachedir:"),
    re.compile(r"^rootdir:"),
    re.compile(r"^configfile:"),
    re.compile(r"^plugins:"),
    re.compile(r"^asyncio:"),
    re.compile(r"^timeout"),
    re.compile(r"^\d+ workers\b"),
    re.compile(r"^scheduling tests via "),
]
PYTEST_RESULT_TOKENS = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
PYTEST_PROGRESS_NODEID_PATTERN = re.compile(
    r"^(?:\[[^\]]+\]\s+)*(?:\[\s*\d+%\]\s+)?[^:\s][^\s]*::[^\s]+$"
)


def deduplicate_paths(paths: list[str]) -> list[str]:
    """Remove file paths already covered by a broader directory path.

    If both ``core/utils/`` and ``core/utils/config.py`` are given, keep
    only ``core/utils/``.
    """
    # Separate probable directories from files by extension.
    dirs = [p for p in paths if not p.endswith(".py")]
    files = [p for p in paths if p.endswith(".py")]

    result = list(dirs)
    for fp in files:
        if not any(fp.startswith(d + "/") for d in dirs):
            result.append(fp)
    return result


def translate_to_test_paths(paths: list[str]) -> list[str]:
    """Translate source paths to their corresponding test paths.

    Rule for core/ paths:
    - ``core/runtime/``   → ``tests/core/runtime/``
    - ``core/utils/config.py`` → ``tests/core/utils/test_config.py``

    Paths already under ``tests/`` are passed through unchanged.
    Everything else is passed through unchanged.
    """
    result: list[str] = []
    for p in paths:
        if p.startswith("core/"):
            rest = p[len("core/") :]
            if rest.endswith(".py"):
                if "/" in rest:
                    dir_part, filename = rest.rsplit("/", 1)
                    result.append(f"tests/core/{dir_part}/test_{filename}")
                else:
                    result.append(f"tests/core/test_{rest}")
            else:
                result.append(f"tests/core/{rest}")
        elif p.startswith("tests/"):
            result.append(p)
        else:
            result.append(p)
    return result


def parse_pytest_counts(output: str) -> tuple[int, int, int]:
    """Return (passed, failed, errors) counts from pytest output."""
    passed_match = re.search(r"(\d+) passed", output)
    failed_match = re.search(r"(\d+) failed", output)
    errors_match = re.search(r"(\d+) error", output)
    return (
        int(passed_match.group(1)) if passed_match else 0,
        int(failed_match.group(1)) if failed_match else 0,
        int(errors_match.group(1)) if errors_match else 0,
    )


def hash_file(path: Path) -> str:
    """Return a stable content hash for *path*."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_snapshot_files(directory: Path) -> list[Path]:
    """Return Python files under *directory*, skipping ignored folders."""
    files: list[Path] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return files

    for entry in entries:
        if entry.is_dir():
            if entry.name in SNAPSHOT_IGNORED_DIRS:
                continue
            files.extend(iter_snapshot_files(entry))
            continue
        if entry.suffix in PYTHON_FILE_SUFFIXES:
            files.append(entry)
    return files


def display_path(path: Path) -> str:
    """Return a stable project-relative path for console output."""
    if path.is_relative_to(PROJECT_ROOT):
        return path.relative_to(PROJECT_ROOT).as_posix()
    return path.as_posix()


def snapshot_target_files(paths: list[str]) -> dict[str, str]:
    """Return content hashes for fixable files under the given targets."""
    snapshot: dict[str, str] = {}

    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / raw_path
        candidate = candidate.resolve()

        if not candidate.exists():
            continue
        if candidate.is_file():
            if candidate.suffix in PYTHON_FILE_SUFFIXES:
                snapshot[display_path(candidate)] = hash_file(candidate)
            continue

        for file_path in iter_snapshot_files(candidate):
            snapshot[display_path(file_path)] = hash_file(file_path)

    return snapshot


def changed_snapshot_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return sorted file paths whose content changed between two snapshots."""
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


def describe_fix_result(returncode: int, elapsed: float, changed_files: list[str]) -> str:
    """Return the status text for an auto-fix step."""
    if changed_files:
        file_word = "file" if len(changed_files) == 1 else "files"
        return f"FIXED ({elapsed:.1f}s, {len(changed_files)} {file_word})"
    if returncode == 0:
        return f"PASS ({elapsed:.1f}s, no fixes needed)"
    return f"UNCHANGED ({elapsed:.1f}s, no automatic fixes applied)"


def _collapse_blank_lines(lines: list[str]) -> list[str]:
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


def _is_pytest_progress_nodeid_line(stripped_line: str) -> bool:
    """Return whether *stripped_line* is a verbose-progress node id entry."""

    if "::" not in stripped_line:
        return False
    if stripped_line.startswith(PYTEST_RESULT_TOKENS):
        return False
    if any(f" {token}" in stripped_line for token in PYTEST_RESULT_TOKENS):
        return False
    return bool(PYTEST_PROGRESS_NODEID_PATTERN.match(stripped_line))


def filter_pytest_failure_output(output: str) -> str:
    """Remove pytest success noise while keeping all failure details."""

    filtered_lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            filtered_lines.append("")
            continue
        if any(pattern.match(stripped) for pattern in PYTEST_NOISE_LINE_PATTERNS):
            continue
        if _is_pytest_progress_nodeid_line(stripped):
            continue
        if " PASSED" in stripped and "FAILED" not in stripped and "ERROR" not in stripped:
            continue
        filtered_lines.append(line.rstrip())

    collapsed = _collapse_blank_lines(filtered_lines)
    filtered_output = "\n".join(collapsed).strip()
    return filtered_output or output.strip()


def main() -> int:
    raw_paths: list[str] = sys.argv[1:]

    # Normalize: backslash → forward slash, strip trailing slash.
    normalized = [p.replace("\\", "/").rstrip("/") for p in raw_paths]
    paths = deduplicate_paths(normalized)

    # ---------- Build command lists ----------
    if paths:
        ruff_fmt_paths = paths
        ruff_fix_paths = paths
        ruff_check_paths = paths
        mypy_paths = paths
        test_paths = translate_to_test_paths(paths)
    else:
        ruff_fmt_paths = ["."]
        ruff_fix_paths = ["."]
        ruff_check_paths = ["."]
        mypy_paths = ["core/", "server/", "cli/", "desktop/", "tests/"]
        test_paths = ["tests/"]

    # Each step: (label, command, kind)
    # kind: "fix" = auto-fix (shows FIXED), "gate" = validation (PASS/FAIL),
    #       "pytest" = test runner with count display
    steps: list[tuple[str, list[str], str, list[str] | None]] = [
        (
            "ruff format",
            [sys.executable, "-m", "ruff", "format"] + ruff_fmt_paths,
            "fix",
            ruff_fmt_paths,
        ),
        (
            "ruff fix",
            [sys.executable, "-m", "ruff", "check", "--fix"] + ruff_fix_paths,
            "fix",
            ruff_fix_paths,
        ),
        ("ruff check", [sys.executable, "-m", "ruff", "check"] + ruff_check_paths, "gate", None),
        ("mypy", [sys.executable, "-m", "mypy", "--pretty"] + mypy_paths, "gate", None),
        (
            "pytest",
            [sys.executable, "-m", "pytest", "-v", "--tb=short", "--timeout=30"] + test_paths,
            "pytest",
            None,
        ),
    ]

    print("Quality Gates")
    print("=============")

    total_elapsed = 0.0
    validation_passed = True
    failures: list[tuple[str, str]] = []  # (label, full_output)

    for label, cmd, kind, snapshot_paths in steps:
        before_snapshot: dict[str, str] = {}
        if kind == "fix" and snapshot_paths is not None:
            before_snapshot = snapshot_target_files(snapshot_paths)

        start = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.monotonic() - start
        total_elapsed += elapsed
        output = (result.stdout + result.stderr).strip()
        changed_files: list[str] = []

        if kind == "fix":
            if snapshot_paths is not None:
                after_snapshot = snapshot_target_files(snapshot_paths)
                changed_files = changed_snapshot_paths(before_snapshot, after_snapshot)
            # ruff format / ruff check --fix
            # Exit code 1 means "unfixable issues remain" — that's fine,
            # the follow-up `ruff check` step will catch them with full detail.
            if result.returncode <= 1:
                status = describe_fix_result(result.returncode, elapsed, changed_files)
            else:
                status = f"FAIL ({elapsed:.1f}s)"
                failures.append((label, output))
        elif kind == "pytest":
            passed, failed, errors = parse_pytest_counts(output)
            total = passed + failed + errors
            # Exit code 5 = "no tests collected" — not a failure, just nothing to run.
            if result.returncode == 0 or result.returncode == 5:
                if total == 0:
                    status = f"PASS ({elapsed:.1f}s, no tests)"
                else:
                    status = f"PASS ({elapsed:.1f}s, {passed}/{total})"
            else:
                status = f"FAIL ({elapsed:.1f}s, {passed}/{total})"
                validation_passed = False
                failures.append((label, filter_pytest_failure_output(output)))
        else:
            # ruff check / mypy
            if result.returncode == 0:
                status = f"PASS ({elapsed:.1f}s)"
            else:
                status = f"FAIL ({elapsed:.1f}s)"
                validation_passed = False
                failures.append((label, output))

        print(f"{label:<14}.... {status}")
        if changed_files:
            for changed_path in changed_files:
                print(f"{'':<18}{changed_path}")

    print()

    # Show complete output for every failed step.
    if failures:
        for label, output in failures:
            print(f"--- {label} ---")
            if output:
                print(output)
            print()

    if validation_passed:
        print(f"All gates passed in {total_elapsed:.1f}s.")
    else:
        # Count validation-gate failures (ruff check, mypy, pytest) for the summary.
        failed_count = sum(1 for label, _ in failures if label not in ("ruff format", "ruff fix"))
        gate_word = "s" if failed_count != 1 else ""
        print(f"{failed_count} gate{gate_word} failed in {total_elapsed:.1f}s.")

    return 0 if validation_passed else 1


if __name__ == "__main__":
    sys.exit(main())
