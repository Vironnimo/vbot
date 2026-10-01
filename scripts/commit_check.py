#!/usr/bin/env python
"""Check a commit: format, lint and type-check the staged files, run the tests they affect.

Git runs this through ``.githooks/pre-commit`` and ``.githooks/pre-merge-commit``
once a clone has enabled the tracked hooks with ``git config core.hooksPath
.githooks``. It covers staged Python files (Ruff, mypy), staged frontend sources
(Prettier, ESLint) and the tests those changes affect.

Tests run when work lands on main: the primary checkout's commits and the merge
commits of ``scripts/worktree.py merge``. That command prepares each merge commit in
a private landing checkout, a linked worktree whose commit it marks with
``VBOT_COMMIT_CHECK_LANDING``, and moves main to the commit only once it passed.
Any other commit in a linked worktree gets the static checks only. Before a merge
takes the merge lock, ``worktree.py merge`` runs ``commit_check.py --branch`` in the
worktree: the pytest tests the branch's changes affect, so the merge commit only has
to run the tests neither side covered.

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

pytest runs the tests affected by the working tree through pytest-testmon, which
compares the code each test executed last time with the current code, plus the
tests that read a data file changed since the tree the checkout's records describe
(``tests/file_dependencies.py``, ``scripts/_test_impact.py``). A change to the
pytest or coverage configuration in ``pyproject.toml`` or to a file read during
collection runs the complete suite, as
do records that are missing, unreadable or without a tested state. A
merge commit runs only the tests that neither this checkout's nor the merged
worktree's test runs cover as merged.
A failing test blocks the commit when it depends on a staged file or only on
committed code; failures that depend on another session's unstaged work are
reported without blocking. A test failing on committed code runs once more alone
and is reported without blocking when it passes then.

Vitest runs the tests related to staged WebUI sources plus the guard tests, and
the WebUI build runs. A change to the WebUI packages or to another WebUI file that
is no source (build and test configuration, page shell, static assets) runs every
Vitest test and the build; a change to the packages or to the lint or format
configuration lints and format-checks every source. The WebUI checks refuse to run
on packages that differ from ``webui/package-lock.json`` and block the commit until
``npm ci`` installs the locked ones; the hook never installs packages itself. A
staged package manifest that the lock does not record blocks until ``npm install``
updates the lock.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import _test_impact, _webui_packages  # noqa: E402
from tests import cpu_pool, file_dependencies  # noqa: E402

PYTHON_SUFFIXES = {".py", ".pyi"}
ESLINT_SUFFIXES = {".js", ".mjs", ".cjs", ".svelte"}
IGNORED_ROOTS = ("archive/",)
WEBUI_ROOT = "webui/"
WEBUI_SOURCE_ROOTS = ("webui/src/", "webui/scripts/")
# Bundled Extension pages and their fixtures keep editable sources in <owner>/ui/.
EXTENSION_UI_PATTERN = re.compile(
    r"^(?:resources/extensions|tests/fixtures/extension-pages)/[^/]+/ui/"
)
# The manifest and lock pin the linter, formatter, test runner, build and libraries.
WEBUI_PACKAGE_FILES = frozenset({"webui/package.json", "webui/package-lock.json"})
# Lint and format configuration can change the verdict on every source.
WEBUI_STYLE_CONFIG = frozenset({"webui/eslint.config.js", "webui/prettier.config.js"})
# Differences a refusal lists before it summarizes the rest.
SHOWN_DIFFERENCES = 5
# The platforms CI type-checks (.github/workflows/ci.yml, static job). mypy keeps
# the host platform in its default cache and every other one in its own.
MYPY_PLATFORMS = ("win32", "linux")
MYPY_LINE_PATTERN = re.compile(r"^(?P<path>[^:\n]+?):\d+(?::\d+)?: (?P<kind>error|note):")
PYTEST_SUMMARY_PATTERN = re.compile(r"^(?:FAILED|ERROR) (?P<test>.+?)(?: - .*)?$")
TESTS_LOCK_NAME = "vbot-commit-tests.lock"
TESTS_ARGUMENTS_NAME = "vbot-commit-tests.args"
# Set by ``worktree.py merge`` for the merge commit it checks in its landing checkout.
LANDING_VARIABLE = "VBOT_COMMIT_CHECK_LANDING"
# Recorded test seconds per xdist worker; starting a worker costs about a second.
SECONDS_PER_WORKER = 4.0
TEST_MODULE_PATTERN = re.compile(r"^tests/(?:.+/)?(?:test_[^/]*|[^/]*_test)\.py$")


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
    ``git merge`` keeps an inherited GIT_REFLOG_ACTION instead of naming its own
    merged heads there, which a test's merge hook reads. The landing mark would make
    the commit checks those tests run in temporary worktrees run tests.
    """
    local = {
        *_git(root, "rev-parse", "--local-env-vars").split(),
        "GIT_REFLOG_ACTION",
        LANDING_VARIABLE,
    }
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


