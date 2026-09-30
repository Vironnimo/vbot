from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from scripts import _test_impact, commit_check
from tests import cpu_pool

REPO_ROOT = Path(__file__).resolve().parents[2]

# Like the hook, most tests start git, pytest with testmon, or ruff as subprocesses:
# seconds each on a loaded machine, beyond the default 30 s per test. The budget
# also covers the module fixture that seeds the test-impact data, since its setup
# counts against the first test that uses it.
pytestmark = pytest.mark.timeout(120)

UNFORMATTED = "value  =  {'a':1}\n"
FORMATTED = 'value = {"a": 1}\n'


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "core.autocrlf", "false")
    return tmp_path


def _staged_content(root: Path, path: str) -> str:
    return _git(root, "show", f":{path}")


def test_fixes_are_restaged_only_for_fully_staged_files(
    impact_project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project with test-impact data: the test step finds no affected test.
    # Another session's unstaged edit of a committed file must stay as it is.
    # mypy, not under test here, starts without a cache: tens of seconds under load.
    monkeypatch.setattr(commit_check, "check_types", lambda *_arguments: [])
    _write(impact_project, "other.py", FORMATTED)
    _git(impact_project, "add", "other.py")
    _git(impact_project, "commit", "-q", "-m", "other", "--no-verify")
    _write(impact_project, "other.py", UNFORMATTED)
    _write(impact_project, "staged.py", UNFORMATTED)
    _git(impact_project, "add", "staged.py")

    assert commit_check.main(impact_project) == 0

    assert (impact_project / "staged.py").read_text() == FORMATTED
    assert _staged_content(impact_project, "staged.py") == FORMATTED
    assert (impact_project / "other.py").read_text() == UNFORMATTED
    assert _git(impact_project, "diff", "--cached", "--name-only").split() == ["staged.py"]
    assert "FIXED and re-staged" in capsys.readouterr().out

    # Checking the fixed commit again finds nothing to fix.
    assert commit_check.main(impact_project) == 0
    assert "FIXED" not in capsys.readouterr().out


def test_partially_staged_file_is_left_alone_and_blocks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commit_check, "check_types", lambda *_arguments: [])
    (repo / "module.py").write_text(UNFORMATTED)
    _git(repo, "add", "module.py")
    work_in_progress = UNFORMATTED + "other  =  2\n"
    (repo / "module.py").write_text(work_in_progress)

    assert commit_check.main(repo) == 1

    assert (repo / "module.py").read_text() == work_in_progress
    assert _staged_content(repo, "module.py") == UNFORMATTED


def test_mypy_errors_block_unless_in_unstaged_work_in_progress() -> None:
    output = "\n".join(
        [
            # A note without an error, as mypy prints for committed files, never blocks.
            "tests/committed.py:7: note: By default the bodies of untyped functions are not"
            " checked, consider using --check-untyped-defs  [annotation-unchecked]",
            "core\\staged.py:3: error: Incompatible return value  [return-value]",
            "core/caller.py:9: error: Missing positional argument  [call-arg]",
            "core/wip.py:1:5: error: Name 'x' is not defined  [name-defined]",
            "core/wip.py:1: note: See https://mypy.readthedocs.io",
            "Found 3 errors in 3 files (checked 4 source files)",
        ]
    )

    blocking, in_progress = commit_check.split_mypy_output(
        output, staged={"core/staged.py"}, dirty={"core/wip.py"}
    )

    assert [line.split(":")[0] for line in blocking] == ["core\\staged.py", "core/caller.py"]
    assert [line.split(":")[0] for line in in_progress] == ["core/wip.py", "core/wip.py"]


