from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import commit_check

REPO_ROOT = Path(__file__).resolve().parents[2]

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
    impact_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A project with test-impact data: the test step finds no affected test.
    # Another session's unstaged edit of a committed file must stay as it is.
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


def test_partially_staged_file_is_left_alone_and_blocks(repo: Path) -> None:
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
    ("path", "expected"),
    [
        ("webui/src/lib/i18n.js", True),
        ("webui/scripts/build-extension-pages.mjs", True),
        ("resources/extensions/swarm/ui/SwarmPage.svelte", True),
        ("tests/fixtures/extension-pages/alpha/ui/main.js", True),
        ("resources/extensions/swarm/web/index.js", False),
        ("webui/package.json", False),
        ("tests/e2e/tests/chat.spec.js", False),
    ],
)
def test_frontend_sources_match_the_checked_roots(path: str, expected: bool) -> None:
    assert commit_check.is_frontend_source(path) is expected


# The test step drives a real pytest-testmon run in a small project; its selection
# and the recorded dependencies are the behavior under test, so it cannot be faked.
IMPACT_PROJECT = {
    ".gitignore": ".testmondata*\n.testfiledeps\n__pycache__/\n.pytest_cache/\n",
    "pytest.ini": "[pytest]\n",
    "conftest.py": 'pytest_plugins = ["tests.file_dependencies"]\n',
    "calc.py": "def double(x):\n    return x * 2\n",
    "wip.py": "def triple(x):\n    return x * 3\n",
    "factor.txt": "2",
    "test_calc.py": "import calc\n\n\ndef test_double():\n    assert calc.double(2) == 4\n",
    "test_wip.py": "import wip\n\n\ndef test_triple():\n    assert wip.triple(2) == 6\n",
    "test_factor.py": (
        "from pathlib import Path\n"
        "\n"
        "import calc\n"
        "\n"
        "\n"
        "def test_factor():\n"
        '    assert calc.double(int(Path("factor.txt").read_text())) < 10\n'
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
        timeout=120,
    )
    assert seed.returncode == 0, seed.stdout + seed.stderr
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


def test_a_checkout_without_test_impact_data_runs_the_complete_suite(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The pytest command is recorded instead of started.
    commands: list[list[str]] = []

    def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> Any:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(commit_check, "_run", run)
    _write(repo, "module.py", FORMATTED)
    _git(repo, "add", "module.py")

    assert _check_tests(repo) == {"PASS": (False, "")}

    [command] = commands
    assert command[-2:] == ["-n", "auto"]
    assert "no usable test-impact data" in capsys.readouterr().out


def test_failure_caused_by_the_staged_change_blocks(impact_project: Path) -> None:
    _write(impact_project, "calc.py", BROKEN_CALC)
    _git(impact_project, "add", "calc.py")

    results = _check_tests(impact_project)

    blocking, details = results["FAIL: tests affected by this commit"]
    assert blocking
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
        (FLAKY_WIP, "NOT BLOCKING: failed on committed code, then passed when run again alone"),
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
    _write(impact_project, "factor.txt", "9")
    _git(impact_project, "add", "factor.txt")

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
    # The changed test reports whether its git call stayed in its own directory.
    probe = (
        '    head = tmp_path / "HEAD"\n'
        '    leaked = not head.is_file() or "GIT_REFLOG_ACTION" in os.environ\n'
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
    """Commit *branch_change* in a new worktree and *main_change* in *primary*, each
    after a passing commit check, and merge the worktree's branch through the test
    step as pre-merge-commit hook, as ``scripts/worktree.py merge`` does. Return the
    merge output; *rebase* first rebases the branch, which runs no hook."""
    _git(primary, "worktree", "add", "-q", "-b", "task", str(worktree))
    for root, (path, content) in ((worktree, branch_change), (primary, main_change)):
        _write(root, path, content)
        _git(root, "add", path)
        assert _check_tests(root) == {"PASS": (False, "")}
        _git(root, "commit", "-q", "-m", path, "--no-verify")
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


# Two commit checks and the merge hook each start pytest in the project; under a
# loaded commit check that exceeds the default 30 s.
@pytest.mark.timeout(120)
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


@pytest.mark.timeout(120)
@pytest.mark.parametrize("rebase", [False, True], ids=["merged", "rebased"])
def test_merge_commit_runs_a_test_both_sides_changed(
    impact_project: Path, tmp_path: Path, rebase: bool
) -> None:
    # Each side passes alone; merged, test_factor doubles the factor 4 to 12. A
    # rebased branch holds both changes, but its test runs saw only its own.
    output = _merge_after_checked_commits(
        impact_project,
        tmp_path / "worktree",
        ("factor.txt", "4"),
        ("calc.py", SKEWED_CALC),
        rebase=rebase,
    )

    assert not output.startswith("exit code 0\n")
    assert "FAIL: tests affected by this commit" in output
    assert "test_factor.py::test_factor" in output


def test_first_commit_in_a_worktree_adopts_the_primary_checkout_data(
    impact_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = tmp_path / "worktree"
    _git(impact_project, "worktree", "add", "-q", "-b", "task", str(worktree))
    _write(worktree, "calc.py", BROKEN_CALC)
    _git(worktree, "add", "calc.py")

    results = _check_tests(worktree)

    output = capsys.readouterr().out
    assert f"using the test-impact data of {impact_project}" in output
    assert "no usable test-impact data" not in output
    assert "test_calc.py::test_double" in results["FAIL: tests affected by this commit"][1]


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
