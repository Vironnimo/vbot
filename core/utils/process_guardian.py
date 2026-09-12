"""POSIX child wrapper that ties one process group to the vBot server lifetime."""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress

_POLL_INTERVAL_SECONDS = 0.2
_READ_SIZE_BYTES = 1
_HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)


def run_guardian(lifetime_fd: int, argv: Sequence[str]) -> int:
    """Run the exact child and kill its process group when the server pipe closes."""

    if os.name == "nt":
        raise RuntimeError("The process guardian is only available on POSIX")
    if lifetime_fd < 0 or not argv:
        raise ValueError("A valid lifetime descriptor and child argv are required")
    child = subprocess.Popen(list(argv), close_fds=True)
    try:
        while True:
            return_code = child.poll()
            if return_code is not None:
                return int(return_code)
            readable, _, _ = select.select([lifetime_fd], [], [], _POLL_INTERVAL_SECONDS)
            if not readable:
                continue
            if os.read(lifetime_fd, _READ_SIZE_BYTES):
                continue
            kill_process_group = os.killpg  # type: ignore[attr-defined]
            get_process_group = os.getpgrp  # type: ignore[attr-defined]
            kill_process_group(get_process_group(), _HARD_KILL_SIGNAL)
    finally:
        with suppress(OSError):
            os.close(lifetime_fd)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the private guardian protocol and return the child exit code."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lifetime-fd", type=int, required=True)
    parser.add_argument("child", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    child = list(arguments.child)
    if child and child[0] == "--":
        child.pop(0)
    return run_guardian(arguments.lifetime_fd, child)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