@pytest.mark.parametrize(
    ("path", "source", "all_styles", "all_tests"),
    [
        ("webui/src/lib/i18n.js", True, False, False),
        ("webui/scripts/build-extension-pages.mjs", True, False, False),
        ("resources/extensions/swarm/ui/SwarmPage.svelte", True, False, False),
        ("tests/fixtures/extension-pages/alpha/ui/main.js", True, False, False),
        ("webui/package.json", False, True, True),
        ("webui/package-lock.json", False, True, True),
        ("webui/eslint.config.js", False, True, False),
        ("webui/prettier.config.js", False, True, False),
        ("webui/vite.config.js", False, False, True),
        ("webui/index.html", False, False, True),
        ("webui/public/brand/vbot-icon.png", False, False, True),
        ("resources/extensions/swarm/web/index.js", False, False, False),
        ("tests/e2e/tests/chat.spec.js", False, False, False),
    ],
)
def test_webui_changes_select_the_checks_they_can_affect(
    path: str, source: bool, all_styles: bool, all_tests: bool
) -> None:
    scope = commit_check.webui_scope([path])

    assert (scope.sources == [path], scope.all_styles, scope.all_tests) == (
        source,
        all_styles,
        all_tests,
    )


LOCKED_PACKAGES: dict[str, dict[str, object]] = {
    "node_modules/vite": {
        "version": "8.0.3",
        "resolved": "https://registry.npmjs.org/vite/-/vite-8.0.3.tgz",
        "integrity": "sha512-vite",
        "dev": True,
    },
    # npm installs an optional package only on the platforms it names.
    "node_modules/fsevents": {
        "version": "2.3.3",
        "resolved": "https://registry.npmjs.org/fsevents/-/fsevents-2.3.3.tgz",
        "integrity": "sha512-fsevents",
        "optional": True,
        "os": ["darwin"],
    },
}
INSTALLED_PACKAGES = {"node_modules/vite": LOCKED_PACKAGES["node_modules/vite"]}
# npm copies the manifest's dependency maps into the lock's root entry.
DEPENDENCIES = {"devDependencies": {"vite": "^8.0.0"}, "optionalDependencies": {"fsevents": "^2"}}


def _webui_project(
    root: Path, monkeypatch: pytest.MonkeyPatch, installed: Mapping[str, object] | None
) -> list[str]:
    """Give *root* a WebUI with *installed* packages; return the commands the checks start.

    ``installed`` None means npm never installed into node_modules. The checks run
    nothing: each command is recorded and succeeds.
    """
    webui = root / "webui"
    for package in ("prettier", "eslint", "vitest"):
        manifest = webui / "node_modules" / package / "package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"bin": {package: f"bin/{package}.js"}}), encoding="utf-8")
    root_entry = {"name": "vbot-webui", **DEPENDENCIES}
    lock = {"lockfileVersion": 3, "packages": {"": root_entry, **LOCKED_PACKAGES}}
    (webui / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (webui / "package.json").write_text(json.dumps(root_entry), encoding="utf-8")
    if installed is not None:
        record = {"lockfileVersion": 3, "packages": installed}
        (webui / "node_modules" / ".package-lock.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    (webui / "src" / "lib").mkdir(parents=True)
    _write(root, "webui/src/lib/i18n.js", "export const locale = 'en';\n")
    commands: list[str] = []

    def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> Any:
        # "node <root>/webui/node_modules/vitest/bin/vitest.js run" -> "node vitest run"
        shown = [
            Path(part).stem if Path(part).suffix in {".js", ".cmd"} else part for part in command
        ]
        commands.append(" ".join(shown))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(commit_check, "_run", run)
    monkeypatch.setattr(commit_check.shutil, "which", lambda name: name)
    return commands


def _webui_checks(root: Path, path: str) -> list[commit_check.StepResult]:
    return [
        *commit_check.check_frontend(root, [path], set()),
        *commit_check.check_frontend_tests(root, [path]),
    ]


@pytest.mark.parametrize(
    ("installed", "runs"),
    [
        (INSTALLED_PACKAGES, True),
        (
            {"node_modules/vite": {**INSTALLED_PACKAGES["node_modules/vite"], "version": "8.0.1"}},
            False,
        ),
        (
            {"node_modules/vite": {**INSTALLED_PACKAGES["node_modules/vite"], "dev": False}},
            False,
        ),
        ({}, False),
        ({**INSTALLED_PACKAGES, "node_modules/left-pad": {"version": "1.3.0"}}, False),
        (None, False),
    ],
    ids=[
        "as locked",
        "other version",
        "other entry",
        "missing package",
        "extra package",
        "never installed",
    ],
)
def test_webui_checks_refuse_packages_that_differ_from_the_lock(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: Mapping[str, object] | None,
    runs: bool,
) -> None:
    commands = _webui_project(repo, monkeypatch, installed)

    results = _webui_checks(repo, "webui/src/lib/i18n.js")

    if runs:
        assert not any(result.blocking for result in results)
        assert "npm run build" in commands
    else:
        # Every WebUI check refuses until `npm ci` installs the locked packages.
        assert [(result.label, result.blocking) for result in results] == [
            ("webui deps", True),
            ("webui deps", True),
        ]
        assert commands == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("webui/vite.config.js", ["node vitest run", "npm run build"]),
        ("webui/eslint.config.js", ["npm run format:check", "npm run lint"]),
        (
            "webui/package-lock.json",
            ["npm run format:check", "npm run lint", "node vitest run", "npm run build"],
        ),
    ],
)
def test_webui_configuration_changes_run_the_complete_checks(
    repo: Path, monkeypatch: pytest.MonkeyPatch, path: str, expected: list[str]
) -> None:
    commands = _webui_project(repo, monkeypatch, INSTALLED_PACKAGES)

    results = _webui_checks(repo, path)

    assert commands == expected
    assert not any(result.blocking for result in results)


