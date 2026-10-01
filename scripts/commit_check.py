#!/usr/bin/env python
"""Check a commit: format, lint and type-check the staged files.

Git runs this through ``.githooks/pre-commit`` and ``.githooks/pre-merge-commit``
once a clone has enabled the tracked hooks with ``git config core.hooksPath
.githooks``. It covers staged Python files (Ruff, mypy) and staged frontend sources
(Prettier, ESLint) in every checkout, the merge commits of ``scripts/worktree.py
merge`` included. It runs no tests: Agents run the tests covering their change, and
``scripts/push.py`` runs every check before main reaches the remote.

Formatter and linter fixes are applied and re-staged only for files whose whole
change is staged, so unstaged work in the same checkout (another session's
edits included) is never rewritten or swept into the commit. A partially staged
file is checked as it is in the working tree and blocks the commit when it
needs a fix.

mypy checks the whole configured project, because a staged change can break a
caller elsewhere, once for Windows and once for Linux (the platforms CI
type-checks), so a Windows-only name without a platform check fails on any host.
Its errors block the commit when they are in a staged file or
in a file without uncommitted changes; errors in files with unstaged or untracked
work in progress are reported without blocking.

The WebUI checks refuse to run on packages that differ from
``webui/package-lock.json`` and block the commit until ``npm ci`` installs the
locked ones; the hook never installs packages itself. A staged package manifest
that the lock does not record blocks until ``npm install`` updates the lock. A
change to the packages or to the lint or format configuration lints and
format-checks every source.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import _webui_packages  # noqa: E402

PYTHON_SUFFIXES = {".py", ".pyi"}
ESLINT_SUFFIXES = {".js", ".mjs", ".cjs", ".svelte"}
IGNORED_ROOTS = ("archive/",)
WEBUI_SOURCE_ROOTS = ("webui/src/", "webui/scripts/")
# Bundled Extension pages and their fixtures keep editable sources in <owner>/ui/.
EXTENSION_UI_PATTERN = re.compile(
    r"^(?:resources/extensions|tests/fixtures/extension-pages)/[^/]+/ui/"
)
# The manifest and lock pin the linter, the formatter and the libraries.
WEBUI_PACKAGE_FILES = frozenset({"webui/package.json", "webui/package-lock.json"})
# Lint and format configuration can change the verdict on every source.
WEBUI_STYLE_CONFIG = frozenset({"webui/eslint.config.js", "webui/prettier.config.js"})
# Differences a refusal lists before it summarizes the rest.
SHOWN_DIFFERENCES = 5
# The platforms CI type-checks (.github/workflows/ci.yml, static job). mypy keeps
# the host platform in its default cache and every other one in its own.
MYPY_PLATFORMS = ("win32", "linux")
MYPY_LINE_PATTERN = re.compile(r"^(?P<path>[^:\n]+?):\d+(?::\d+)?: (?P<kind>error|note):")


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


class WebUIScope(NamedTuple):
    """The WebUI checks a set of changed paths calls for."""

    sources: list[str]
    """Changed WebUI and Extension page sources: lint and format them."""
    all_styles: bool
    """Lint and format every source: the linter, the formatter or their configuration changed."""


def webui_scope(paths: Iterable[str]) -> WebUIScope:
    """Return the WebUI checks *paths* call for.

    The package manifest and lock pin the linter and the formatter, so a change to
    them, like one to the lint or format configuration, can change the verdict on
    every source.
    """
    paths = list(paths)
    return WebUIScope(
        sources=[path for path in paths if is_frontend_source(path)],
        all_styles=any(path in WEBUI_PACKAGE_FILES | WEBUI_STYLE_CONFIG for path in paths),
    )


def _listed(differences: list[str]) -> str:
    shown = differences[:SHOWN_DIFFERENCES]
    if len(differences) > len(shown):
        shown.append(f"... and {len(differences) - len(shown)} more")
    return "\n".join(shown)


def _stale_packages(webui: Path) -> StepResult | None:
    """Return a blocking result when node_modules does not hold the locked packages."""
    differences = _webui_packages.installed_differences(
        webui / "node_modules", webui / "package-lock.json"
    )
    if not differences:
        return None
    return StepResult(
        "webui deps",
        "FAIL: webui/node_modules does not match package-lock.json; run `npm ci` in webui/",
        True,
        _listed(differences),
    )


def _unrecorded_manifest(webui: Path, staged: list[str]) -> StepResult | None:
    """Return a blocking result when a staged package manifest and its lock disagree."""
    if not WEBUI_PACKAGE_FILES & set(staged):
        return None
    differences = _webui_packages.manifest_differences(
        webui / "package.json", webui / "package-lock.json"
    )
    if not differences:
        return None
    return StepResult(
        "webui deps",
        "FAIL: webui/package-lock.json does not record package.json; run `npm install` in webui/",
        True,
        _listed(differences),
    )


def _npm() -> str | None:
    return shutil.which("npm.cmd" if os.name == "nt" else "npm")


def split_mypy_output(
    output: str, staged: set[str], dirty: set[str]
) -> tuple[list[str], list[str]]:
    """Return mypy lines as ``(blocking, work_in_progress)``.

    An error blocks when its file is staged or has no uncommitted changes;
    errors in files with unstaged or untracked work belong to work in progress.
    A note goes with the error it follows; a note without one (such as
    ``annotation-unchecked``) reports nothing to fix and is dropped, like lines
    without a file location (summaries).
    """
    blocking: list[str] = []
    in_progress: list[str] = []
    group: list[str] | None = None
    for line in output.splitlines():
        match = MYPY_LINE_PATTERN.match(line)
        if match is None:
            continue
        if match.group("kind") == "note":
            if group is not None:
                group.append(line)
            continue
        path = PurePosixPath(match.group("path").replace("\\", "/")).as_posix()
        group = blocking if path in staged or path not in dirty else in_progress
        group.append(line)
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
    if targets:
        results.extend(check_types(root, targets, set(staged), dirty))
    return results


def check_types(
    root: Path, targets: list[str], staged: set[str], dirty: set[str]
) -> list[StepResult]:
    """Run mypy over *targets* for every platform in ``MYPY_PLATFORMS``, concurrently."""

    def run(platform: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "mypy"]
        if platform != sys.platform:
            command += ["--platform", platform, "--cache-dir", f".mypy_cache/{platform}"]
        return _run([*command, *targets], root)

    with ThreadPoolExecutor() as pool:
        runs = list(pool.map(run, MYPY_PLATFORMS))
    results: list[StepResult] = []
    for platform, mypy in zip(MYPY_PLATFORMS, runs, strict=True):
        label = f"mypy {platform}"
        if mypy.returncode == 0:
            results.append(StepResult(label, "PASS", False))
            continue
        if mypy.returncode != 1:
            results.append(StepResult(label, "FAIL", True, _output(mypy)))
            continue
        blocking, in_progress = split_mypy_output(mypy.stdout, staged, dirty)
        results.append(
            StepResult(label, "FAIL", True, "\n".join(blocking))
            if blocking
            else StepResult(label, "PASS", False)
        )
        if in_progress:
            results.append(
                StepResult(
                    label,
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
    """Format and lint the staged WebUI sources; all of them when the tools or rules changed."""
    scope = webui_scope(staged)
    files = scope.sources
    if not files and not scope.all_styles:
        return []
    webui = root / "webui"
    # npm ci, as CI runs it, refuses a lock that does not record the manifest.
    unrecorded = _unrecorded_manifest(webui, staged)
    if unrecorded is not None:
        return [unrecorded]
    prettier = _node_bin(webui, "prettier", "prettier")
    eslint = _node_bin(webui, "eslint", "eslint")
    npm = _npm()
    if prettier is None or eslint is None or (scope.all_styles and npm is None):
        return [
            StepResult(
                "frontend",
                "SKIPPED (node or webui/node_modules missing; run `npm ci` in webui/)",
                False,
            )
        ]
    stale = _stale_packages(webui)
    if stale is not None:
        return [stale]
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
    if scope.all_styles and npm is not None:
        # The package scripts check every source, as CI does.
        results.append(_gate("prettier", [npm, "run", "format:check"], webui))
        results.append(_gate("eslint", [npm, "run", "lint"], webui))
        return results
    results.append(
        _gate("prettier", [*prettier, *prettier_options, "--check", *from_webui(files)], webui)
    )
    if lintable:
        results.append(_gate("eslint", [*eslint, *eslint_options, *lintable], root))
    return results


def _print_report(title: str, results: list[StepResult]) -> None:
    print(title)
    for result in results:
        print(f"  {result.label:<12} {result.status}")
    for result in results:
        if result.details:
            print(f"\n--- {result.label}: {result.status} ---\n{result.details}")


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
    _print_report("Commit check", results)
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
