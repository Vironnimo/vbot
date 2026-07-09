#!/usr/bin/env python
"""Quality gate runner (frontend) --- formats, auto-fixes, then checks code quality.

Usage:
    python scripts/quality-frontend.py [paths...]

Paths can be files or directories, relative to the project root
(e.g. ``webui/src/components/Foo.svelte`` or just ``src/``).
If no paths are given, the full frontend is checked. Explicit test-file paths
are passed directly to vitest; a source file is translated to its mirrored
``__tests__`` test (searched from its own directory up to ``src/``), falling back
to the nearest ancestor directory that holds tests when it has no dedicated one.
All npm commands run with ``cwd="webui"``.
"""

import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
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

WEBUI_ROOT = PROJECT_ROOT / "webui"
SRC_ROOT = WEBUI_ROOT / "src"
FRONTEND_FILE_SUFFIXES = {
    ".cjs",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".scss",
    ".svelte",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
SNAPSHOT_IGNORED_DIRS = {
    ".git",
    ".svelte-kit",
    "build",
    "coverage",
    "dist",
    "node_modules",
}

# ---------- path helpers ----------


def _has_extension(path: str) -> bool:
    """Return True if the last segment of *path* looks like a file name."""
    return "." in path.rsplit("/", 1)[-1]


def strip_webui_prefix(path: str) -> str:
    """Remove the ``webui/`` prefix so paths are relative to the webui dir."""
    if path.startswith("webui/"):
        return path[len("webui/") :]
    return path


def _is_explicit_test_file(path: str) -> bool:
    """Return whether *path* is an explicit test file path for Vitest."""

    if not _has_extension(path):
        return False
    if "/__tests__/" in path:
        return True
    filename = path.rsplit("/", 1)[-1]
    return ".test." in filename or ".spec." in filename


# Test files live in ``__tests__`` dirs and carry one of these extensions.
TEST_FILE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}


def _looks_like_test_file(name: str) -> bool:
    """Return whether file *name* is a Vitest test file."""
    return ".test." in name or ".spec." in name


def _relative_to_webui(path: Path) -> str:
    """Return *path* as a posix string relative to the webui dir."""
    return path.relative_to(WEBUI_ROOT).as_posix()


def _iter_dirs_up_to_src(start_dir: Path) -> Iterator[Path]:
    """Yield *start_dir* and each parent up to and including ``src/``.

    Stops at ``src/`` and never climbs above it; yields nothing when *start_dir*
    is neither ``src/`` nor one of its descendants.
    """
    directory = start_dir
    while directory == SRC_ROOT or SRC_ROOT in directory.parents:
        yield directory
        if directory == SRC_ROOT:
            return
        directory = directory.parent


def _dir_has_tests(directory: Path) -> bool:
    """Return whether *directory*'s subtree contains any test file."""
    return any(
        entry.is_file() and _looks_like_test_file(entry.name) for entry in directory.rglob("*")
    )


def _find_named_test_files(stem: str, start_dir: Path) -> list[Path]:
    """Return the mirrored test files for source *stem*, nearest level first.

    Searches the ``__tests__`` dir at *start_dir* and each parent up to ``src/``
    for ``<stem>.test.*`` / ``<stem>.spec.*``, returning the matches from the
    nearest level that has any (empty when none exists). This is what lets a
    changed ``src/components/settings/SettingsProvidersPanel.svelte`` find its
    test in ``src/components/__tests__/`` one level up.
    """
    for directory in _iter_dirs_up_to_src(start_dir):
        tests_dir = directory / "__tests__"
        if not tests_dir.is_dir():
            continue
        matches = [
            entry
            for entry in sorted(tests_dir.iterdir())
            if entry.is_file()
            and entry.suffix in TEST_FILE_SUFFIXES
            and (entry.name.startswith(f"{stem}.test.") or entry.name.startswith(f"{stem}.spec."))
        ]
        if matches:
            return matches
    return []


def _nearest_ancestor_with_tests(start_dir: Path) -> Path | None:
    """Return the nearest dir (from *start_dir* up to ``src/``) that holds tests."""
    for directory in _iter_dirs_up_to_src(start_dir):
        if _dir_has_tests(directory):
            return directory
    return None