@pytest.mark.parametrize(
    ("manifest", "runs"),
    [
        (DEPENDENCIES, True),
        # npm leaves out of the lock a dependency that optionalDependencies lists too.
        ({**DEPENDENCIES, "dependencies": {"fsevents": "^2"}}, True),
        ({**DEPENDENCIES, "devDependencies": {"vite": "^8.1.0"}}, False),
    ],
    ids=["as locked", "optional listed twice", "edited without npm install"],
)
def test_webui_checks_refuse_a_package_manifest_the_lock_does_not_record(
    repo: Path, monkeypatch: pytest.MonkeyPatch, manifest: Mapping[str, object], runs: bool
) -> None:
    commands = _webui_project(repo, monkeypatch, INSTALLED_PACKAGES)
    _write(repo, "webui/package.json", json.dumps({"name": "vbot-webui", **manifest}))

    results = commit_check.check_frontend(repo, ["webui/package.json"], set())

    if runs:
        assert not any(result.blocking for result in results)
        assert "npm run lint" in commands
    else:
        # `npm ci`, as CI runs it, would refuse the lock; `npm install` updates it.
        assert [(result.label, result.blocking) for result in results] == [("webui deps", True)]
        assert commands == []


# The test step drives a real pytest-testmon run in a small project; its selection
# and the recorded dependencies are the behavior under test, so it cannot be faked.
IMPACT_PROJECT = {
    ".gitignore": ".testmondata*\n.testfiledeps\n__pycache__/\n.pytest_cache/\n",
    "pytest.ini": "[pytest]\n",
    "conftest.py": 'pytest_plugins = ["tests.file_dependencies"]\n',
    "calc.py": "def double(x):\n    return x * 2\n",
    "wip.py": "def triple(x):\n    return x * 3\n",
    # Mixed case: the records fold case where the filesystem ignores it (Windows).
    "Factor.txt": "2",
    "test_calc.py": "import calc\n\n\ndef test_double():\n    assert calc.double(2) == 4\n",
    "test_wip.py": "import wip\n\n\ndef test_triple():\n    assert wip.triple(2) == 6\n",
    "test_factor.py": (
        "from pathlib import Path\n"
        "\n"
        "import calc\n"
        "\n"
        "\n"
        "def test_factor():\n"
        '    assert calc.double(int(Path("Factor.txt").read_text())) < 10\n'
    ),
    "test_git.py": (
        "import subprocess\n"
        "\n"
        "\n"
        "def test_git_repository(tmp_path):\n"
        '    subprocess.run(["git", "init", "-q", "--bare"], cwd=tmp_path, check=True)\n'
    ),
}
HARMLESS_CALC = "def double(x):\n    return x * 2\n\n\ndef half(x):\n    return x / 2\n"
BROKEN_CALC = "def double(x):\n    return x * 3\n"
BROKEN_WIP = "def triple(x):\n    return x * 4\n"
HARMLESS_WIP = "def triple(x):\n    return x * 3\n\n\ndef third(x):\n    return x / 3\n"
# Wrong in its first run only, like a test that fails on a busy machine.
FLAKY_WIP = (
    "import os\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def triple(x):\n"
    '    first_run = Path(os.environ["FLAKY_MARKER"])\n'
    "    if not first_run.exists():\n"
    '        first_run.write_text("")\n'
    "        return x * 4\n"
    "    return x * 3\n"
)
FLAKY_CALC = (
    "import os\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def double(x):\n"
    '    first_run = Path(os.environ["FLAKY_MARKER"])\n'
    "    if not first_run.exists():\n"
    '        first_run.write_text("")\n'
    "        return x * 3\n"
    "    return x * 2\n"
)
# Keeps test_calc passing, but doubles a factor above 2 to 10 or more.
SKEWED_CALC = "def double(x):\n    return x * 2 if x < 3 else x * 3\n"
# The test step as a pre-merge-commit hook, like .githooks/pre-merge-commit.
MERGE_HOOK = """\
import sys
from pathlib import Path

from scripts import commit_check

root = Path.cwd()
changed = sorted({*commit_check.staged_files(root), *commit_check.staged_deletions(root)})
results = commit_check.check_tests(root, changed, commit_check.dirty_files(root))
for result in results:
    print(result.status, result.details, sep="\\n")
sys.exit(any(result.blocking for result in results))
"""


