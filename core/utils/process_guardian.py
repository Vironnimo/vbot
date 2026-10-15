"""POSIX child wrapper: controlling terminal and server-lifetime containment.

On Linux a subreaper retains descendants across setsid and double-fork, reports
the root's exit independently, and confirms a stop only after reaping the tree.

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
    status_fd: int | None = None,
) -> int:
    """Run the exact child and contain its lifetime while the server pipe is open.

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
    # Ctrl-C and Ctrl-\ in a terminal, and a command's stop, signal the whole
    # process group, this guardian included, but they are meant for the child. A
    # handler rather than SIG_IGN, because exec resets handlers and the child
    # must start with the default action.
    for terminal_signal in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(terminal_signal, _leave_to_child)
    if sys.platform == "linux":
        return _run_subreaper(lifetime_fd, argv, status_fd=status_fd)
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


def _run_subreaper(lifetime_fd: int, argv: Sequence[str], *, status_fd: int | None) -> int:
    """Keep every descendant until reaped, even after double-fork and setsid.

    Killing direct children repeatedly is sufficient: their descendants become
    our children before waitpid can report ECHILD. No process enumeration race
    can report a successful stop while an escaped descendant remains alive.
    The shell's exit travels separately; quiet children may outlive its result.
    """
    if sys.platform != "linux":
        raise RuntimeError("Subreaper containment requires Linux")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    wake_read, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    signal.set_wakeup_fd(wake_write)
    stopping = False

    def stop(_number: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGCHLD, _leave_to_child)
    signal.signal(signal.SIGTERM, stop)
    # Closing a PTY master sends SIGHUP to its session leader. Treat it as a
    # tree stop instead of losing the subreaper while detached children live.
    signal.signal(signal.SIGHUP, stop)
    # Open before starting user code: a missing/inaccessible procfs must fail
    # the launch rather than leave a tree whose children we cannot enumerate.
    with open(f"/proc/self/task/{os.getpid()}/children", encoding="ascii") as children_file:
        if status_fd is not None:
            os.set_inheritable(status_fd, False)
        root = _spawn(argv, lifetime_fd)
        # Only the command and its children own its streams. Holding our copies
        # would manufacture an open output pipe after the shell has exited.
        for descriptor in (0, 1, 2):
            with suppress(OSError):
                os.close(descriptor)
        watched = select.poll()
        watched.register(wake_read, select.POLLIN)
        watched.register(lifetime_fd, select.POLLIN)
        result = 128 + signal.SIGTERM
        try:
            while True:
                while True:
                    try:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        return result
                    if pid == 0:
                        break
                    if pid == root:
                        code = os.waitstatus_to_exitcode(status)
                        result = code if code >= 0 else 128 - code
                        if status_fd is not None:
                            with suppress(BrokenPipeError):
                                os.write(status_fd, f"{result}\n".encode("ascii"))
                            os.close(status_fd)
                            status_fd = None
                if stopping:
                    # We are single-threaded and do not reap between this read and
                    # kill: an exited child's PID remains ours as a zombie, so it
                    # cannot be reused for an unrelated process.
                    children_file.seek(0)
                    pids = children_file.read().split()
                    for pid_text in pids:
                        # Retain custody when a child cannot currently be killed;
                        # the owner reports an unconfirmed stop, never success.
                        with suppress(ProcessLookupError, PermissionError):
                            os.kill(int(pid_text), signal.SIGKILL)
                for descriptor, _events in watched.poll(100 if stopping else None):
                    if descriptor == lifetime_fd:
                        if not os.read(lifetime_fd, _READ_SIZE_BYTES):
                            stopping = True
                            watched.unregister(lifetime_fd)
                    else:
                        with suppress(BlockingIOError):
                            while os.read(wake_read, 4096):
                                pass
        finally:
            signal.set_wakeup_fd(-1)
            for remaining in (wake_read, wake_write, lifetime_fd, status_fd):
                if remaining is not None:
                    with suppress(OSError):
                        os.close(remaining)


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

    The non-Linux fallback reaps the child on a thread, then closes the write
    end of a pipe, which makes its read end readable. Linux uses the subreaper's
    SIGCHLD wakeup instead, including on kernels without pidfd support.
    """

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
    [--lifetime-fd N] [--status-fd N] [--lc-ctype=VALUE] -- CHILD...``.
    """

    arguments = list(argv)
    lifetime_fd: int | None = None
    lc_ctype: str | None = None
    controlling_terminal = False
    status_fd: int | None = None
    while arguments:
        option = arguments.pop(0)
        if option == "--":
            break
        if option == "--controlling-terminal":
            controlling_terminal = True
        elif option == "--lifetime-fd" and arguments:
            lifetime_fd = int(arguments.pop(0))
        elif option == "--status-fd" and arguments:
            status_fd = int(arguments.pop(0))
        elif option.startswith("--lc-ctype="):
            lc_ctype = option.partition("=")[2]
        else:
            raise SystemExit(f"process_guardian: unexpected argument {option!r}")
    return run_guardian(
        lifetime_fd,
        arguments,
        lc_ctype=lc_ctype,
        controlling_terminal=controlling_terminal,
        status_fd=status_fd,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
