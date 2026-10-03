"""POSIX child wrapper: takes a PTY child's controlling terminal and ties its process
group to the vBot server lifetime.

Standard library only: ``core.utils.processes.guarded_process_launch`` runs this
file by path in isolated mode, in the child's working directory and environment,
where no vBot package is importable. Every Bash command starts it, so it imports
only what it needs: ``argparse`` and ``subprocess`` would add about 8 ms of the
roughly 18 ms it takes to start.
"""

from __future__ import annotations

import os
import select
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress
from types import FrameType

_READ_SIZE_BYTES = 1
_HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
# Python ignores these at startup; a program it execs would inherit that.
_PYTHON_IGNORED_SIGNALS = ("SIGPIPE", "SIGXFZ", "SIGXFSZ")
# Wait statuses the reaper thread collected, by child pid (no pidfd only).
_reaped: dict[int, int] = {}


def run_guardian(
    lifetime_fd: int | None,
    argv: Sequence[str],
    *,
    lc_ctype: str | None,
    controlling_terminal: bool = False,
) -> int:
    """Run the exact child, killing its process group when the server pipe closes.

    The launcher starts the guardian as a session leader. With
    *controlling_terminal* the guardian first makes the PTY on its stdin the
    session's controlling terminal, which the child then shares; doing it here,
    after exec, keeps Python code out of the launcher's forked child. Without a
    lifetime descriptor there is nothing to watch, and the guardian replaces
    itself with the child.

    The child inherits this process's environment. *lc_ctype* is the child's
    requested ``LC_CTYPE``, ``None`` when it has none: the interpreter's locale
    coercion (PEP 538) sets ``LC_CTYPE`` at startup when the environment names
    no UTF-8 locale, and isolated mode cannot turn that off.
    """

    if sys.platform == "win32":
        raise RuntimeError("The process guardian is only available on POSIX")
    if not argv or (lifetime_fd is not None and lifetime_fd < 0):
        raise ValueError("A valid lifetime descriptor and child argv are required")
    if lc_ctype is None:
        os.environ.pop("LC_CTYPE", None)
    else:
        os.environ["LC_CTYPE"] = lc_ctype
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
    child_pid = _spawn(argv, lifetime_fd)
    exit_fd = _child_exit_descriptor(child_pid)
    try:
        # Sleep without a timeout until the child exits or the server pipe closes,
        # so the child's exit reaches the caller the moment it happens.
        watched = select.poll()
        watched.register(exit_fd, select.POLLIN)
        watched.register(lifetime_fd, select.POLLIN)
        while True:
            ready = {descriptor for descriptor, _events in watched.poll()}
            if exit_fd in ready:
                return _exit_code(child_pid)
            if lifetime_fd in ready and not os.read(lifetime_fd, _READ_SIZE_BYTES):
                os.killpg(os.getpgrp(), _HARD_KILL_SIGNAL)
    finally:
        for descriptor in (exit_fd, lifetime_fd):
            with suppress(OSError):
                os.close(descriptor)


def _spawn(argv: Sequence[str], lifetime_fd: int) -> int:
    """Start the child with the default disposition of every signal Python ignores.

    The child inherits the standard streams and this process's environment, but
    not the server pipe: holding it would keep the pipe open after the server.
    """

    if sys.platform == "win32":
        raise RuntimeError("The process guardian is only available on POSIX")
    os.set_inheritable(lifetime_fd, False)
    ignored = {getattr(signal, name) for name in _PYTHON_IGNORED_SIGNALS if hasattr(signal, name)}
    return os.posix_spawnp(argv[0], list(argv), os.environ, setsigdef=ignored)


def _exit_code(child_pid: int) -> int:
    """Reap the exited child; one killed by signal N exits as a shell reports it: 128 + N."""

    status = _reaped.pop(child_pid, None)
    if status is None:
        _pid, status = os.waitpid(child_pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def _child_exit_descriptor(child_pid: int) -> int:
    """Return a descriptor that becomes readable once the child has exited.

    A pidfd on Linux 5.3 and later. Where the kernel lacks ``pidfd_open``, a
    seccomp filter denies it, or on another system, a thread reaps the child and
    then closes the write end of a pipe, which makes its read end readable.
    """

    if sys.platform == "linux":
        with suppress(OSError):
            return os.pidfd_open(child_pid)
    import threading

    read_fd, write_fd = os.pipe()

    def reap() -> None:
        try:
            _pid, _reaped[child_pid] = os.waitpid(child_pid, 0)
        finally:
            os.close(write_fd)

    threading.Thread(target=reap, name="child-exit", daemon=True).start()
    return read_fd


def _leave_to_child(_signal_number: int, _frame: FrameType | None) -> None:
    """Ignore a terminal signal the child receives for itself."""


def main(argv: Sequence[str]) -> int:
    """Run the private guardian protocol and return the child exit code.

    ``guarded_process_launch`` writes it: ``[--controlling-terminal]
    [--lifetime-fd N] [--lc-ctype=VALUE] -- CHILD...``.
    """

    arguments = list(argv)
    lifetime_fd: int | None = None
    lc_ctype: str | None = None
    controlling_terminal = False
    while arguments:
        option = arguments.pop(0)
        if option == "--":
            break
        if option == "--controlling-terminal":
            controlling_terminal = True
        elif option == "--lifetime-fd" and arguments:
            lifetime_fd = int(arguments.pop(0))
        elif option.startswith("--lc-ctype="):
            lc_ctype = option.partition("=")[2]
        else:
            raise SystemExit(f"process_guardian: unexpected argument {option!r}")
    return run_guardian(
        lifetime_fd, arguments, lc_ctype=lc_ctype, controlling_terminal=controlling_terminal
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
