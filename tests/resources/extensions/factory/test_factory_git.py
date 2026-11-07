"""Fixed-argv, bounded Git inspection and exact submitted commit facts."""

from __future__ import annotations

import asyncio
from typing import override

import pytest

from resources.extensions.factory import _git
from resources.extensions.factory._git import GitRepository
from resources.extensions.factory.store import FactoryError

_HEAD = "a" * 40
_BASE = "b" * 40
_MERGE = "c" * 40


class Repository(GitRepository):
    def __init__(self, *, branch=True, base=True, count="2", merge=True):
        super().__init__("C:/repo")
        self.branch_exists = branch
        self.base_exists = base
        self.count = count
        self.common = merge
        self.commands = []

    @override
    async def _run(self, *arguments: str, allowed=(0,)):
        self.commands.append(arguments)
        command = arguments[0]
        if command == "check-ref-format":
            return 0, arguments[-1]
        if command == "rev-parse":
            branch = arguments[-1].startswith("refs/heads/")
            return (
                (0, _HEAD if branch else _BASE)
                if (self.branch_exists if branch else self.base_exists)
                else (128, "")
            )
        if command == "rev-list":
            return 0, self.count
        return (0, _MERGE) if self.common else (1, "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({}, None),
        ({"branch": False}, "missing_branch"),
        ({"base": False}, "missing_base"),
        ({"count": "0"}, "no_commits"),
        ({"merge": False}, "unrelated_history"),
    ],
)
async def test_submission_resolves_local_branch_base_and_nonempty_exact_range(options, code):
    repository = Repository(**options)
    if code:
        with pytest.raises(FactoryError) as caught:
            await repository.submission("topic", "main")
        assert caught.value.code == code
    else:
        assert await repository.submission("topic", "main") == {
            "branch": "topic",
            "base": "main",
            "base_sha": _MERGE,
            "head_sha": _HEAD,
        }
        assert ("rev-list", "--count", f"{_BASE}..{_HEAD}") in repository.commands
        assert (
            "rev-parse",
            "--verify",
            "--end-of-options",
            "refs/heads/topic^{commit}",
        ) in repository.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["--help", "@{-1}", "", "a\x00b"])
async def test_branch_expressions_and_options_never_reach_git(branch):
    repository = Repository()
    with pytest.raises(FactoryError) as caught:
        await repository.submission(branch, "main")
    assert caught.value.code == "invalid_ref" and not repository.commands


class Process:
    def __init__(self, output: bytes, *, running=False):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        if not running:
            self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = None if running else 0
        self.done = asyncio.Event()
        self.killed = False
        if not running:
            self.done.set()

    async def wait(self):
        await self.done.wait()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1
        self.stdout.feed_eof()
        self.done.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "output", "timeout", "missing"])
async def test_git_process_is_fixed_argv_bounded_and_terminated_on_failure(
    monkeypatch, tmp_path, case
):
    process = Process(
        b"x" * (_git._OUTPUT_LIMIT + 1) if case == "output" else b"result",
        running=case in {"timeout", "output"},
    )
    calls = []

    async def launch(*arguments, **kwargs):
        calls.append((arguments, kwargs))
        if case == "missing":
            raise FileNotFoundError("git")
        return process

    monkeypatch.setenv("GIT_DIR", "wrong-repository")
    monkeypatch.setattr(_git.asyncio, "create_subprocess_exec", launch)
    if case == "timeout":
        monkeypatch.setattr(_git, "_TIMEOUT", 0)
    repository = GitRepository(tmp_path)
    if case == "normal":
        assert await repository._run("rev-parse", "--show-toplevel") == (0, "result")
    else:
        with pytest.raises(FactoryError) as caught:
            await repository._run("rev-parse", "--show-toplevel")
        assert (
            caught.value.code
            == {
                "output": "git_output_limit",
                "timeout": "git_timeout",
                "missing": "git_unavailable",
            }[case]
        )
        if case != "missing":
            assert process.killed
    assert calls[0][0][0] == "git" and "--no-optional-locks" in calls[0][0]
    assert "GIT_DIR" not in calls[0][1]["env"]
    assert calls[0][1]["cwd"] == tmp_path
