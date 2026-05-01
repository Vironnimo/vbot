#!/usr/bin/env python
"""Quality gate runner --- runs ruff, mypy, and pytest in sequence.

Usage:
    python scripts/quality.py [paths...]

If paths are given, each gate checks only those paths.
If no paths are given, the full project is checked.
"""

import re
import subprocess
import sys


def run_gate(cmd: list[str]) -> tuple[bool, str]:
    """Run a quality gate command. Returns (passed, combined_output)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, (result.stdout + result.stderr).rstrip()


def format_error_summary(output: str) -> str:
    """Extract last few meaningful lines for a brief error display."""
    lines = [line for line in output.splitlines() if line.strip()]
    return "\n".join(lines[-5:])


def main() -> int:
    raw_paths: list[str] = sys.argv[1:]

    # Normalize paths: strip trailing slashes, use forward slashes
    paths: list[str] = [p.replace("\\", "/").rstrip("/") for p in raw_paths]

    # ---------- ruff check ----------
    ruff_paths = paths if paths else ["."]

    # ---------- ruff format ----------
    fmt_paths = paths if paths else ["."]

    # ---------- mypy ----------
    if paths:
        mypy_paths: list[str] = paths
    else:
        mypy_paths = ["core/", "server/", "cli/", "desktop/", "tests/"]

    # ---------- pytest ----------
    if paths:
        test_paths: list[str] = []
        for p in paths:
            if p.startswith("core/"):
                test_paths.append("tests/" + p)
            else:
                test_paths.append(p)
    else:
        test_paths = ["tests/"]

    # fmt: off
    gates: list[tuple[str, list[str]]] = [
        ("ruff check",  [sys.executable, "-m", "ruff", "check"] + ruff_paths),
        ("ruff format", [sys.executable, "-m", "ruff", "format", "--check"] + fmt_paths),
        ("mypy",        [sys.executable, "-m", "mypy", "--pretty"] + mypy_paths),
    ]
    # fmt: on

    print("Quality Gates")
    print("=============")

    all_passed = True

    for name, cmd in gates:
        passed, output = run_gate(cmd)
        status = "PASS" if passed else "FAIL"
        print(f"{name:<14}.... {status}")
        if not passed:
            all_passed = False
            err_summary = format_error_summary(output)
            if err_summary:
                for line in err_summary.splitlines():
                    print(f"  {line}")

    # ---------- pytest (run separately to extract test count) ----------
    pytest_cmd: list[str] = [
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "--tb=short",
    ] + test_paths

    passed, output = run_gate(pytest_cmd)

    if passed:
        match = re.search(r"(\d+) passed", output)
        count = match.group(1) if match else "?"
        status = f"PASS ({count}/{count})"
    else:
        status = "FAIL"
        all_passed = False

    print(f"{'pytest':<14}.... {status}")
    if not passed:
        err_summary = format_error_summary(output)
        if err_summary:
            for line in err_summary.splitlines():
                print(f"  {line}")

    print()
    if all_passed:
        print("All gates passed.")
    else:
        print("Some gates failed.")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
