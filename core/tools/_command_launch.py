"""Start a command process on pipes, contained in its process tree from creation on.

A command never gets input and never has a terminal: stdout and stderr share one
pipe (their order is kept), no console or PTY is attached, and stdin is empty: a
program that asks for input reads EOF at once, and ``isatty()`` is false for all
three. POSIX gives it the null device. Windows gives it a pipe without a writer:
the null device is a character device there, which programs take for a console.

Containment leaves no moment in which the command could start a process that
escapes:

- Windows: the shell is created suspended, windowless and in a new process group
  (``CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP``). The
  tracker assigns it to a kill-on-close Job Object before its first instruction,
  and only then is its main thread resumed; every process it creates inherits
  the Job. A windowless console still has a console host (``conhost.exe``) in
  the Job, which the command's end detection ignores.
- POSIX: the shell (or the server-lifetime guardian that runs it) starts a new
  session before it executes anything, so everything it starts belongs to that
  session and process group unless it explicitly starts its own.

A process that cannot be tracked is killed and not started.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import psutil  # type: ignore[import-untyped]

from core.utils.processes import (
    create_subprocess_exec,
    guarded_process_launch,
    subprocess_creation_flags,
    wait_for_exit,
)

from .process_tree import ProcessTree, ProcessTreeTracker

_CREATE_SUSPENDED = 0x00000004
_READ_BYTES = 65_536


class PipeCommandProcess:
    """A command process started by ``launch_pipe_process``."""

    def __init__(self, process: asyncio.subprocess.Process, tree: ProcessTree) -> None:
        self._process = process
        self._tree = tree

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def tree(self) -> ProcessTree:
        return self._tree

    async def read(self) -> bytes:
        stdout = self._process.stdout
        if stdout is None:
            return b""
        return await stdout.read(_READ_BYTES)

    async def wait(self) -> int:
        return await wait_for_exit(self._process)

    def interrupt(self) -> bool:
        if sys.platform == "win32":
            # A windowless process shares no console with vBot, so no console
            # control event reaches it; the stop kills the Job instead.
            return False
        try:
            os.killpg(self._process.pid, signal.SIGINT)
        except ProcessLookupError, PermissionError:
            return False
        return True

    def close_output(self) -> None:
        transport = cast(Any, self._process)._transport
        pipe = transport.get_pipe_transport(1) if transport is not None else None
        if pipe is not None:
            # Windows may hold one already-read chunk in the paused transport,
            # outside the StreamReader; resuming schedules its delivery before
            # the close schedules EOF.
            pipe.resume_reading()
            pipe.close()

    def close(self) -> None:
        transport = cast(Any, self._process)._transport
        if transport is not None:
            # The process has exited; closing only releases the transport.
            transport.close()


async def launch_pipe_process(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    tracker: ProcessTreeTracker,
) -> PipeCommandProcess:
    """Start *argv* in *cwd* with exactly *env*, on pipes, tracked by *tracker* from creation.

    Raises OSError (or the tracker's error) when the process cannot be started
    or tracked; nothing then keeps running.
    """
    trees: list[ProcessTree] = []

    def created(pid: int) -> None:
        tree = tracker(pid)
        try:
            if sys.platform == "win32":
                psutil.Process(pid).resume()
        except BaseException:
            with contextlib.suppress(OSError):
                tree.terminate()
            tree.close()
            raise
        trees.append(tree)

    try:
        if sys.platform == "win32":
            stdin, writer = os.pipe()
            os.close(writer)
            try:
                process = await create_subprocess_exec(
                    *argv,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=cwd,
                    env=dict(env),
                    creationflags=subprocess_creation_flags(new_process_group=True)
                    | _CREATE_SUSPENDED,
                    on_created=created,
                )
            finally:
                # The process holds its own copy of the reading end.
                os.close(stdin)
        else:
            launch = guarded_process_launch(argv, env=env)
            process = await create_subprocess_exec(
                *launch.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=dict(env),
                start_new_session=True,
                pass_fds=launch.pass_fds,
                on_created=created,
            )
    except BaseException:
        for tree in trees:
            with contextlib.suppress(OSError):
                tree.terminate()
            tree.close()
        raise
    return PipeCommandProcess(process, trees[0])


__all__ = ["PipeCommandProcess", "launch_pipe_process"]
