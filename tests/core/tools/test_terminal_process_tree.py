"""A command's process tree: survivors, child exit codes, CPU time and the kill."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator

import pytest

from core.tools._terminal_process_tree import ProcessTree, ProcessTreeFacts, track_process_tree

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


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows reports child exit codes")
def test_tree_records_the_exit_code_of_a_failed_direct_child() -> None:
    with _tracked_root(_ROOT_WITH_A_FAILED_CHILD) as (root, tree):
        _start_children(root)
        facts = _first_facts(tree, lambda facts: bool(facts.nonzero_exits))
        assert [failure.describe() for failure in facts.nonzero_exits] == [
            f"{os.path.basename(sys.executable)} exited with code 5"
        ]
