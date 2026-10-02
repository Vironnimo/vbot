"""POSIX child wrapper: takes a PTY child's controlling terminal and ties its process
group to the vBot server lifetime."""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from types import FrameType

_POLL_INTERVAL_SECONDS = 0.2
_READ_SIZE_BYTES = 1
_HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
# Python ignores these at startup; a program it execs would inherit that.
_PYTHON_IGNORED_SIGNALS = ("SIGPIPE", "SIGXFZ", "SIGXFSZ")


def run_guardian(
    lifetime_fd: int | None, argv: Sequence[str], *, controlling_terminal: bool = False
) -> int:
    """Run the exact child, killing its process group when the server pipe closes.

    The launcher starts the guardian as a session leader. With
    *controlling_terminal* the guardian first makes the PTY on its stdin the
    session's controlling terminal, which the child then shares; doing it here,
    after exec, keeps Python code out of the launcher's forked child. Without a
    lifetime descriptor there is nothing to watch, and the guardian replaces
    itself with the child.
    """

    if sys.platform == "win32":
        raise RuntimeError("The process guardian is only available on POSIX")
    if not argv or (lifetime_fd is not None and lifetime_fd < 0):
        raise ValueError("A valid lifetime descriptor and child argv are required")
    if controlling_terminal:
        import fcntl
        import termios

        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    if lifetime_fd is None:
        for name in _PYTHON_IGNORED_SIGNALS:
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_DFL)
        os.execvp(argv[0], list(argv))
    if controlling_terminal:
        # Ctrl-C and Ctrl-\ signal the terminal's foreground process group, this
        # guardian included, but they are meant for the child. A handler rather
        # than SIG_IGN, because exec resets handlers and the child must start
        # with the default action.
        for terminal_signal in (signal.SIGINT, signal.SIGQUIT):
            signal.signal(terminal_signal, _leave_to_child)
    child = subprocess.Popen(list(argv), close_fds=True)
    try:
        while True:
            return_code = child.poll()
            if return_code is not None:
                # A child killed by signal N exits as a shell reports it: 128 + N.
                return return_code if return_code >= 0 else 128 - return_code
            readable, _, _ = select.select([lifetime_fd], [], [], _POLL_INTERVAL_SECONDS)
            if not readable:
                continue
            if os.read(lifetime_fd, _READ_SIZE_BYTES):
                continue
            os.killpg(os.getpgrp(), _HARD_KILL_SIGNAL)
    finally:
        with suppress(OSError):
            os.close(lifetime_fd)


def _leave_to_child(_signal_number: int, _frame: FrameType | None) -> None:
    """Ignore a terminal signal the child receives for itself."""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the private guardian protocol and return the child exit code."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lifetime-fd", type=int)
    parser.add_argument("--controlling-terminal", action="store_true")
    parser.add_argument("child", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    child = list(arguments.child)
    if child and child[0] == "--":
        child.pop(0)
    return run_guardian(
        arguments.lifetime_fd, child, controlling_terminal=arguments.controlling_terminal
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
