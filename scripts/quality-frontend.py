#!/usr/bin/env python
"""Quality gate runner (frontend) --- formats, auto-fixes, then checks code quality.

Usage:
    python scripts/quality-frontend.py [paths...]

Paths can be files or directories, relative to the project root
(e.g. ``webui/src/components/Foo.svelte`` or just ``src/``).
If no paths are given, the full frontend is checked. Explicit test-file paths
are passed directly to vitest; other file paths are translated to their parent
directory. All npm commands
run with ``cwd="webui"``.
"""

import hashlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBUI_ROOT = PROJECT_ROOT / "webui"
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


def deduplicate_paths(paths: list[str]) -> list[str]:
    """Remove file paths already covered by a broader directory path.

    If both ``src/components/`` and ``src/components/Foo.svelte`` are
    given, keep only ``src/components/``.
    """
    dirs = [p for p in paths if not _has_extension(p)]
    files = [p for p in paths if _has_extension(p)]

    result = list(dirs)
    for fp in files:
        if not any(fp.startswith(d + "/") for d in dirs):
            result.append(fp)
    return result


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


def translate_to_vitest_targets(paths: list[str]) -> list[str]:
    """Translate input paths to the narrowest useful Vitest targets.

    Explicit test files stay file-scoped. Other file paths expand to their
    parent directory so Vitest can auto-discover nearby tests.
    """
    result: list[str] = []
    for p in paths:
        if _is_explicit_test_file(p):
            result.append(p)
        elif "/" in p and _has_extension(p):
            result.append(p.rsplit("/", 1)[0])
        else:
            result.append(p)
    return result


# ---------- vitest output parsing ----------

# vitest verbose reporter summary lines:
#   Tests  2 passed (2)
#   Tests  1 failed | 2 passed (3)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VITEST_FAILED_PASSED_RE = re.compile(r"Tests\s+(\d+)\s+failed\s+\|\s+(\d+)\s+passed\s+\((\d+)\)")
_VITEST_PASSED_RE = re.compile(r"Tests\s+(\d+)\s+passed\s+\((\d+)\)")


def parse_vitest_counts(output: str) -> tuple[int, int]:
    """Return ``(passed, total)`` from vitest verbose output."""
    cleaned_output = _ANSI_ESCAPE_RE.sub("", output)

    m = _VITEST_FAILED_PASSED_RE.search(cleaned_output)
    if m:
        return int(m.group(2)), int(m.group(3))

    m = _VITEST_PASSED_RE.search(cleaned_output)
    if m:
        return int(m.group(1)), int(m.group(2))

    return 0, 0


def hash_file(path: Path) -> str:
    """Return a stable content hash for *path*."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_snapshot_files(directory: Path) -> list[Path]:
    """Return snapshot-eligible frontend files under *directory*."""
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
        if entry.suffix in FRONTEND_FILE_SUFFIXES:
            files.append(entry)
    return files


def display_path(path: Path) -> str:
    """Return a stable project-relative path for console output."""
    if path.is_relative_to(PROJECT_ROOT):
        return path.relative_to(PROJECT_ROOT).as_posix()
    return path.as_posix()


def snapshot_target_files(paths: list[str]) -> dict[str, str]:
    """Return content hashes for fixable frontend files under the given targets."""
    snapshot: dict[str, str] = {}

    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = WEBUI_ROOT / raw_path
        candidate = candidate.resolve()

        if not candidate.exists():
            continue
        if candidate.is_file():
            if candidate.suffix in FRONTEND_FILE_SUFFIXES:
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

    collapsed = _collapse_blank_lines(filtered_lines)
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
    paths = deduplicate_paths(normalized)

    # Strip webui/ prefix — all npm commands run with cwd="webui".
    stripped = [strip_webui_prefix(p) for p in paths]

    # ---------- Build command lists ----------
    is_full_scan = len(stripped) == 0

    if stripped:
        prettier_paths = stripped
        eslint_fix_paths = stripped
        eslint_check_paths = stripped
        vitest_paths = translate_to_vitest_targets(stripped)
    else:
        prettier_paths = ["src/"]
        eslint_fix_paths = ["src/"]
        eslint_check_paths = ["src/"]
        vitest_paths = ["src/"]

    # Each step: (label, command, kind)
    # kind: "fix" = auto-fix (shows FIXED), "gate" = validation (PASS/FAIL),
    #       "test" = test runner with count display
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
            [npx_exe, "vitest", "run", "--reporter=verbose"] + vitest_paths,
            "test",
            None,
        ),
    ]

    # Build is always full-project — only run when no paths were given.
    if is_full_scan:
        steps.append(("build", [npm_exe, "run", "build"], "gate", None))

    title = "Quality Gates (Frontend)"
    print(title)
    print("=" * len(title))

    total_elapsed = 0.0
    validation_passed = True
    failures: list[tuple[str, str]] = []  # (label, full_output)

    for label, cmd, kind, snapshot_paths in steps:
        before_snapshot: dict[str, str] = {}
        if kind == "fix" and snapshot_paths is not None:
            before_snapshot = snapshot_target_files(snapshot_paths)

        start = time.monotonic()
        # Use errors="replace" and text=True to avoid UnicodeDecodeError on Windows.
        # Capture stdout and stderr properly.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd="webui",
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
                after_snapshot = snapshot_target_files(snapshot_paths)
                changed_files = changed_snapshot_paths(before_snapshot, after_snapshot)
            # prettier --write / eslint --fix
            # Exit code 1 from eslint --fix means "unfixable issues remain"
            # — that is expected; the follow-up eslint gate step catches them.
            if result.returncode <= 1:
                status = describe_fix_result(result.returncode, elapsed, changed_files)
            else:
                status = f"FAIL ({elapsed:.1f}s)"
                failures.append((label, output))
        elif kind == "test":
            passed, total = parse_vitest_counts(output)
            if result.returncode == 0:
                if total == 0:
                    status = f"PASS ({elapsed:.1f}s, no tests)"
                else:
                    status = f"PASS ({elapsed:.1f}s, {passed}/{total})"
            else:
                status = f"FAIL ({elapsed:.1f}s, {passed}/{total})"
                validation_passed = False
                failures.append((label, filter_vitest_failure_output(output)))
        else:
            # eslint / build
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
        # Count validation-gate failures (eslint, vitest, build) for the summary.
        failed_count = sum(1 for label, _ in failures if label not in ("prettier", "eslint fix"))
        gate_word = "s" if failed_count != 1 else ""
        print(f"{failed_count} gate{gate_word} failed in {total_elapsed:.1f}s.")

    return 0 if validation_passed else 1


if __name__ == "__main__":
    sys.exit(main())