class WebUIScope(NamedTuple):
    """The WebUI checks a set of changed paths calls for."""

    sources: list[str]
    """Changed WebUI and Extension page sources: lint and format them, run related tests."""
    all_styles: bool
    """Lint and format every source: the linter, the formatter or their configuration changed."""
    all_tests: bool
    """Run every Vitest test and the build: packages or build or test configuration changed."""


def webui_scope(paths: Iterable[str]) -> WebUIScope:
    """Return the WebUI checks *paths* call for.

    Every WebUI file that is no source feeds the packages, the build or the tests:
    the package manifest and lock, the Vite configuration (which holds the Vitest
    configuration), the page shell and the static assets. The lint and format
    configuration only affects lint and format results.
    """
    paths = list(paths)
    return WebUIScope(
        sources=[path for path in paths if is_frontend_source(path)],
        all_styles=any(path in WEBUI_PACKAGE_FILES | WEBUI_STYLE_CONFIG for path in paths),
        all_tests=any(
            path.startswith(WEBUI_ROOT)
            and not is_frontend_source(path)
            and path not in WEBUI_STYLE_CONFIG
            for path in paths
        ),
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


@contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """Hold an OS file lock; concurrent commits in one checkout run tests one at a time."""
    notice = "Commit check: waiting for the tests of another commit in this checkout..."
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            waiting = False
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not waiting:
                        print(notice, flush=True)
                        waiting = True
                    time.sleep(0.5)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(notice, flush=True)
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
    dependencies = _test_impact.dependencies(root, set(failed))
    # The dependencies are spelled as the records spell paths.
    staged = set(map(file_dependencies.recorded_path, staged))
    dirty = set(map(file_dependencies.recorded_path, dirty))
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


def _commit(root: Path, revision: str) -> str:
    """Return the commit *revision* names in *root*, or an empty string."""
    return subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"{revision}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _merged_branch_checkout(root: Path) -> tuple[Path, str] | None:
    """Return the other checkout whose HEAD this merge commit merges, and that HEAD.

    ``git commit`` concluding a merge finds MERGE_HEAD. ``git merge`` runs the
    pre-merge-commit hook before it writes MERGE_HEAD and names the merged heads in
    GIT_REFLOG_ACTION (``merge <head>...``) instead.
    """
    merge_head = _commit(root, "MERGE_HEAD")
    if not merge_head:
        action = os.environ.get("GIT_REFLOG_ACTION", "").split()
        if len(action) != 2 or action[0] != "merge":
            return None
        merge_head = _commit(root, action[1])
        if not merge_head:
            return None
    for block in _git(root, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        if fields.get("HEAD") == merge_head and "worktree" in fields:
            checkout = Path(fields["worktree"])
            if checkout.resolve() != root.resolve():
                return checkout, merge_head
    return None


def _primary_checkout(root: Path) -> Path | None:
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    if common.name != ".git" or common.parent.resolve() == root.resolve():
        return None
    return common.parent


def _adopt_primary_data(root: Path, lock_path: Path) -> None:
    """Give a worktree without test-impact data a copy of the primary checkout's."""
    source = _primary_checkout(root)
    if source is None or (root / file_dependencies.TESTMON_DATA).is_file():
        return
    with _exclusive(lock_path):
        try:
            copied = _test_impact.copy_data(source, root)
        except (OSError, sqlite3.Error):
            return
    if copied:
        print(f"Commit check: using the test-impact data of {source}.", flush=True)


def _changed_since_tested(root: Path, records: Path, untested: str) -> set[str] | None:
    """Return the paths of *root*'s index that differ from the state *records* describe.

    Records without a tested state are taken to describe the commit *untested*.
    None when the tree they describe no longer exists.
    """
    state = _test_impact.tested_state(records)
    tree, dirty = state if state is not None else (untested, frozenset[str]())
    try:
        changed = _git_paths(root, "diff", "--cached", "--name-only", "--no-renames", tree)
    except subprocess.CalledProcessError:
        return None
    return {path for path in changed | dirty if not path.startswith(IGNORED_ROOTS)}


def _record_tested_state(root: Path, dirty: set[str]) -> None:
    """Record that *root*'s records now describe its index, except for the *dirty* paths."""
    try:
        tree = _git(root, "write-tree").strip()
    except subprocess.CalledProcessError:
        return
    with contextlib.suppress(OSError, sqlite3.Error):
        _test_impact.record_tested_state(root, tree, dirty)


def _reuse_runs(
    root: Path, other: Path, untested: str, selection: _test_impact.Selection
) -> tuple[_test_impact.Selection, set[str] | None]:
    """Narrow *selection* to the tests neither this checkout nor *other* ran as they are now.

    *selection* judges *root*'s index against its own test runs. The checkout
    *other* ran its tests on the state its records describe (the commit *untested*
    when they name none): for a merge commit the merged branch's worktree, for a
    branch check the primary checkout, whose runs cover what the branch took over
    from main. A test either side leaves out passed there with the code and files it
    has now. *root* adopts *other*'s record of each test whose current state *other*
    tested, so later checks here judge that test by it. Also return the paths that
    differ from *other*'s tested state, None when its records name none.
    """
    since_other = _changed_since_tested(root, other, untested)
    on_other = _test_impact.select(root, since_other, records=other)
    tested_on_other = {
        test
        for test in on_other.durations
        if selection.selects(test) and not on_other.selects(test)
    }
    with contextlib.suppress(OSError, sqlite3.Error):
        _test_impact.adopt(other, root, tested_on_other)
    print(f"Commit check: reusing the test runs of {other}.", flush=True)
    named = _test_impact.tested_state(other) is not None
    return selection & on_other, since_other if named else None


def _workers(seconds: float) -> list[str]:
    """Size xdist to the recorded test duration; short runs are fastest in one process."""
    workers = math.ceil(seconds / SECONDS_PER_WORKER)
    if workers <= 1:
        return ["-n", "0"]
    return ["-n", str(min(workers, cpu_pool.check_cores()))]


def _pytest() -> list[str]:
    # testmon only records here: this hook selects the tests. testmon's selection
    # plugin would select and order them inside each xdist worker from records the
    # controller rewrites meanwhile, and xdist needs every worker to collect alike.
    return [
        *[sys.executable, "-m", "pytest", "-q", "-rfE", "--no-header"],
        *["--testmon-noselect", "-p", "no:TestmonSelect"],
    ]


def _pytest_command(
    root: Path, selection: _test_impact.Selection, staged_tests: list[str], arguments_file: Path
) -> list[str] | None:
    pytest = _pytest()
    if selection.complete:
        print(
            f"Commit check: running the complete suite (about 5-10 minutes): {selection.reason}.",
            flush=True,
        )
        return [*pytest, "-n", str(cpu_pool.check_cores())]
    arguments = selection.pytest_arguments(root, staged_tests)
    if not arguments:
        return None
    arguments_file.write_text("\n".join(arguments) + "\n", encoding="utf-8")
    return [*pytest, *_workers(selection.seconds), f"@{arguments_file}"]


def _rerun_alone(root: Path, failed: list[str], env: dict[str, str]) -> tuple[list[str], list[str]]:
    """Run the *failed* tests once more in one process; return ``(failing, passed)``.

    A test that failed only among the parallel test runs of a busy machine passes
    alone. Collection errors, which name no single test, are not run again.
    """
    env = {**env, cpu_pool.KIND_VARIABLE: "rerun"}
    env.pop(cpu_pool.REASON_VARIABLE, None)
    tests = [test for test in failed if "::" in test and (root / test.split("::")[0]).is_file()]
    if not tests:
        return failed, []
    result = _run([*_pytest(), "-n", "0", *tests], root, env)
    failing = set(failed_tests(result.stdout))
    # A run that ends with 1 but names no failure died before its report.
    if result.returncode not in (0, 1) or (result.returncode == 1 and not failing):
        return failed, []
    passed = [test for test in tests if test not in failing]
    return [test for test in failed if test not in passed], passed


def check_tests(
    root: Path, changed: list[str], dirty: set[str], kind: str = "commit"
) -> list[StepResult]:
    """Run the pytest tests affected by *changed*, the staged and deleted paths.

    *kind* names the check in the test core pool's run log; a merge commit is a merge.
    """
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").strip())
    lock_path = git_dir / TESTS_LOCK_NAME
    merged = _merged_branch_checkout(root)
    if merged is None:
        _adopt_primary_data(root, lock_path)
    # A merge commit reuses the merged branch's test runs; a branch check reuses the
    # primary checkout's, which cover what a rebase took over from main.
    primary = _primary_checkout(root) if kind == "branch" else None
    reused = merged or (None if primary is None else (primary, "HEAD"))
    selection = _test_impact.select(root, _changed_since_tested(root, root, "HEAD"))
    # Staged test modules may hold tests no record knows yet. Other sessions' new
    # test modules are left to their own commits.
    staged_tests = [path for path in changed if TEST_MODULE_PATTERN.match(path)]
    python_changed = any(Path(path).suffix in PYTHON_SUFFIXES for path in changed)
    unaffected = [StepResult("pytest", "PASS (no test affected)", False)] if python_changed else []
    if merged is None and not (selection.complete or selection.tests or staged_tests):
        with _exclusive(lock_path):
            _record_tested_state(root, dirty)
        return unaffected

    env = {**_test_environment(root), cpu_pool.KIND_VARIABLE: kind}
    with _exclusive(lock_path):
        if merged is not None:
            env[cpu_pool.KIND_VARIABLE] = "merge"
        if reused is not None:
            selection, since_other = _reuse_runs(root, *reused, selection)
            # A test module main tested as it is now need not run whole again.
            if merged is None and since_other is not None:
                staged_tests = [path for path in staged_tests if path in since_other]
        if selection.complete:
            env[cpu_pool.REASON_VARIABLE] = selection.reason
        command = _pytest_command(root, selection, staged_tests, git_dir / TESTS_ARGUMENTS_NAME)
        if command is None:
            _record_tested_state(root, dirty)
            return unaffected
        for name in _test_impact.discard_corrupt(root):
            print(
                f"Commit check: discarded the corrupt {name}; this run records afresh.", flush=True
            )
        result = _run(command, root, env)
        output = result.stdout
        failed = failed_tests(output)
        # 5: no test selected. A run that ends with 1 but names no failure died
        # before its report, as when pytest-timeout ends the process.
        if result.returncode not in (0, 1, 5) or (result.returncode == 1 and not failed):
            return [
                StepResult("pytest", f"FAIL (exit code {result.returncode})", True, _output(result))
            ]
        if not failed:
            _record_tested_state(root, dirty)
            return [StepResult("pytest", "PASS", False)]
        commit, committed, in_progress = classify_failures(root, failed, set(changed), dirty)
        # A busy machine can fail any test: only a test that fails alone as well blocks.
        failing, flaky = _rerun_alone(root, [*commit, *committed], env)
        commit = [test for test in commit if test in failing]
        committed = [test for test in committed if test in failing]
        # A blocked run leaves the tested state as it was, so the next run judges
        # every change since then again, tests whose failure left no record included.
        if not commit and not committed:
            _record_tested_state(root, dirty)

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
    if flaky:
        results.append(
            StepResult(
                "pytest",
                "NOT BLOCKING: failed, then passed when run again alone",
                False,
                _summary_lines(output, flaky),
            )
        )
    if not commit and not committed:
        results.insert(0, StepResult("pytest", "PASS", False))
    return results


def check_frontend_tests(root: Path, changed: list[str]) -> list[StepResult]:
    """Run the Vitest tests *changed* affects, and the WebUI build.

    Changed WebUI and Extension page sources run their related tests plus the guard
    tests; a change to the packages or to the build or test configuration runs every
    test.
    """
    scope = webui_scope(changed)
    if not scope.sources and not scope.all_tests:
        return []
    webui = root / "webui"
    vitest = _node_bin(webui, "vitest", "vitest")
    npm = _npm()
    if vitest is None or npm is None:
        return [
            StepResult(
                "vitest",
                "SKIPPED (node or webui/node_modules missing; run `npm ci` in webui/)",
                False,
            )
        ]
    stale = _stale_packages(webui)
    if stale is not None:
        return [stale]
    if scope.all_tests:
        tests = [*vitest, "run"]
    else:
        # The guard tests scan every WebUI and Extension page source, so they relate
        # to every change without importing it.
        guard_tests = sorted(
            path.relative_to(webui).as_posix() for path in webui.glob("src/**/*.guard.test.js")
        )
        related = [Path("..", path).as_posix() for path in scope.sources if (root / path).is_file()]
        tests = [*vitest, "related", "--run", *related, *guard_tests]
    env = _test_environment(root)
    return [
        _gate("vitest", tests, webui, env),
        _gate("webui build", [npm, "run", "build"], webui, env),
    ]


def _print_report(title: str, results: list[StepResult]) -> None:
    print(title)
    for result in results:
        print(f"  {result.label:<12} {result.status}")
    for result in results:
        if result.details:
            print(f"\n--- {result.label}: {result.status} ---\n{result.details}")


def check_branch(root: Path) -> int:
    """Run the pytest tests a worktree branch affects, before ``worktree.py merge``.

    They are the tests the changes since the checkout's tested state affect; a
    worktree takes the primary checkout's records over when it is created, so these
    are the branch's changes. They run outside the merge lock, and the merge commit
    then runs only the tests neither side covered, plus the frontend tests and the
    WebUI build, which have no records to reuse.
    """
    start = time.monotonic()
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").strip())
    _adopt_primary_data(root, git_dir / TESTS_LOCK_NAME)
    changed = _changed_since_tested(root, root, "HEAD") or set()
    results = check_tests(root, sorted(changed), dirty_files(root), kind="branch")
    elapsed = time.monotonic() - start
    if results:
        _print_report("Branch check", results)
    if any(result.blocking for result in results):
        print(
            f"\nBranch check failed ({elapsed:.1f}s). Fix the reported problems in the "
            "worktree, commit, and merge again."
        )
        return 1
    print(f"\nBranch check passed ({elapsed:.1f}s).")
    return 0


def main(root: Path = PROJECT_ROOT, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a commit, or a worktree branch.")
    parser.add_argument(
        "--branch",
        action="store_true",
        help="run the tests the worktree branch affects, as worktree.py merge does first",
    )
    arguments = parser.parse_args(argv or [])
    # Tool output may hold characters a legacy Windows code page cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if arguments.branch:
        return check_branch(root)
    # A staged file deleted from the working tree has nothing left to check.
    staged = [path for path in staged_files(root) if (root / path).is_file()]
    changed = sorted({*staged, *staged_deletions(root)})
    if not changed:
        return 0
    dirty = dirty_files(root)
    start = time.monotonic()
    results = [*check_python(root, staged, dirty), *check_frontend(root, staged, dirty)]
    if _primary_checkout(root) is not None and not os.environ.get(LANDING_VARIABLE):
        if results:
            notice = "NOT RUN in a worktree; `python scripts/worktree.py merge` runs them"
            results.append(StepResult("tests", notice, False))
    elif any(result.blocking for result in results):
        results.append(StepResult("tests", "NOT RUN until the problems above are fixed", False))
    else:
        results += [*check_tests(root, changed, dirty), *check_frontend_tests(root, changed)]
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
    sys.exit(main(argv=sys.argv[1:]))
