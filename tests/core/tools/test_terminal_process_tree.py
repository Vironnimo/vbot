"""A command's process tree: survivors, child exit codes, CPU time and the kill."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from core.tools._terminal_process_tree import track_process_tree

# The root runs a child that fails, starts a survivor it does not wait for, and
# exits 0 once it has read a line - after the tracker is in place. The survivor
# works briefly before it sleeps: on POSIX only running members count towards
# the CPU time, which the kernel charges in clock ticks.
_SURVIVOR = """
import time
start = time.process_time()
while time.process_time() - start < 0.05:
    pass
time.sleep(60)
"""
_ROOT = f"""
import subprocess, sys
sys.stdin.readline()
subprocess.run([sys.executable, "-c", "raise SystemExit(5)"])
subprocess.Popen([sys.executable, "-c", {_SURVIVOR!r}])
"""


def test_tree_reports_survivors_and_failed_children_and_kills_every_member() -> None:
    # Real processes: the contract is the operating system's (about 0.5 s).
    root = subprocess.Popen(
        [sys.executable, "-c", _ROOT],
        stdin=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )
    tree = track_process_tree(root.pid)
    try:
        assert root.stdin is not None
        root.stdin.write("go\n")
        root.stdin.close()
        assert root.wait(timeout=20) == 0

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            facts = tree.facts()
            if facts.running and facts.cpu_seconds > 0:
                break
            time.sleep(0.05)
        assert len(facts.running) == 1
        assert facts.running[0].name.lower().startswith("python")
        assert facts.cpu_seconds > 0
        assert facts.started >= 1
        if os.name == "nt":
            # Only Windows job notifications carry the exit codes of children.
            assert [exit.describe() for exit in facts.nonzero_exits] == [
                f"{os.path.basename(sys.executable)} exited with code 5"
            ]

        tree.terminate()
        assert tree.facts().running == ()
    finally:
        tree.close()
        if root.poll() is None:
            root.kill()
