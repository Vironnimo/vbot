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

# Tool output is decoded as UTF-8, but Windows consoles often use a legacy
# code page that cannot encode every character — degrade those to "?" instead
# of crashing the runner mid-report.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

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
    # pytest-timeout header lines only — not arbitrary output starting with "timeout".
    re.compile(r"^timeout: \d"),
    re.compile(r"^timeout method:"),
    re.compile(r"^timeout func_only:"),
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


MIRRORED_TEST_PACKAGES = ("cli", "core", "desktop", "scripts", "server")


def _normalized_stem(name: str) -> str:
    """Return a module-comparable stem: test modules turn hyphens into ``_``.

    ``scripts/quality-frontend.py`` mirrors to ``test_quality_frontend.py``, so
    source stems must be normalized before matching against test-file names.
    """
    return name.replace("-", "_")


def _source_stems(source_dir: Path) -> list[str]:
    """Return normalized source-file stems in *source_dir*, longest first.

    Longest-first ordering lets :func:`_owning_source_stem` resolve a split test
    file like ``test_openai_compatible_oauth`` to the most specific source
    (``openai_compatible``) rather than a shorter prefix (``openai``).
    """
    if not source_dir.is_dir():
        return []
    stems = {_normalized_stem(entry.stem) for entry in source_dir.glob("*.py")}
    return sorted(stems, key=len, reverse=True)


def _owning_source_stem(test_rest: str, source_stems: list[str]) -> str | None:
    """Return the source stem that owns a ``test_<test_rest>.py`` file, or None.

    A test file belongs to source stem ``S`` when *test_rest* is exactly ``S``
    or begins with ``S_`` (a split sibling such as ``test_<S>_oauth``). With
    *source_stems* ordered longest-first, the first match is the most specific
    owner, so ``test_openai_compatible_oauth`` resolves to ``openai_compatible``
    rather than ``openai``.
    """
    for stem in source_stems:
        if test_rest == stem or test_rest.startswith(stem + "_"):
            return stem
    return None


def _owned_test_files(directory: str, stem: str) -> list[str]:
    """Return mirror test files owned by source *stem* in *directory*.

    Includes the exact mirror ``test_<stem>.py`` and any split siblings
    ``test_<stem>_*.py`` that no more-specific source file claims. Returns
    sorted project-relative posix paths; empty when the mirror directory is
    absent or holds no test file owned by *stem*.
    """
    mirror_dir = f"tests/{directory}" if directory else "tests"
    mirror_path = PROJECT_ROOT / mirror_dir
    if not mirror_path.is_dir():
        return []
    source_dir = PROJECT_ROOT / directory if directory else PROJECT_ROOT
    source_stems = _source_stems(source_dir)
    target_stem = _normalized_stem(stem)
    owned: list[str] = []
    for test_file in sorted(mirror_path.glob("test_*.py")):
        test_rest = test_file.stem[len("test_") :]
        if _owning_source_stem(test_rest, source_stems) == target_stem:
            owned.append(f"{mirror_dir}/{test_file.name}")
    return owned


def translate_to_test_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """Translate source paths to existing mirrored test paths.

    Returns ``(test_paths, notes)``. A source file maps to its exact mirror
    ``tests/<package>/<...>/test_<file>.py`` **plus** any split-sibling test
    files ``test_<file>_*.py`` in the same directory that no more-specific
    source file owns (e.g. ``openai_compatible.py`` also runs
    ``test_openai_compatible_oauth.py``). When no owned test file exists, the
    mirrored test directory runs instead so related tests are still exercised.
    Paths without any mirrored tests become a note instead of a pytest argument:
    a nonexistent path makes pytest-xdist collect zero items overall and
    silently skip even the valid paths next to it.
    """
    test_paths: list[str] = []
    notes: list[str] = []

    def add(path: str) -> None:
        if path not in test_paths:
            test_paths.append(path)

    for p in paths:
        if p == "tests" or p.startswith("tests/"):
            add(p)
            continue

        package = p.split("/", 1)[0]
        if package not in MIRRORED_TEST_PACKAGES:
            notes.append(f"{p}: not under a mirrored test package, no tests selected")
            continue

        if p.endswith(".py"):
            directory, _, filename = p.rpartition("/")
            stem = filename[: -len(".py")]
            mirror_dir = f"tests/{directory}" if directory else "tests"
            owned = _owned_test_files(directory, stem)
            if owned:
                for mirror_file in owned:
                    add(mirror_file)
            elif (PROJECT_ROOT / mirror_dir).is_dir():
                notes.append(f"{p}: no test_{stem}*.py, running {mirror_dir}/ instead")
                add(mirror_dir)
            else:
                notes.append(f"{p}: no mirrored tests (test_{stem}*.py and {mirror_dir}/ missing)")
            continue

        mirror_dir = f"tests/{p}"
        if (PROJECT_ROOT / mirror_dir).is_dir():
            add(mirror_dir)
        else:
            notes.append(f"{p}: no mirrored test directory {mirror_dir}/")

    return test_paths, notes


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

    # Reject unknown paths before running anything: a typo would otherwise
    # surface as a confusing tool error (or worse, as a silently green run).
    missing_inputs = [p for p in paths if not (PROJECT_ROOT / p).exists()]
    if missing_inputs:
        for missing in missing_inputs:
            print(f"ERROR: path not found: {missing}")
        return 2

    # ---------- Build command lists ----------
    if paths:
        ruff_fmt_paths = paths
        ruff_fix_paths = paths
        ruff_check_paths = paths
        mypy_paths = paths
        test_paths, test_notes = translate_to_test_paths(paths)
    else:
        ruff_fmt_paths = ["."]
        ruff_fix_paths = ["."]
        ruff_check_paths = ["."]
        mypy_paths = ["core/", "server/", "cli/", "desktop/", "tests/"]
        test_paths = ["tests/"]
        test_notes = []

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
        # Without any mirrored test path, running pytest with no arguments
        # would execute the full suite — skip explicitly instead.
        if kind == "pytest" and not test_paths:
            print(f"{label:<14}.... SKIP (no mirrored tests)")
            for note in test_notes:
                print(f"{'':<18}note: {note}")
            continue

        before_snapshot: dict[str, str] = {}
        if kind == "fix" and snapshot_paths is not None:
            before_snapshot = snapshot_target_files(snapshot_paths)

        start = time.monotonic()
        # ruff/mypy/pytest emit UTF-8 regardless of the console code page.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            encoding="utf-8",
            errors="replace",
        )
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
                validation_passed = False
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
        if kind == "pytest":
            for note in test_notes:
                print(f"{'':<18}note: {note}")

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
        failed_count = len(failures)
        gate_word = "s" if failed_count != 1 else ""
        print(f"{failed_count} gate{gate_word} failed in {total_elapsed:.1f}s.")

    return 0 if validation_passed else 1


if __name__ == "__main__":
    sys.exit(main())