@pytest.fixture(scope="module")
def seeded_impact_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("impact") / "project"
    root.mkdir()
    for name, content in IMPACT_PROJECT.items():
        (root / name).write_bytes(content.encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base", "--no-verify")
    seed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--testmon"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "COVERAGE_CORE": "ctrace"},
        capture_output=True,
        text=True,
        timeout=90,  # Within the test budget: a hang fails here, not the worker.
    )
    assert seed.returncode == 0, seed.stdout + seed.stderr
    # Durations as an idle machine records them: a seed slowed by load would make
    # the checks start xdist workers, slower still and a different run.
    with closing(sqlite3.connect(root / _test_impact.TESTMON_DATA)) as records, records:
        records.execute("UPDATE test_execution SET duration = 0.01")
    # As the primary checkout's last commit check would have recorded it.
    _test_impact.record_tested_state(root, _git(root, "write-tree").strip(), ())
    return root


@pytest.fixture
def impact_project(
    seeded_impact_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    root = tmp_path / "project"
    shutil.copytree(seeded_impact_project, root)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("COVERAGE_CORE", "ctrace")
    return root


def _write(root: Path, path: str, content: str) -> None:
    (root / path).write_bytes(content.encode("utf-8"))


def _check_tests(root: Path) -> dict[str, tuple[bool, str]]:
    changed = sorted({*commit_check.staged_files(root), *commit_check.staged_deletions(root)})
    results = commit_check.check_tests(root, changed, commit_check.dirty_files(root))
    return {result.status: (result.blocking, result.details) for result in results}


@pytest.mark.parametrize(
    ("records", "path", "content"),
    [
        ("missing", "calc.py", HARMLESS_CALC),
        ("missing", "Factor.txt", "9"),
        ("without tested state", "Factor.txt", "9"),
        ("corrupt", "calc.py", HARMLESS_CALC),
    ],
    ids=["no records, code", "no records, data", "no tested state", "corrupt"],
)
def test_a_checkout_without_usable_test_impact_data_runs_the_complete_suite(
    impact_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    records: str,
    path: str,
    content: str,
) -> None:
    # The pytest command is recorded instead of started.
    commands: list[list[str]] = []
    environments: list[dict[str, str] | None] = []

    def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> Any:
        commands.append(command)
        environments.append(env)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(commit_check, "_run", run)
    testmon_data = impact_project / _test_impact.TESTMON_DATA
    file_reads = impact_project / _test_impact.DATA_FILE
    if records == "missing":
        testmon_data.unlink()
        file_reads.unlink()
    elif records == "without tested state":
        # As a plain `pytest --testmon` run leaves them: they describe no known tree.
        connection = sqlite3.connect(file_reads)
        with connection:
            connection.execute("DELETE FROM tested_state")
        connection.close()
    else:
        testmon_data.write_bytes(b"not a database, " * 64)
    _write(impact_project, path, content)
    _git(impact_project, "add", path)

    assert _check_tests(impact_project) == {"PASS": (False, "")}

    [command] = commands
    # As many workers as the test core pool lets a check hold.
    assert command[-2:] == ["-n", str(cpu_pool.check_cores())]
    [env] = environments
    assert env is not None
    assert env[cpu_pool.KIND_VARIABLE] == "commit"
    assert env[cpu_pool.REASON_VARIABLE]
    assert env[cpu_pool.REASON_VARIABLE] in capsys.readouterr().out
    if records == "corrupt":
        # testmon cannot open it: the complete run starts without it and records afresh.
        assert not testmon_data.exists()


@pytest.mark.parametrize(
    ("calc", "status"),
    [
        (BROKEN_CALC, "FAIL: tests affected by this commit"),
        (FLAKY_CALC, "NOT BLOCKING: failed, then passed when run again alone"),
    ],
    ids=["failing", "passing alone"],
)
def test_failure_caused_by_the_staged_change_blocks_unless_it_passes_alone(
    impact_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calc: str, status: str
) -> None:
    monkeypatch.setenv("FLAKY_MARKER", str(tmp_path / "first-run"))
    _write(impact_project, "calc.py", calc)
    _git(impact_project, "add", "calc.py")

    results = _check_tests(impact_project)

    blocking, details = results[status]
    assert blocking is status.startswith("FAIL")
    assert any(result[0] for result in results.values()) is blocking
    assert "test_calc.py::test_double" in details
    assert "test_wip.py" not in details


def test_failure_in_unstaged_work_of_another_file_does_not_block(impact_project: Path) -> None:
    _write(impact_project, "calc.py", HARMLESS_CALC)
    _git(impact_project, "add", "calc.py")
    _write(impact_project, "wip.py", BROKEN_WIP)

    results = _check_tests(impact_project)

    assert not any(blocking for blocking, _details in results.values())
    assert "PASS" in results
    _blocking, details = results["NOT BLOCKING: failures depending on uncommitted work in progress"]
    assert "test_wip.py::test_triple" in details


@pytest.mark.parametrize(
    ("wip", "status"),
    [
        (BROKEN_WIP, "FAIL: tests failing on committed code; fix them in a separate commit first"),
        (FLAKY_WIP, "NOT BLOCKING: failed, then passed when run again alone"),
    ],
    ids=["failing", "passing alone"],
)
def test_failure_on_committed_code_blocks_every_commit_unless_it_passes_alone(
    impact_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wip: str, status: str
) -> None:
    monkeypatch.setenv("FLAKY_MARKER", str(tmp_path / "first-run"))
    _write(impact_project, "wip.py", wip)
    _git(impact_project, "commit", "-q", "-am", "unchecked", "--no-verify")
    _write(impact_project, "calc.py", HARMLESS_CALC)
    _git(impact_project, "add", "calc.py")

    results = _check_tests(impact_project)

    blocking, details = results[status]
    assert blocking is status.startswith("FAIL")
    assert any(result[0] for result in results.values()) is blocking
    assert "test_wip.py::test_triple" in details


def test_staged_data_file_runs_the_tests_that_read_it(impact_project: Path) -> None:
    _write(impact_project, "Factor.txt", "9")
    _git(impact_project, "add", "Factor.txt")

    results = _check_tests(impact_project)

    blocking, details = results["FAIL: tests affected by this commit"]
    assert blocking
    assert "test_factor.py::test_factor" in details


def test_tests_run_by_the_hook_cannot_reach_the_committing_repository(
    impact_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A git hook exports these; a test's own git calls must not inherit them.
    monkeypatch.setenv("GIT_DIR", str(impact_project / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(impact_project / ".git" / "index"))
    monkeypatch.setenv("GIT_REFLOG_ACTION", "merge task")
    # worktree.py merge marks the merge commit it checks in its landing checkout.
    monkeypatch.setenv(commit_check.LANDING_VARIABLE, "1")
    # The changed test reports whether its git call stayed in its own directory.
    probe = (
        '    head = tmp_path / "HEAD"\n'
        '    leaked = not head.is_file() or "GIT_REFLOG_ACTION" in os.environ\n'
        f'    leaked = leaked or "{commit_check.LANDING_VARIABLE}" in os.environ\n'
        '    raise AssertionError("leaked" if leaked else "isolated")\n'
    )
    _write(impact_project, "test_git.py", "import os\n" + IMPACT_PROJECT["test_git.py"] + probe)
    _git(impact_project, "add", "test_git.py")

    _blocking, details = _check_tests(impact_project)["FAIL: tests affected by this commit"]

    assert "AssertionError: isolated" in details
    assert _git(impact_project, "config", "core.bare").strip() == "false"


def _merge_after_checked_commits(
    primary: Path,
    worktree: Path,
    main_change: tuple[str, str],
    branch_change: tuple[str, str],
    *,
    rebase: bool = False,
) -> str:
    """Commit *branch_change* in a new worktree and pass its branch check, commit
    *main_change* in *primary* after a passing commit check, and merge the worktree's
    branch through the test step as pre-merge-commit hook, as ``scripts/worktree.py
    merge`` does. Return the merge output; *rebase* first rebases the branch after
    its check, as before a hand merge."""
    _git(primary, "worktree", "add", "-q", "-b", "task", str(worktree))
    path, content = branch_change
    _write(worktree, path, content)
    _git(worktree, "add", path)
    _git(worktree, "commit", "-q", "-m", path, "--no-verify")
    assert commit_check.check_branch(worktree) == 0
    path, content = main_change
    _write(primary, path, content)
    _git(primary, "add", path)
    assert _check_tests(primary) == {"PASS": (False, "")}
    _git(primary, "commit", "-q", "-m", path, "--no-verify")
    if rebase:
        _git(worktree, "rebase", "-q", _git(primary, "rev-parse", "HEAD").strip())
    hooks = primary / ".git" / "test-hooks"
    hooks.mkdir()
    (hooks / "check.py").write_text(MERGE_HOOK, encoding="utf-8")
    hook = hooks / "pre-merge-commit"
    python = Path(sys.executable).as_posix()
    hook.write_text(f'#!/bin/sh\nexec "{python}" .git/test-hooks/check.py\n', encoding="utf-8")
    hook.chmod(0o755)
    _git(primary, "config", "core.hooksPath", ".git/test-hooks")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "-m", "merge", "task"],
        cwd=primary,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return f"exit code {merge.returncode}\n{merge.stdout}{merge.stderr}"


def test_merge_commit_reuses_the_test_runs_of_both_sides(
    impact_project: Path, tmp_path: Path
) -> None:
    output = _merge_after_checked_commits(
        impact_project, tmp_path / "worktree", ("wip.py", HARMLESS_WIP), ("calc.py", HARMLESS_CALC)
    )

    assert output.startswith("exit code 0\n"), output
    assert "PASS (no test affected)" in output

    # The merged checkout adopted the branch's runs: calc.py is as the branch tested it.
    _write(impact_project, "notes.py", "NOTE = 1\n")
    _git(impact_project, "add", "notes.py")
    assert _check_tests(impact_project) == {"PASS (no test affected)": (False, "")}


@pytest.mark.parametrize("rebase", [False, True], ids=["merged", "rebased"])
def test_merge_commit_runs_a_test_both_sides_changed(
    impact_project: Path, tmp_path: Path, rebase: bool
) -> None:
    # Each side passes alone; merged, test_factor doubles the factor 4 to 12. A
    # rebased branch holds both changes, but its test runs saw only its own.
    output = _merge_after_checked_commits(
        impact_project,
        tmp_path / "worktree",
        ("Factor.txt", "4"),
        ("calc.py", SKEWED_CALC),
        rebase=rebase,
    )

    assert not output.startswith("exit code 0\n")
    assert "FAIL: tests affected by this commit" in output
    assert "test_factor.py::test_factor" in output


# Two branch checks and a commit check each start pytest in the project.
@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("main_change", "branch_change", "expected"),
    [
        (("calc.py", HARMLESS_CALC), ("wip.py", HARMLESS_WIP), "PASS (no test affected)"),
        (("Factor.txt", "4"), ("calc.py", SKEWED_CALC), "FAIL: tests affected by this commit"),
    ],
    ids=["main tested its change", "both sides changed"],
)
def test_branch_check_after_a_rebase_runs_only_the_tests_main_did_not_run(
    impact_project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    main_change: tuple[str, str],
    branch_change: tuple[str, str],
    expected: str,
) -> None:
    worktree = tmp_path / "worktree"
    _git(impact_project, "worktree", "add", "-q", "-b", "task", str(worktree))
    path, content = branch_change
    _write(worktree, path, content)
    _git(worktree, "add", path)
    _git(worktree, "commit", "-q", "-m", path, "--no-verify")
    assert commit_check.check_branch(worktree) == 0
    path, content = main_change
    _write(impact_project, path, content)
    _git(impact_project, "add", path)
    assert _check_tests(impact_project) == {"PASS": (False, "")}
    _git(impact_project, "commit", "-q", "-m", path, "--no-verify")
    _git(worktree, "rebase", "-q", _git(impact_project, "rev-parse", "HEAD").strip())
    capsys.readouterr()

    exit_code = commit_check.check_branch(worktree)

    output = capsys.readouterr().out
    assert expected in output
    assert (exit_code == 0) is expected.startswith("PASS")
    # Merged, test_factor doubles the factor 4 to 12; neither side ran it so.
    assert ("test_factor.py::test_factor" in output) is (exit_code != 0)


# The branch check starts a nested pytest process under parallel suite load.
@pytest.mark.timeout(120)
def test_worktree_commits_leave_the_tests_to_the_branch_check(
    impact_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worktree = tmp_path / "worktree"
    _git(impact_project, "worktree", "add", "-q", "-b", "task", str(worktree))
    _write(worktree, "calc.py", BROKEN_CALC)
    _git(worktree, "add", "calc.py")
    # Ruff and mypy are not under test here.
    passed = [commit_check.StepResult("mypy", "PASS", False)]
    monkeypatch.setattr(commit_check, "check_python", lambda *_arguments: passed)

    assert commit_check.main(worktree) == 0
    assert "NOT RUN in a worktree" in capsys.readouterr().out
    _git(worktree, "commit", "-q", "-m", "calc", "--no-verify")

    assert commit_check.main(worktree, ["--branch"]) == 1

    # A worktree made without scripts/worktree.py takes the records over now.
    output = capsys.readouterr().out
    assert f"using the test-impact data of {impact_project}" in output
    assert "running the complete suite" not in output
    assert "FAIL: tests affected by this commit" in output
    assert "test_calc.py::test_double" in output
    assert "test_wip.py" not in output


def test_the_merge_commit_in_a_landing_checkout_runs_the_tests(
    impact_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # worktree.py merge checks each merge commit in a linked landing checkout.
    landing = tmp_path / "landing"
    _git(impact_project, "worktree", "add", "-q", "--detach", str(landing))
    _write(landing, "calc.py", BROKEN_CALC)
    _git(landing, "add", "calc.py")
    passed = [commit_check.StepResult("mypy", "PASS", False)]
    monkeypatch.setattr(commit_check, "check_python", lambda *_arguments: passed)
    tested = []

    def check_tests(root: Path, changed: list[str], _dirty: set[str]) -> list:
        tested.append((root, changed))
        return [commit_check.StepResult("pytest", "PASS", False)]

    monkeypatch.setattr(commit_check, "check_tests", check_tests)
    monkeypatch.setattr(commit_check, "check_frontend_tests", lambda *_arguments: [])
    monkeypatch.setenv(commit_check.LANDING_VARIABLE, "1")

    assert commit_check.main(landing) == 0
    assert tested == [(landing, ["calc.py"])]


@pytest.mark.parametrize(
    ("path", "content", "reported"),
    [
        ("calc.py", BROKEN_CALC, "test_calc.py::test_double"),
        # Like pytest-timeout ending the run: no report, and no record of the test.
        (
            "tests/test_crash.py",
            "import os\n\n\ndef test_crash():\n    os._exit(1)\n",
            "FAIL (exit code 1)",
        ),
    ],
    ids=["failing test", "run ended without report"],
)
def test_a_failed_branch_check_fails_again_when_retried(
    impact_project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    path: str,
    content: str,
    reported: str,
) -> None:
    worktree = tmp_path / "worktree"
    _git(impact_project, "worktree", "add", "-q", "-b", "task", str(worktree))
    (worktree / path).parent.mkdir(exist_ok=True)
    _write(worktree, path, content)
    _git(worktree, "add", path)
    _git(worktree, "commit", "-q", "-m", path, "--no-verify")

    # A retry without a fix, as after a failure blamed on a busy machine.
    for _attempt in range(2):
        assert commit_check.check_branch(worktree) == 1
        assert reported in capsys.readouterr().out


def test_a_test_that_failed_last_time_runs_with_every_commit_until_it_passes(
    impact_project: Path,
) -> None:
    # As testmon records a failure that no change since the tested state explains.
    with closing(sqlite3.connect(impact_project / _test_impact.TESTMON_DATA)) as records, records:
        records.execute(
            "UPDATE test_execution SET failed = 1 WHERE test_name = ?",
            ("test_wip.py::test_triple",),
        )
    for number in (1, 2):
        _write(impact_project, f"notes{number}.txt", "read by no test")
        _git(impact_project, "add", f"notes{number}.txt")

        # It runs with an unrelated change, passes, and is left out afterwards.
        assert _check_tests(impact_project) == ({"PASS": (False, "")} if number == 1 else {})
        _git(impact_project, "commit", "-q", "-m", f"notes {number}", "--no-verify")


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        ("notes.txt", "unread", {}),
        (
            "calc.py",
            "# Arithmetic helpers.\n" + IMPACT_PROJECT["calc.py"],
            {"PASS (no test affected)": (False, "")},
        ),
    ],
    ids=["unread data file", "code change no test executes"],
)
def test_unaffected_change_starts_no_test_run(
    impact_project: Path, path: str, content: str, expected: dict[str, tuple[bool, str]]
) -> None:
    _write(impact_project, path, content)
    _git(impact_project, "add", path)

    assert _check_tests(impact_project) == expected