def translate_to_vitest_targets(paths: list[str]) -> tuple[list[str], list[str]]:
    """Translate input paths to the Vitest targets that actually cover them.

    Returns ``(targets, notes)``. Explicit test files stay file-scoped. A source
    file resolves to its mirrored test (``<stem>.test.*`` / ``.spec.*``) in a
    ``__tests__`` dir from the file's own directory up to ``src/``; a directory
    that holds tests runs directly. When a file has no dedicated test, or a
    directory keeps its tests one level up, the nearest ancestor directory that
    holds any tests runs instead so a broader suite still exercises it. Inputs
    with no tests anywhere become a note, not a Vitest argument, so the runner
    reports "no tests" honestly instead of a silent green pass.
    """
    targets: list[str] = []
    notes: list[str] = []

    def add(target: str) -> None:
        if target not in targets:
            targets.append(target)

    for p in paths:
        if _is_explicit_test_file(p):
            add(p)
            continue

        absolute = WEBUI_ROOT / p

        if absolute.is_dir():
            if _dir_has_tests(absolute):
                add(p)
                continue
            ancestor = _nearest_ancestor_with_tests(absolute)
            if ancestor is None:
                notes.append(f"{p}: no tests found")
            else:
                relative = _relative_to_webui(ancestor)
                add(relative)
                notes.append(f"{p}: no tests here, running {relative}/ instead")
            continue

        filename = p.rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0]
        named = _find_named_test_files(stem, absolute.parent)
        if named:
            for test_file in named:
                add(_relative_to_webui(test_file))
            continue

        ancestor = _nearest_ancestor_with_tests(absolute.parent)
        if ancestor is None:
            notes.append(f"{p}: no tests found")
        else:
            relative = _relative_to_webui(ancestor)
            add(relative)
            notes.append(f"{p}: no {stem} test, running {relative}/ instead")

    return deduplicate_paths(targets, _has_extension), notes


# ---------- vitest output parsing ----------

# vitest summary line, with any combination of segments:
#   Tests  2 passed (2)
#   Tests  1 failed | 2 passed (3)
#   Tests  1 skipped | 4 passed (5)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VITEST_TESTS_SUMMARY_RE = re.compile(
    r"^\s*Tests\s+(?P<details>[^()\n]+)\((?P<total>\d+)\)\s*$", re.MULTILINE
)
_VITEST_PASSED_COUNT_RE = re.compile(r"(\d+)\s+passed")


def parse_vitest_counts(output: str) -> tuple[int, int]:
    """Return ``(passed, total)`` from vitest verbose output."""
    cleaned_output = _ANSI_ESCAPE_RE.sub("", output)

    summary = _VITEST_TESTS_SUMMARY_RE.search(cleaned_output)
    if summary is None:
        return 0, 0

    passed_match = _VITEST_PASSED_COUNT_RE.search(summary.group("details"))
    passed = int(passed_match.group(1)) if passed_match else 0
    return passed, int(summary.group("total"))


def filter_vitest_failure_output(output: str) -> str:
    """Remove passing-test noise from Vitest output while keeping failure detail."""

    cleaned_output = _ANSI_ESCAPE_RE.sub("", output)
    filtered_lines: list[str] = []

    for line in cleaned_output.splitlines():
        stripped = line.strip()
        if not stripped:
            filtered_lines.append("")
            continue
        if stripped.startswith("RUN ") or stripped.startswith("DEV "):
            continue
        if stripped.startswith("✓"):
            continue
        filtered_lines.append(line.rstrip())

    collapsed = collapse_blank_lines(filtered_lines)
    filtered_output = "\n".join(collapsed).strip()
    return filtered_output or cleaned_output.strip()


# ---------- main ----------


