#!/usr/bin/env python
"""Quality gate runner --- formats, auto-fixes, then checks code quality.

Usage:
    python scripts/quality.py [paths...]

Paths can be files or directories. If no paths are given, the full project
is checked. Direct files are routed only to tools that explicitly own their
format; directories remain mixed scopes. Python files (e.g.
``core/utils/config.py``) are translated to their corresponding test paths
(``tests/core/utils/test_config.py``) for pytest.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

from _quality_common import (
    PROJECT_ROOT,
    changed_snapshot_paths,
    collapse_blank_lines,
    configure_console_encoding,
    deduplicate_paths,
    describe_fix_result,
    snapshot_target_files,
)

configure_console_encoding()

PYTHON_FILE_SUFFIXES = {".py", ".pyi"}
PYTHON_CONFIG_FILES = {"pyproject.toml"}
FULL_MYPY_PATHS = ["core/", "server/", "cli/", "desktop/", "tests/"]
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
PYTEST_PROFILE_COUNT = 25


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

        suffix = Path(p).suffix
        if suffix in PYTHON_FILE_SUFFIXES:
            directory, _, filename = p.rpartition("/")
            stem = filename[: -len(suffix)]
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


def _is_python_config(path: str) -> bool:
    """Return whether *path* configures the whole Python quality pipeline."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve() in {
        (PROJECT_ROOT / config_path).resolve() for config_path in PYTHON_CONFIG_FILES
    }


def _unsupported_direct_files(paths: list[str]) -> list[str]:
    """Return explicit files for which no backend quality capability is registered."""
    unsupported: list[str] = []
    for path in paths:
        candidate = PROJECT_ROOT / path
        if (
            candidate.is_file()
            and candidate.suffix not in PYTHON_FILE_SUFFIXES
            and not _is_python_config(path)
        ):
            unsupported.append(path)
    return unsupported


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

    collapsed = collapse_blank_lines(filtered_lines)
    filtered_output = "\n".join(collapsed).strip()
    return filtered_output or output.strip()


