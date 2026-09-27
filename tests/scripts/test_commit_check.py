from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import commit_check

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


def test_fully_staged_file_is_fixed_and_restaged(repo: Path) -> None:
    (repo / "module.py").write_text(UNFORMATTED)
    _git(repo, "add", "module.py")

    assert commit_check.main(repo) == 0

    assert (repo / "module.py").read_text() == FORMATTED
    assert _staged_content(repo, "module.py") == FORMATTED


def test_partially_staged_file_is_left_alone_and_blocks(repo: Path) -> None:
    (repo / "module.py").write_text(UNFORMATTED)
    _git(repo, "add", "module.py")
    work_in_progress = UNFORMATTED + "other  =  2\n"
    (repo / "module.py").write_text(work_in_progress)

    assert commit_check.main(repo) == 1

    assert (repo / "module.py").read_text() == work_in_progress
    assert _staged_content(repo, "module.py") == UNFORMATTED


def test_unstaged_work_of_another_file_is_not_touched(repo: Path) -> None:
    (repo / "staged.py").write_text(FORMATTED)
    (repo / "other.py").write_text(FORMATTED)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base", "--no-verify")
    (repo / "staged.py").write_text(FORMATTED + "extra = 1\n")
    _git(repo, "add", "staged.py")
    (repo / "other.py").write_text(UNFORMATTED)

    assert commit_check.main(repo) == 0

    assert (repo / "other.py").read_text() == UNFORMATTED
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["staged.py"]


def test_mypy_errors_block_unless_in_unstaged_work_in_progress() -> None:
    output = "\n".join(
        [
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
