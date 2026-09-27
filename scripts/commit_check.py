#!/usr/bin/env python
"""Check a commit: format, lint and type-check the staged files, run the tests they affect.

Git runs this through ``.githooks/pre-commit`` and ``.githooks/pre-merge-commit``
once a clone has enabled the tracked hooks with ``git config core.hooksPath
.githooks``. It covers staged Python files (Ruff, mypy), staged frontend sources
(Prettier, ESLint) and the tests those changes affect.

Formatter and linter fixes are applied and re-staged only for files whose whole
change is staged, so unstaged work in the same checkout (another session's
edits included) is never rewritten or swept into the commit. A partially staged
file is checked as it is in the working tree and blocks the commit when it
needs a fix.

mypy checks the whole configured project, because a staged change can break a
caller elsewhere. Its errors block the commit when they are in a staged file or
in a file without uncommitted changes; errors in files with unstaged or untracked
work in progress are reported without blocking.

pytest runs the tests affected by the working tree through pytest-testmon, which
compares the code each test executed last time with the current code, plus the
tests that read a staged data file (``tests/file_dependencies.py``). A change to
``pyproject.toml`` or to a file read during collection runs the complete suite.
A failing test blocks the commit when it depends on a staged file or only on
committed code; failures that depend on another session's unstaged work are
reported without blocking. Vitest runs the tests related to staged WebUI
sources plus the guard tests, and the WebUI build runs.
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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tests import file_dependencies  # noqa: E402

PYTHON_SUFFIXES = {".py", ".pyi"}
ESLINT_SUFFIXES = {".js", ".mjs", ".cjs", ".svelte"}
IGNORED_ROOTS = ("archive/",)
WEBUI_SOURCE_ROOTS = ("webui/src/", "webui/scripts/")
# Bundled Extension pages and their fixtures keep editable sources in <owner>/ui/.
EXTENSION_UI_PATTERN = re.compile(
    r"^(?:resources/extensions|tests/fixtures/extension-pages)/[^/]+/ui/"
)
MYPY_LINE_PATTERN = re.compile(r"^(?P<path>[^:\n]+?):\d+(?::\d+)?: (?:error|note):")
PYTEST_SUMMARY_PATTERN = re.compile(r"^(?:FAILED|ERROR) (?P<test>.+?)(?: - .*)?$")
# Changes that can affect any test: pytest and plugin configuration.
FULL_SUITE_TRIGGERS = frozenset({"pyproject.toml"})
TESTS_LOCK_NAME = "vbot-commit-tests.lock"


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


def _run(
    command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _test_environment(root: Path) -> dict[str, str]:
    """Return the environment without the variables that bind git to this repository.

    A git hook exports variables such as GIT_DIR and GIT_INDEX_FILE. Tests that run
    git in temporary repositories would inherit them and change this repository.
    """
    local = set(_git(root, "rev-parse", "--local-env-vars").split())
    return {name: value for name, value in os.environ.items() if name not in local}


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout + result.stderr).strip()


def _file_bytes(root: Path, paths: Iterable[str]) -> dict[str, bytes]:
    return {path: (root / path).read_bytes() for path in paths}


def staged_files(root: Path) -> list[str]:
    """Return staged added, copied, modified and renamed paths, sorted."""
    staged = _git_paths(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return sorted(path for path in staged if not path.startswith(IGNORED_ROOTS))


def staged_deletions(root: Path) -> list[str]:
    """Return staged deleted paths, sorted; their tests still need to run."""
    deleted = _git_paths(root, "diff", "--cached", "--name-only", "--diff-filter=D")
    return sorted(path for path in deleted if not path.startswith(IGNORED_ROOTS))


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


def _gate(
    label: str, command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> StepResult:
    result = _run(command, cwd, env)
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


@contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """Hold an OS file lock; concurrent commits in one checkout run tests one at a time."""
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def failed_tests(output: str) -> list[str]:
    """Return the test ids and collection errors of a pytest ``-rfE`` summary."""
    failed: list[str] = []
    for line in output.splitlines():
        match = PYTEST_SUMMARY_PATTERN.match(line)
        if match is not None and match.group("test") not in failed:
            failed.append(match.group("test"))
    return failed


def classify_failures(
    root: Path, failed: list[str], staged: set[str], dirty: set[str]
) -> tuple[list[str], list[str], list[str]]:
    """Split failed tests into ``(caused_by_commit, on_committed_code, work_in_progress)``.

    A failure belongs to the commit when the test depends on a staged file, to
    work in progress when it depends on a file with unstaged or untracked changes
    and on no staged file, and otherwise to committed code.
    """
    dependencies = file_dependencies.dependencies(root, set(failed))
    commit: list[str] = []
    committed: list[str] = []
    in_progress: list[str] = []
    for test in failed:
        files = dependencies[test]
        if files & staged:
            commit.append(test)
        elif files & dirty:
            in_progress.append(test)
        else:
            committed.append(test)
    return commit, committed, in_progress


def _summary_lines(output: str, tests: list[str]) -> str:
    wanted = set(tests)
    lines = []
    for line in output.splitlines():
        match = PYTEST_SUMMARY_PATTERN.match(line)
        if match is not None and match.group("test") in wanted:
            lines.append(line)
    return "\n".join(lines)


def check_tests(root: Path, changed: list[str], dirty: set[str]) -> list[StepResult]:
    """Run the pytest tests affected by *changed*, the staged and deleted paths."""
    python_changed = any(Path(path).suffix in PYTHON_SUFFIXES for path in changed)
    data_files = {path for path in changed if Path(path).suffix not in PYTHON_SUFFIXES}
    readers = file_dependencies.readers(root, data_files)
    complete = bool(FULL_SUITE_TRIGGERS & data_files) or file_dependencies.COLLECTION in readers
    readers.discard(file_dependencies.COLLECTION)
    if not (python_changed or complete or readers):
        return []

    pytest = [sys.executable, "-m", "pytest", "-q", "-rfE", "--no-header"]
    runs: list[tuple[str, list[str]]] = []
    if complete:
        runs.append(("complete suite", [*pytest, "--testmon-noselect"]))
    else:
        if python_changed:
            runs.append(("affected by code", [*pytest, "--testmon"]))
        # testmon selects by Python code only; run the modules of data-file readers
        # without its selection. Deleted tests leave stale records behind.
        modules = sorted({test.partition("::")[0] for test in readers})
        modules = [module for module in modules if (root / module).is_file()]
        if modules:
            runs.append(("reading staged data files", [*pytest, "--testmon-noselect", *modules]))
    if not (root / file_dependencies.TESTMON_DATA).is_file():
        print(
            "Commit check: no test-impact data in this checkout yet; this commit runs the "
            "complete suite once to record it (about 10 minutes).",
            flush=True,
        )

    lock_path = Path(_git(root, "rev-parse", "--absolute-git-dir").strip()) / TESTS_LOCK_NAME
    env = _test_environment(root)
    outputs: list[str] = []
    with _exclusive(lock_path):
        for label, command in runs:
            result = _run(command, root, env)
            if result.returncode not in (0, 1, 5):  # 5: no test selected
                return [StepResult("pytest", f"FAIL ({label})", True, _output(result))]
            outputs.append(result.stdout)
    output = "\n".join(outputs)
    failed = failed_tests(output)
    if not failed:
        return [StepResult("pytest", "PASS", False)]

    commit, committed, in_progress = classify_failures(root, failed, set(changed), dirty)
    results: list[StepResult] = []
    if commit:
        results.append(
            StepResult(
                "pytest",
                "FAIL: tests affected by this commit",
                True,
                _summary_lines(output, commit),
            )
        )
    if committed:
        results.append(
            StepResult(
                "pytest",
                "FAIL: tests failing on committed code; fix them in a separate commit first",
                True,
                _summary_lines(output, committed),
            )
        )
    if in_progress:
        results.append(
            StepResult(
                "pytest",
                "NOT BLOCKING: failures depending on uncommitted work in progress",
                False,
                _summary_lines(output, in_progress),
            )
        )
    if not commit and not committed:
        results.insert(0, StepResult("pytest", "PASS", False))
    return results


def check_frontend_tests(root: Path, changed: list[str]) -> list[StepResult]:
    """Run the Vitest tests related to *changed* frontend sources, and the WebUI build."""
    files = [path for path in changed if is_frontend_source(path)]
    if not files:
        return []
    webui = root / "webui"
    vitest = _node_bin(webui, "vitest", "vitest")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if vitest is None or npm is None:
        return [
            StepResult(
                "vitest",
                "SKIPPED (node or webui/node_modules missing; run `npm ci` in webui/)",
                False,
            )
        ]
    # The guard tests scan every WebUI and Extension page source, so they relate
    # to every change without importing it.
    guard_tests = sorted(
        path.relative_to(webui).as_posix() for path in webui.glob("src/**/*.guard.test.js")
    )
    related = [Path("..", path).as_posix() for path in files if (root / path).is_file()]
    env = _test_environment(root)
    return [
        _gate("vitest", [*vitest, "related", "--run", *related, *guard_tests], webui, env),
        _gate("webui build", [npm, "run", "build"], webui, env),
    ]


def main(root: Path = PROJECT_ROOT) -> int:
    # Tool output may hold characters a legacy Windows code page cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    # A staged file deleted from the working tree has nothing left to check.
    staged = [path for path in staged_files(root) if (root / path).is_file()]
    changed = sorted({*staged, *staged_deletions(root)})
    if not changed:
        return 0
    dirty = dirty_files(root)
    start = time.monotonic()
    results = [*check_python(root, staged, dirty), *check_frontend(root, staged, dirty)]
    if any(result.blocking for result in results):
        results.append(StepResult("tests", "NOT RUN until the problems above are fixed", False))
    else:
        results += [*check_tests(root, changed, dirty), *check_frontend_tests(root, changed)]
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