def extract_pytest_profile_output(output: str) -> str:
    """Return pytest's slowest-duration section without its summary footer."""

    profile_lines: list[str] = []
    in_profile = False
    for line in output.splitlines():
        stripped = line.strip()
        if not in_profile:
            if stripped.startswith("=") and " slowest " in stripped and " durations " in stripped:
                in_profile = True
                profile_lines.append(line.rstrip())
            continue
        if stripped.startswith("=") and stripped.endswith("="):
            break
        profile_lines.append(line.rstrip())
    return "\n".join(profile_lines).strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Run the backend quality pipeline over the whole project or selected paths."),
        epilog="""Modes:
  default       Auto-format and auto-fix before validating.
  --check       Validate without changing source files. Formatting differences fail.

Pipeline:
  ruff format -> ruff check -> mypy -> pytest
  The default mode adds Ruff's auto-fix pass. --check uses `ruff format --check`.

Path behavior:
  With no PATH, run the complete backend gate. PATH values are project-root-relative
  files or directories and are deduplicated. Python source paths select their mirrored
  pytest tests; pyproject.toml selects the complete Python pipeline. Missing paths and
  direct files without a registered quality capability abort before any tool runs.

Notes:
  The default mode keeps and reports every source-file change made by Ruff. --check
  does not modify source files, although underlying tools may still write caches.
  --profile prints pytest's 25 slowest setup/call/teardown durations.

Exit codes:
  0  All gates passed.
  1  One or more gates failed.
  2  Invalid arguments, paths, or unsupported direct files.

Examples:
  python scripts/quality.py
  python scripts/quality.py --check
  python scripts/quality.py core/runtime/
  python scripts/quality.py --check core/utils/config.py tests/core/utils/""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate without applying formatter or linter fixes",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help=f"show pytest's {PYTEST_PROFILE_COUNT} slowest test durations",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="project-root-relative file or directory; omit for the complete gate",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    raw_paths: list[str] = args.paths

    # Normalize: backslash → forward slash, strip trailing slash.
    normalized = [p.replace("\\", "/").rstrip("/") for p in raw_paths]

    # Reject unknown paths before running anything: a typo would otherwise
    # surface as a confusing tool error (or worse, as a silently green run).
    missing_inputs = [p for p in normalized if not (PROJECT_ROOT / p).exists()]
    if missing_inputs:
        for missing in missing_inputs:
            print(f"ERROR: path not found: {missing}")
        return 2

    paths = deduplicate_paths(normalized, lambda path: (PROJECT_ROOT / path).is_file())
    unsupported_files = _unsupported_direct_files(paths)
    if unsupported_files:
        for unsupported in unsupported_files:
            print(
                f"ERROR: no backend quality capability is registered for direct file: {unsupported}"
            )
        print("Add a format-specific quality route before gating these files.")
        return 2

    # ---------- Build command lists ----------
    if paths:
        if any(_is_python_config(path) for path in paths):
            ruff_fmt_paths = ["."]
            ruff_fix_paths = ["."]
            ruff_check_paths = ["."]
            mypy_paths = FULL_MYPY_PATHS
            test_paths = ["tests/"]
            test_notes = ["pyproject.toml configures the full Python pipeline"]
        else:
            ruff_fmt_paths = paths
            ruff_fix_paths = paths
            ruff_check_paths = paths
            mypy_paths = paths
            test_paths, test_notes = translate_to_test_paths(paths)
    else:
        ruff_fmt_paths = ["."]
        ruff_fix_paths = ["."]
        ruff_check_paths = ["."]
        mypy_paths = FULL_MYPY_PATHS
        test_paths = ["tests/"]
        test_notes = []

    # Each step: (label, command, kind)
    # kind: "fix" = auto-fix (shows FIXED), "gate" = validation (PASS/FAIL),
    #       "pytest" = test runner with count display
    steps: list[tuple[str, list[str], str, list[str] | None]]
    if args.check:
        steps = [
            (
                "ruff format",
                [sys.executable, "-m", "ruff", "format", "--check"] + ruff_fmt_paths,
                "gate",
                None,
            )
        ]
    else:
        steps = [
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
        ]
    pytest_command = [
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "--tb=short",
        "--timeout=30",
    ]
    if args.profile:
        pytest_command.append(f"--durations={PYTEST_PROFILE_COUNT}")
    pytest_command.extend(test_paths)

    steps.extend(
        [
            (
                "ruff check",
                [sys.executable, "-m", "ruff", "check"] + ruff_check_paths,
                "gate",
                None,
            ),
            ("mypy", [sys.executable, "-m", "mypy", "--pretty"] + mypy_paths, "gate", None),
            (
                "pytest",
                pytest_command,
                "pytest",
                None,
            ),
        ]
    )

    print("Quality Gates")
    print("=============")

    total_elapsed = 0.0
    validation_passed = True
    failures: list[tuple[str, str]] = []  # (label, full_output)

    for label, cmd, kind, snapshot_paths in steps:
        # Without any mirrored test path, running pytest with no arguments
        # would execute the full suite — skip explicitly instead.
        if kind == "pytest" and not test_paths:
            print(f"{label:<14}.... NO TESTS (nothing mirrored)")
            for note in test_notes:
                print(f"{'':<18}note: {note}")
            continue

        before_snapshot: dict[str, str] = {}
        if kind == "fix" and snapshot_paths is not None:
            before_snapshot = snapshot_target_files(
                snapshot_paths, PROJECT_ROOT, PYTHON_FILE_SUFFIXES, SNAPSHOT_IGNORED_DIRS
            )

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
                after_snapshot = snapshot_target_files(
                    snapshot_paths, PROJECT_ROOT, PYTHON_FILE_SUFFIXES, SNAPSHOT_IGNORED_DIRS
                )
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
                    status = f"NO TESTS ({elapsed:.1f}s)"
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
            if args.profile and result.returncode in {0, 5}:
                profile_output = extract_pytest_profile_output(output)
                if profile_output:
                    print()
                    print("--- pytest profile ---")
                    print(profile_output)

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