def main() -> int:
    # Resolve full executable paths so subprocess.run works on Windows
    # (CreateProcess cannot find .cmd executables without shell resolution).
    npx_exe = shutil.which("npx")
    npm_exe = shutil.which("npm")
    if not npx_exe or not npm_exe:
        print("ERROR: npx and/or npm not found on PATH.", file=sys.stderr)
        return 1

    raw_paths: list[str] = sys.argv[1:]

    # Normalize: backslash → forward slash, strip trailing slash.
    normalized = [p.replace("\\", "/").rstrip("/") for p in raw_paths]
    paths = deduplicate_paths(normalized, _has_extension)

    # Strip webui/ prefix — all npm commands run with cwd=WEBUI_ROOT.
    stripped = [strip_webui_prefix(p) for p in paths]

    # Reject unknown paths before running anything: a typo would otherwise
    # surface as a confusing tool error instead of a clear message.
    missing_inputs = [p for p in stripped if not (WEBUI_ROOT / p).exists()]
    if missing_inputs:
        for missing in missing_inputs:
            print(f"ERROR: path not found under webui/: {missing}")
        return 2

    # ---------- Build command lists ----------
    is_full_scan = len(stripped) == 0

    if stripped:
        prettier_paths = stripped
        eslint_fix_paths = stripped
        eslint_check_paths = stripped
        vitest_paths, vitest_notes = translate_to_vitest_targets(stripped)
    else:
        prettier_paths = ["src/"]
        eslint_fix_paths = ["src/"]
        eslint_check_paths = ["src/"]
        vitest_paths = ["src/"]
        vitest_notes = []

    # Each step: (label, command, kind)
    # kind: "fix" = auto-fix (shows FIXED), "gate" = validation (PASS/FAIL),
    #       "test" = test runner with count display,
    #       "build" = full build; surfaces stderr warnings on success without failing
    steps: list[tuple[str, list[str], str, list[str] | None]] = [
        (
            "prettier",
            [npx_exe, "prettier", "--write"] + prettier_paths,
            "fix",
            prettier_paths,
        ),
        (
            "eslint fix",
            [npx_exe, "eslint", "--fix"] + eslint_fix_paths,
            "fix",
            eslint_fix_paths,
        ),
        ("eslint", [npx_exe, "eslint"] + eslint_check_paths, "gate", None),
        (
            "vitest",
            # --passWithNoTests: a path filter without nearby tests must not
            # fail the gate (vitest exits 1 on "No test files found").
            [npx_exe, "vitest", "run", "--reporter=verbose", "--passWithNoTests"] + vitest_paths,
            "test",
            None,
        ),
    ]

    # Build is always full-project — only run when no paths were given.
    if is_full_scan:
        steps.append(("build", [npm_exe, "run", "build"], "build", None))

    title = "Quality Gates (Frontend)"
    print(title)
    print("=" * len(title))

    total_elapsed = 0.0
    validation_passed = True
    failures: list[tuple[str, str]] = []  # (label, full_output)
    build_warnings: list[tuple[str, str]] = []  # (label, stderr) — non-fatal

    for label, cmd, kind, snapshot_paths in steps:
        # A scoped run whose inputs map to no test files must not fall through to
        # ``vitest`` with no path argument — that would silently run the whole
        # suite. Report the honest "no tests" outcome and move on.
        if kind == "test" and not vitest_paths:
            print(f"{label:<14}.... NO TESTS (nothing to run)")
            for note in vitest_notes:
                print(f"{'':<18}note: {note}")
            continue

        before_snapshot: dict[str, str] = {}
        if kind == "fix" and snapshot_paths is not None:
            before_snapshot = snapshot_target_files(
                snapshot_paths, WEBUI_ROOT, FRONTEND_FILE_SUFFIXES, SNAPSHOT_IGNORED_DIRS
            )

        start = time.monotonic()
        # Use errors="replace" and text=True to avoid UnicodeDecodeError on Windows.
        # Capture stdout and stderr properly.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=WEBUI_ROOT,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.monotonic() - start
        total_elapsed += elapsed
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        output = output.strip()
        changed_files: list[str] = []

        if kind == "fix":
            if snapshot_paths is not None:
                after_snapshot = snapshot_target_files(
                    snapshot_paths, WEBUI_ROOT, FRONTEND_FILE_SUFFIXES, SNAPSHOT_IGNORED_DIRS
                )
                changed_files = changed_snapshot_paths(before_snapshot, after_snapshot)
            # prettier --write / eslint --fix
            # Exit code 1 from eslint --fix means "unfixable issues remain"
            # — that is expected; the follow-up eslint gate step catches them.
            if result.returncode <= 1:
                status = describe_fix_result(result.returncode, elapsed, changed_files)
            else:
                status = f"FAIL ({elapsed:.1f}s)"
                validation_passed = False
                failures.append((label, output))
        elif kind == "test":
            passed, total = parse_vitest_counts(output)
            if result.returncode == 0:
                if total == 0:
                    status = f"NO TESTS ({elapsed:.1f}s)"
                else:
                    status = f"PASS ({elapsed:.1f}s, {passed}/{total})"
            else:
                status = f"FAIL ({elapsed:.1f}s, {passed}/{total})"
                validation_passed = False
                failures.append((label, filter_vitest_failure_output(output)))
        elif kind == "build":
            # A successful build still emits warnings (oversized chunks, a11y,
            # unused CSS, deprecations) on stderr without changing the exit code.
            # Surface those — they are the whole reason to look here — but never
            # let them fail the gate. The asset-list noise stays on stdout, so a
            # clean build prints nothing extra.
            if result.returncode == 0:
                warning_text = _ANSI_ESCAPE_RE.sub("", result.stderr or "").strip()
                if warning_text:
                    status = f"PASS ({elapsed:.1f}s, warnings)"
                    build_warnings.append((label, warning_text))
                else:
                    status = f"PASS ({elapsed:.1f}s)"
            else:
                status = f"FAIL ({elapsed:.1f}s)"
                validation_passed = False
                failures.append((label, output))
        else:
            # eslint
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
        if kind == "test":
            for note in vitest_notes:
                print(f"{'':<18}note: {note}")

    print()

    # Show complete output for every failed step.
    if failures:
        for label, output in failures:
            print(f"--- {label} ---")
            if output:
                print(output)
            print()

    # Surface non-fatal build warnings — the build still passed.
    if build_warnings:
        for label, warning_text in build_warnings:
            print(f"--- {label} (warnings) ---")
            print(warning_text)
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
