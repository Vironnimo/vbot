#!/usr/bin/env python
"""Check staged files before a commit: format, lint and type-check them.

Git runs this through ``.githooks/pre-commit`` once a clone has enabled the
tracked hooks with ``git config core.hooksPath .githooks``. It covers staged
Python files (Ruff, mypy) and staged frontend sources (Prettier, ESLint); tests
stay with the committer and the complete suite runs in CI.

Formatter and linter fixes are applied and re-staged only for files whose whole
change is staged, so unstaged work in the same checkout (another session's
edits included) is never rewritten or swept into the commit. A partially staged
file is checked as it is in the working tree and blocks the commit when it
needs a fix.

mypy checks the whole configured project, because a staged change can break a
caller elsewhere. Its errors block the commit when they are in a staged file or
in a file without uncommitted changes; errors in files with unstaged or untracked
work in progress are reported without blocking.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PYTHON_SUFFIXES = {".py", ".pyi"}
ESLINT_SUFFIXES = {".js", ".mjs", ".cjs", ".svelte"}
IGNORED_ROOTS = ("archive/",)
WEBUI_SOURCE_ROOTS = ("webui/src/", "webui/scripts/")
# Bundled Extension pages and their fixtures keep editable sources in <owner>/ui/.
EXTENSION_UI_PATTERN = re.compile(
    r"^(?:resources/extensions|tests/fixtures/extension-pages)/[^/]+/ui/"
)
MYPY_LINE_PATTERN = re.compile(r"^(?P<path>[^:\n]+?):\d+(?::\d+)?: (?:error|note):")


class StepResult(NamedTuple):
    label: str
    status: str
    blocking: bool
    details: str = ""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def _git_paths(root: Path, *args: str) -> set[str]:
    return {path for path in _git(root, *args, "-z").split("\0") if path}


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout + result.stderr).strip()


def _file_bytes(root: Path, paths: Iterable[str]) -> dict[str, bytes]:
    return {path: (root / path).read_bytes() for path in paths}


def staged_files(root: Path) -> list[str]:
    """Return staged added, copied, modified and renamed paths, sorted."""
    staged = _git_paths(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return sorted(path for path in staged if not path.startswith(IGNORED_ROOTS))


def dirty_files(root: Path) -> set[str]:
    """Return tracked paths with unstaged changes plus untracked paths."""
    unstaged = _git_paths(root, "diff", "--name-only")
    untracked = _git_paths(root, "ls-files", "--others", "--exclude-standard")
    return unstaged | untracked


def is_frontend_source(path: str) -> bool:
    return path.startswith(WEBUI_SOURCE_ROOTS) or EXTENSION_UI_PATTERN.match(path) is not None


def split_mypy_output(
    output: str, staged: set[str], dirty: set[str]
) -> tuple[list[str], list[str]]:
    """Return mypy lines as ``(blocking, work_in_progress)``.

    A line blocks when its file is staged or has no uncommitted changes; lines
    for files with unstaged or untracked work belong to work in progress. Lines
    without a file location (summaries) are dropped.
    """
    blocking: list[str] = []
    in_progress: list[str] = []
    for line in output.splitlines():
        match = MYPY_LINE_PATTERN.match(line)
        if match is None:
            continue
        path = PurePosixPath(match.group("path").replace("\\", "/")).as_posix()
        if path in staged or path not in dirty:
            blocking.append(line)
        else:
            in_progress.append(line)
    return blocking, in_progress


def _fix_and_restage(
    root: Path,
    label: str,
    fixable: list[str],
    commands: list[tuple[list[str], Path]],
) -> StepResult | None:
    """Run fixers over fully staged files and stage what they changed."""
    if not fixable:
        return None
    before = _file_bytes(root, fixable)
    for command, cwd in commands:
        result = _run(command, cwd)
        if result.returncode > 1:
            return StepResult(f"{label} fix", "FAIL", True, _output(result))
    changed = [
        path for path, content in _file_bytes(root, fixable).items() if content != before[path]
    ]
    if not changed:
        return None
    _git(root, "add", "--", *changed)
    return StepResult(f"{label} fix", "FIXED and re-staged", False, "\n".join(changed))


def _gate(label: str, command: list[str], cwd: Path) -> StepResult:
    result = _run(command, cwd)
    if result.returncode == 0:
        return StepResult(label, "PASS", False)
    return StepResult(label, "FAIL", True, _output(result))


def _mypy_targets(root: Path, python_files: list[str]) -> list[str]:
    """Return the configured mypy files plus staged Python files outside them."""
    pyproject = root / "pyproject.toml"
    configured: list[str] = []
    if pyproject.is_file():
        with pyproject.open("rb") as handle:
            configured = list(tomllib.load(handle).get("tool", {}).get("mypy", {}).get("files", []))
    roots = tuple(entry.rstrip("/") + "/" for entry in configured)
    extra = [path for path in python_files if not path.startswith(roots)]
    return [*configured, *extra]


def check_python(root: Path, staged: list[str], dirty: set[str]) -> list[StepResult]:
    python_files = [path for path in staged if Path(path).suffix in PYTHON_SUFFIXES]
    if not python_files and "pyproject.toml" not in staged:
        return []
    results: list[StepResult] = []
    if python_files:
        ruff = [sys.executable, "-m", "ruff"]
        fixable = [path for path in python_files if path not in dirty]
        fixed = _fix_and_restage(
            root,
            "ruff",
            fixable,
            [
                ([*ruff, "check", "--fix", "--exit-zero", "--", *fixable], root),
                ([*ruff, "format", "--", *fixable], root),
            ],
        )
        if fixed is not None:
            results.append(fixed)
        results.append(
            _gate("ruff format", [*ruff, "format", "--check", "--", *python_files], root)
        )
        results.append(_gate("ruff check", [*ruff, "check", "--", *python_files], root))

    targets = _mypy_targets(root, python_files)
    if not targets:
        return results
    mypy = _run([sys.executable, "-m", "mypy", *targets], root)
    if mypy.returncode == 0:
        results.append(StepResult("mypy", "PASS", False))
    elif mypy.returncode != 1:
        results.append(StepResult("mypy", "FAIL", True, _output(mypy)))
    else:
        blocking, in_progress = split_mypy_output(mypy.stdout, set(staged), dirty)
        if blocking:
            results.append(StepResult("mypy", "FAIL", True, "\n".join(blocking)))
        else:
            results.append(StepResult("mypy", "PASS", False))
        if in_progress:
            results.append(
                StepResult(
                    "mypy",
                    "NOT BLOCKING: errors in files with uncommitted work in progress",
                    False,
                    "\n".join(in_progress),
                )
            )
    return results


def _node_bin(webui: Path, package: str, command: str) -> list[str] | None:
    """Return a ``node <package bin>`` command from webui/node_modules, or None."""
    node = shutil.which("node")
    manifest = webui / "node_modules" / package / "package.json"
    if node is None or not manifest.is_file():
        return None
    bin_field = json.loads(manifest.read_text(encoding="utf-8")).get("bin")
    relative = bin_field.get(command) if isinstance(bin_field, dict) else bin_field
    if not isinstance(relative, str):
        return None
    return [node, str((manifest.parent / relative).resolve())]


def check_frontend(root: Path, staged: list[str], dirty: set[str]) -> list[StepResult]:
    files = [path for path in staged if is_frontend_source(path)]
    if not files:
        return []
    webui = root / "webui"
    prettier = _node_bin(webui, "prettier", "prettier")
    eslint = _node_bin(webui, "eslint", "eslint")
    if prettier is None or eslint is None:
        return [
            StepResult(
                "frontend",
                "SKIPPED (node or webui/node_modules missing; run `npm ci` in webui/)",
                False,
            )
        ]
    # Prettier resolves configuration per file; Extension pages live outside
    # webui/, so the WebUI policy and Svelte plugin are passed explicitly.
    prettier_options = [
        "--config",
        "prettier.config.js",
        "--plugin",
        "prettier-plugin-svelte",
        "--ignore-unknown",
    ]
    # ESLint runs from the repository root, where the WebUI config covers both
    # webui/ sources and Extension page sources.
    eslint_options = ["--config", "webui/eslint.config.js", "--no-warn-ignored"]

    def from_webui(paths: list[str]) -> list[str]:
        return [Path("..", path).as_posix() for path in paths]

    lintable = [path for path in files if Path(path).suffix in ESLINT_SUFFIXES]
    fixable = [path for path in files if path not in dirty]
    fixable_lintable = [path for path in fixable if path in lintable]
    fix_commands: list[tuple[list[str], Path]] = []
    if fixable_lintable:
        fix_commands.append(([*eslint, *eslint_options, "--fix", *fixable_lintable], root))
    if fixable:
        fix_commands.append(
            ([*prettier, *prettier_options, "--write", *from_webui(fixable)], webui)
        )

    results: list[StepResult] = []
    fixed = _fix_and_restage(root, "frontend", fixable, fix_commands)
    if fixed is not None:
        results.append(fixed)
    results.append(
        _gate("prettier", [*prettier, *prettier_options, "--check", *from_webui(files)], webui)
    )
    if lintable:
        results.append(_gate("eslint", [*eslint, *eslint_options, *lintable], root))
    return results


def main(root: Path = PROJECT_ROOT) -> int:
    # Tool output may hold characters a legacy Windows code page cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    # A staged file deleted from the working tree has nothing left to check.
    staged = [path for path in staged_files(root) if (root / path).is_file()]
    if not staged:
        return 0
    dirty = dirty_files(root)
    start = time.monotonic()
    results = [*check_python(root, staged, dirty), *check_frontend(root, staged, dirty)]
    if not results:
        return 0

    partial = [path for path in staged if path in dirty]
    print("Commit check")
    for result in results:
        print(f"  {result.label:<12} {result.status}")
    for result in results:
        if result.details:
            print(f"\n--- {result.label}: {result.status} ---\n{result.details}")
    if partial:
        print(
            "\nnote: these staged files also have unstaged changes; they were checked as they "
            "are in the working tree and not auto-fixed:\n  " + "\n  ".join(partial)
        )

    elapsed = time.monotonic() - start
    if any(result.blocking for result in results):
        print(
            f"\nCommit blocked ({elapsed:.1f}s). Fix the reported problems, stage the "
            "files and commit again. Do not bypass this check with --no-verify."
        )
        return 1
    print(f"\nCommit check passed ({elapsed:.1f}s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
