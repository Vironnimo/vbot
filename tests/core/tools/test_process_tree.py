"""A command's process tree: survivors, child exit codes, CPU time and the kill."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import psutil  # type: ignore[import-untyped]
import pytest

from core.tools import process_tree
from core.tools.process_tree import (
    KillNotConfirmedError,
    ProcessTree,
    ProcessTreeFacts,
    track_process_tree,
)

# Real processes: the contract is the operating system's. Each root waits for a
# line before it starts children, so the tracker is in place first.

# The survivor works briefly before it sleeps: on POSIX only running members
# count towards the CPU time, which the kernel charges in clock ticks.
_SURVIVOR = """
import time
start = time.process_time()
while time.process_time() - start < 0.05:
    pass
time.sleep(60)
"""
_ROOT_LEAVING_A_SURVIVOR = f"""
import subprocess, sys
sys.stdin.readline()
subprocess.Popen([sys.executable, "-c", {_SURVIVOR!r}])
"""
# The root keeps its failed child's handle open until the test ends: a child
# whose last handle closes before the tree has handled the job notification of
# its start leaves no exit code, and a loaded machine delays that handling.
_ROOT_WITH_A_FAILED_CHILD = """
import subprocess, sys
sys.stdin.readline()
failed = subprocess.Popen([sys.executable, "-c", "raise SystemExit(5)"])
failed.wait()
sys.stdin.readline()
"""


@contextlib.contextmanager
def _tracked_root(script: str) -> Iterator[tuple[subprocess.Popen[str], ProcessTree]]:
    root = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )
    tree = track_process_tree(root.pid)
    try:
        yield root, tree
    finally:
        with contextlib.suppress(OSError):
            tree.terminate()
        tree.close()
        if root.poll() is None:
            root.kill()
        root.wait(timeout=20)
        if root.stdin is not None:
            root.stdin.close()


def _start_children(root: subprocess.Popen[str]) -> None:
    assert root.stdin is not None
    root.stdin.write("go\n")
    root.stdin.flush()


def _first_facts(tree: ProcessTree, ready: Callable[[ProcessTreeFacts], bool]) -> ProcessTreeFacts:
    """The first facts that are *ready*, or the last ones after a generous deadline."""
    deadline = time.monotonic() + 10
    while not ready(facts := tree.facts()) and time.monotonic() < deadline:
        time.sleep(0.02)
    return facts


def test_tree_lists_the_processes_the_root_left_running_until_terminate_kills_them() -> None:
    with _tracked_root(_ROOT_LEAVING_A_SURVIVOR) as (root, tree):
        before = tree.facts()
        assert [process.pid for process in before.running] == [root.pid]

        _start_children(root)
        assert root.wait(timeout=20) == 0

        def survivor_listed(facts: ProcessTreeFacts) -> bool:
            # Windows can still list the exited root briefly.
            running = [process.pid for process in facts.running]
            return len(running) == 1 and root.pid not in running and facts.cpu_seconds > 0

        facts = _first_facts(tree, survivor_listed)
        assert len(facts.running) == 1
        assert facts.running[0].pid != root.pid
        assert facts.running[0].name.lower().startswith("python")
        assert facts.cpu_seconds > 0
        assert facts.started > before.started

        tree.terminate()
        assert tree.facts().running == ()


class _Member:
    """A session member that ignores the kill or that vBot may not signal."""

    def __init__(self, *, may_signal: bool) -> None:
        self.may_signal = may_signal

    def kill(self) -> None:
        if not self.may_signal:
            raise psutil.AccessDenied(4242)


@pytest.mark.parametrize("sent", [False, True], ids=["refused", "not-confirmed"])
def test_posix_terminate_tells_a_refused_kill_from_one_not_confirmed(
    monkeypatch: pytest.MonkeyPatch, sent: bool
) -> None:
    # Survivors that a real kill leaves are rare (a process stuck in the
    # kernel, or one of another user), so the operating system's answers are faked.
    def killpg(sid: int, signal_number: int) -> None:
        if not sent:
            raise PermissionError("not permitted")

    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    monkeypatch.setattr(process_tree, "_TERMINATE_CONFIRM_SECONDS", 0.0)
    tree = process_tree._PosixSessionTree(4242)
    member = _Member(may_signal=sent)
    monkeypatch.setattr(tree, "_live_members", lambda: [member])

    with pytest.raises(OSError) as failure:
        tree.terminate()

    # Only a kill that reached the tree leaves processes that end of it later.
    assert isinstance(failure.value, KillNotConfirmedError) == sent


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows reports child exit codes")
def test_tree_records_the_exit_code_of_a_failed_direct_child() -> None:
    with _tracked_root(_ROOT_WITH_A_FAILED_CHILD) as (root, tree):
        _start_children(root)
        facts = _first_facts(tree, lambda facts: bool(facts.nonzero_exits))
        assert [(failure.name, failure.exit_code) for failure in facts.nonzero_exits] == [
            (os.path.basename(sys.executable), 5)
        ]


@pytest.mark.parametrize(
    ("name", "arguments", "invocation"),
    [
        ("python.exe", ["python", "-m", "package.check", "--token", "SECRET"], "-m package.check"),
        ("pythonw.exe", ["pythonw", "scripts/check.py", "SECRET"], "scripts/check.py"),
        (
            "Python3.14.exe",
            ["python", r"C:\my scripts\check.py", "SECRET"],
            "C:/my scripts/check.py",
        ),
        ("python.exe", ["python", "-c", "print('SECRET')", "other.py"], "-c"),
        ("python.exe", ["python", "-cprint('SECRET')"], "-c"),
        ("python.exe", ["python", "-", "SECRET"], "-"),
        (
            "python.exe",
            ["python", "-uB", "-X", "SECRET", "-Wignore:SECRET", "-mpackage.check", "SECRET"],
            "-m package.check",
        ),
        ("python.exe", ["python", "--", "-script.py", "SECRET"], "-script.py"),
        ("python.exe", ["python", "--unknown", "SECRET", "script.py"], ""),
        ("python.exe", ["python", "-m", "SECRET\nnot-a-module"], ""),
        ("python.exe", ["python", "SECRET\nscript.py"], ""),
        ("python.exe", ["python", "x" * 201, "SECRET"], ""),
        ("python.exe", psutil.AccessDenied(2), ""),
        ("python.exe", psutil.NoSuchProcess(2), ""),
        ("curl.exe", ["curl", "--user", "SECRET"], ""),
        ("node.exe", ["node", r"tests\check.js", "--token", "SECRET"], "tests/check.js"),
        ("node.exe", ["node", "-e", "console.log('SECRET')"], "-e"),
        ("node.exe", ["node", "-pe", "'SECRET'"], "-p"),
        (
            "node.exe",
            ["node", "--input-type=module", "-r", "SECRET", "--no-warnings", "app.mjs"],
            "app.mjs",
        ),
        ("node.exe", ["node", "--test", "SECRET.test.js"], "--test"),
        ("node.exe", ["node", "--unknown", "SECRET", "app.js"], ""),
        (
            "node.exe",
            ["node", "C:/nodejs/node_modules/npm/bin/npm-cli.js", "run", "build:prod", "SECRET"],
            "npm run build:prod",
        ),
        ("node.exe", ["node", "npm-cli.js", "run", "https://SECRET@host/x"], "npm run"),
        ("node.exe", ["node", "npm-cli.js", "install", "SECRET"], "npm install"),
        ("node.exe", ["node", "npm-cli.js", "--prefix", "SECRET", "test"], "npm"),
        (
            "node.exe",
            ["node", "npx-cli.js", "-y", "create-vite@5.2.0", "SECRET"],
            "npx create-vite@5.2.0",
        ),
        ("node.exe", ["node", "npx-cli.js", "https://SECRET@host/x.tgz"], "npx"),
    ],
    ids=[
        "module",
        "script",
        "script-path",
        "inline-code",
        "attached-inline-code",
        "stdin",
        "interpreter-options",
        "end-of-options",
        "unknown-option",
        "invalid-module",
        "control-bearing-path",
        "long-path",
        "access-denied",
        "already-exited",
        "other-program",
        "node-script",
        "node-inline-code",
        "node-print",
        "node-options",
        "node-test-runner",
        "node-unknown-option",
        "npm-run-script",
        "npm-run-unsafe-name",
        "npm-command",
        "npm-option-first",
        "npx-package",
        "npx-url",
    ],
)
def test_child_start_retains_only_its_invocation_selector_through_exit(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: list[str] | psutil.Error,
    invocation: str,
) -> None:
    # Drive the OS notification boundary deterministically: command-line reads
    # can fail for an exited child even while its retained handle has its code.
    reads: list[int] = []
    closed: list[int] = []

    def image_name(handle: int, flags: int, buffer: Any, size: Any) -> bool:
        buffer.value = name
        return True

    def parent(handle: int, kind: int, information: Any, size: int, returned: Any) -> int:
        information._obj.InheritedFromUniqueProcessId = 1
        return 0

    def exit_code(handle: int, code: Any) -> bool:
        code._obj.value = 5
        return True

    class Child:
        def __init__(self, pid: int) -> None:
            reads.append(pid)

        def cmdline(self) -> list[str]:
            if isinstance(arguments, psutil.Error):
                raise arguments
            return arguments

    kernel32 = SimpleNamespace(
        OpenProcess=lambda access, inherit, pid: pid,
        CloseHandle=closed.append,
        QueryFullProcessImageNameW=image_name,
        GetExitCodeProcess=exit_code,
        QueryInformationJobObject=lambda *args: True,
    )
    # Job creation belongs to the real-process tests above; these notifications
    # enter the same tracker and facts interface without launching processes.
    tree = object.__new__(process_tree._WindowsJobTree)
    tree._kernel = SimpleNamespace(ntdll=SimpleNamespace(NtQueryInformationProcess=parent))  # type: ignore[assignment]
    tree._k = kernel32
    tree._root_pid = 1
    tree._lock = threading.Lock()
    tree._children = {}
    tree._exits = []
    tree._closed = False
    tree._job = 10
    monkeypatch.setattr(process_tree.psutil, "Process", Child)

    tree.notify(process_tree._MSG_NEW_PROCESS, 2)
    tree.notify(process_tree._MSG_EXIT_PROCESS, 2)
    facts = tree.facts()
    (failure,) = facts.nonzero_exits
    assert (failure.name, failure.exit_code, failure.invocation) == (name, 5, invocation)
    described_name, _, described_code = failure.describe().partition(" exited with code ")
    assert (described_name, described_code.split(" ", 1)[0]) == (name, "5")
    assert "SECRET" not in failure.describe()
    if invocation:
        assert invocation in failure.describe()
    assert tree.facts() == facts
    assert tree.exit_count() == 1
    assert reads == ([] if name == "curl.exe" else [2])
    assert closed == [2]
