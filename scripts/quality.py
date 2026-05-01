#!/usr/bin/env python
"""Quality gate runner --- formats, auto-fixes, then checks code quality.

Usage:
    python scripts/quality.py [paths...]

Paths can be files or directories. If no paths are given, the full project
is checked. File paths (e.g. ``core/utils/config.py``) are translated to
their corresponding test paths (``tests/core/utils/test_config.py``) for
pytest; ruff and mypy receive them directly.
"""

import re
import subprocess
import sys
import time


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
    steps: list[tuple[str, list[str], str]] = [
        ("ruff format", [sys.executable, "-m", "ruff", "format"] + ruff_fmt_paths, "fix"),
        ("ruff fix", [sys.executable, "-m", "ruff", "check", "--fix"] + ruff_fix_paths, "fix"),
        ("ruff check", [sys.executable, "-m", "ruff", "check"] + ruff_check_paths, "gate"),
        ("mypy", [sys.executable, "-m", "mypy", "--pretty"] + mypy_paths, "gate"),
        (
            "pytest",
            [sys.executable, "-m", "pytest", "-v", "--tb=short", "--timeout=30"] + test_paths,
            "pytest",
        ),
    ]

    print("Quality Gates")
    print("=============")

    total_elapsed = 0.0
    validation_passed = True
    failures: list[tuple[str, str]] = []  # (label, full_output)

    for label, cmd, kind in steps:
        start = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.monotonic() - start
        total_elapsed += elapsed
        output = (result.stdout + result.stderr).strip()

        if kind == "fix":
            # ruff format / ruff check --fix
            # Exit code 1 means "unfixable issues remain" — that's fine,
            # the follow-up `ruff check` step will catch them with full detail.
            if result.returncode <= 1:
                status = f"FIXED ({elapsed:.1f}s)"
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
                failures.append((label, output))
        else:
            # ruff check / mypy
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
        # Count validation-gate failures (ruff check, mypy, pytest) for the summary.
        failed_count = sum(1 for label, _ in failures if label not in ("ruff format", "ruff fix"))
        gate_word = "s" if failed_count != 1 else ""
        print(f"{failed_count} gate{gate_word} failed in {total_elapsed:.1f}s.")

    return 0 if validation_passed else 1


if __name__ == "__main__":
    sys.exit(main())
