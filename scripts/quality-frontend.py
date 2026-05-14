#!/usr/bin/env python
"""Quality gate runner (frontend) --- formats, auto-fixes, then checks code quality.

Usage:
    python scripts/quality-frontend.py [paths...]

Paths can be files or directories, relative to the project root
(e.g. ``webui/src/components/Foo.svelte`` or just ``src/``).
If no paths are given, the full frontend is checked.  File paths are
translated to their parent directory for vitest.  All npm commands
run with ``cwd="webui"``.
"""

import re
import shutil
import subprocess
import sys
import time

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


def translate_to_parent_dir(paths: list[str]) -> list[str]:
    """Translate file paths to their parent directory for vitest.

    vitest auto-discovers ``__tests__/`` subdirectories, so passing the
    parent directory of a specific file is the right scope.
    """
    result: list[str] = []
    for p in paths:
        if "/" in p and _has_extension(p):
            result.append(p.rsplit("/", 1)[0])
        else:
            result.append(p)
    return result


# ---------- vitest output parsing ----------

# vitest verbose reporter summary lines:
#   Tests  2 passed (2)
#   Tests  1 failed | 2 passed (3)

_VITEST_FAILED_PASSED_RE = re.compile(r"Tests\s+(\d+)\s+failed\s+\|\s+(\d+)\s+passed\s+\((\d+)\)")
_VITEST_PASSED_RE = re.compile(r"Tests\s+(\d+)\s+passed\s+\((\d+)\)")


def parse_vitest_counts(output: str) -> tuple[int, int]:
    """Return ``(passed, total)`` from vitest verbose output."""
    m = _VITEST_FAILED_PASSED_RE.search(output)
    if m:
        return int(m.group(2)), int(m.group(3))

    m = _VITEST_PASSED_RE.search(output)
    if m:
        return int(m.group(1)), int(m.group(2))

    return 0, 0


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
        vitest_paths = translate_to_parent_dir(stripped)
    else:
        prettier_paths = ["src/"]
        eslint_fix_paths = ["src/"]
        eslint_check_paths = ["src/"]
        vitest_paths = ["src/"]

    # Each step: (label, command, kind)
    # kind: "fix" = auto-fix (shows FIXED), "gate" = validation (PASS/FAIL),
    #       "test" = test runner with count display
    steps: list[tuple[str, list[str], str]] = [
        ("prettier", [npx_exe, "prettier", "--write"] + prettier_paths, "fix"),
        ("eslint fix", [npx_exe, "eslint", "--fix"] + eslint_fix_paths, "fix"),
        ("eslint", [npx_exe, "eslint"] + eslint_check_paths, "gate"),
        (
            "vitest",
            [npx_exe, "vitest", "run", "--reporter=verbose"] + vitest_paths,
            "test",
        ),
    ]

    # Build is always full-project — only run when no paths were given.
    if is_full_scan:
        steps.append(("build", [npm_exe, "run", "build"], "gate"))

    title = "Quality Gates (Frontend)"
    print(title)
    print("=" * len(title))

    total_elapsed = 0.0
    validation_passed = True
    failures: list[tuple[str, str]] = []  # (label, full_output)

    for label, cmd, kind in steps:
        start = time.monotonic()
        # Use errors="replace" and text=True to avoid UnicodeDecodeError on Windows.
        # Capture stdout and stderr properly.
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd="webui", errors="replace"
        )
        elapsed = time.monotonic() - start
        total_elapsed += elapsed
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        output = output.strip()

        if kind == "fix":
            # prettier --write / eslint --fix
            # Exit code 1 from eslint --fix means "unfixable issues remain"
            # — that is expected; the follow-up eslint gate step catches them.
            if result.returncode <= 1:
                status = f"FIXED ({elapsed:.1f}s)"
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
                failures.append((label, output))
        else:
            # eslint / build
            if result.returncode == 0:
                status = f"PASS ({elapsed:.1f}s)"
            else:
                status = f"FAIL ({elapsed:.1f}s)"
                validation_passed = False
                failures.append((label, output))

        print(f"{label:<14}.... {status}")

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
