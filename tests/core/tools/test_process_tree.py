"""A command's process tree: survivors, child exit codes, CPU time and the kill."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator

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
        assert [failure.describe() for failure in facts.nonzero_exits] == [
            f"{os.path.basename(sys.executable)} exited with code 5"
        ]
