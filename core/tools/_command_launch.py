"""Start a command process on pipes, contained in its process tree from creation on.

A command never gets input and never has a terminal: stdout and stderr share one
pipe (their order is kept), no PTY is attached, and stdin is empty by the time
the command runs: a program that asks for input reads EOF at once, and
``isatty()`` is false for all three. POSIX gives it the null device, and its new
session has no controlling terminal. Windows gives it a pipe: the null device is
a character device there, which programs take for a console. A launch with a
request writes the request into that pipe and closes it; the shell reads the
request up to the end of its input before it runs the command (``_powershell``).
Without a request the pipe has no writer. A windowless process on Windows still
has a hidden console of its own, whose input nothing writes: a program that
reads that console directly instead of stdin, such as Python's ``getpass``,
waits until it is stopped.

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

Warm shell (Windows). Starting PowerShell takes about a quarter of a second, and
it reads its command only from the request. ``PipeLauncher`` therefore keeps one
shell started ahead of time, created and contained exactly like a command's,
in a directory no user deletes, waiting for its request. A launch whose
program, arguments, environment and tracker equal the warm shell's claims it
once its process exists: the request goes to that shell, and its process tree
becomes the command's. Any other launch, and every launch while the warm shell
is claimed or still being created, starts its own shell. A shell serves one
command and exits with it; nothing of one command reaches the next. After every
launch with a request the launcher starts the next warm shell for the same
program and environment unless one exists, so the first call of a server starts
cold, and a burst of parallel calls starts no more shells at once than it has
calls. A warm shell nobody claims for ``WARM_SHELL_IDLE_SECONDS`` ends, and so
does one whose launch no longer matches. One that exited or could not start is
passed over: the launch starts its own shell, and a warning is logged once per
kind of failure. The warm shell is never a command: no list, count or limit of
commands includes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol, cast

import psutil  # type: ignore[import-untyped]

from core.utils.logging import get_logger
from core.utils.processes import (
    create_subprocess_exec,
    guarded_process_launch,
    subprocess_creation_flags,
    wait_for_exit,
)

from ._command_types import CommandProcess
from .process_tree import ProcessTree, ProcessTreeTracker

_LOGGER = get_logger("tools.command_launch")

# A warm shell no launch claimed for this long ends.
WARM_SHELL_IDLE_SECONDS = 10 * 60.0

_CREATE_SUSPENDED = 0x00000004
_READ_BYTES = 65_536


class PipeCommandProcess:
    """A command process started on pipes; with a request, its stdin is still open."""

    def __init__(
        self, process: asyncio.subprocess.Process, tree: ProcessTree, writer: int | None = None
    ) -> None:
        self._process = process
        self._tree = tree
        self._writer = writer

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
        self._close_writer()
        transport = cast(Any, self._process)._transport
        if transport is not None:
            # The process has exited; closing only releases the transport.
            transport.close()

    # The launcher's side

    def running(self) -> bool:
        """Whether the process has not exited, as far as the Event Loop has seen."""
        return self._process.returncode is None

    def send(self, request: bytes) -> None:
        """Write *request* to stdin and close it, on a thread of its own.

        The write waits while the pipe is full, until the shell reads or exits;
        a shell that exited loses the request and reports its own exit.
        """
        writer, self._writer = self._writer, None
        if writer is None:
            raise RuntimeError("This process takes no request")
        threading.Thread(
            target=_write_and_close, args=(writer, request), name="command-request", daemon=True
        ).start()

    def kill(self) -> None:
        """End the process tree now and release it: a shell no command claimed."""
        self._close_writer()
        with contextlib.suppress(OSError):
            # The kill-on-close Job ends every process in it.
            self._tree.close()
        with contextlib.suppress(OSError, RuntimeError):
            # Kills the root where the tree cannot (POSIX) and closes the pipes.
            transport = cast(Any, self._process)._transport
            if transport is not None:
                transport.close()

    def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            with contextlib.suppress(OSError):
                os.close(writer)


def _write_and_close(writer: int, request: bytes) -> None:
    try:
        view = memoryview(request)
        while view:
            view = view[os.write(writer, view) :]
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(writer)


async def launch_pipe_process(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    tracker: ProcessTreeTracker,
    *,
    request_pipe: bool = False,
) -> PipeCommandProcess:
    """Start *argv* in *cwd* with exactly *env*, on pipes, tracked by *tracker* from creation.

    With *request_pipe*, stdin is a pipe whose writing end the process keeps
    for ``send``; otherwise stdin is empty. Raises OSError (or the tracker's
    error) when the process cannot be started or tracked; nothing then keeps
    running.
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

    writer: int | None = None
    try:
        if sys.platform == "win32" or request_pipe:
            stdin, writer = os.pipe()
            if not request_pipe:
                os.close(writer)
                writer = None
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
        if writer is not None:
            os.close(writer)
        for tree in trees:
            with contextlib.suppress(OSError):
                tree.terminate()
            tree.close()
        raise
    return PipeCommandProcess(process, trees[0], writer)


class _Shell(CommandProcess, Protocol):
    """A process ``PipeLauncher`` started (``PipeCommandProcess``)."""

    def running(self) -> bool: ...

    def send(self, request: bytes) -> None: ...

    def kill(self) -> None: ...


# Starts a process on pipes, with a request pipe when asked: ``launch_pipe_process``.
ShellStart = Callable[..., Awaitable[_Shell]]


@dataclass(eq=False)
class _WarmShell:
    argv: tuple[str, ...]
    env: dict[str, str]
    tracker: ProcessTreeTracker
    task: asyncio.Future[_Shell]
    expiry: asyncio.Future[None] | None = field(default=None)

    def serves(self, argv: Sequence[str], env: Mapping[str, str], tracker: object) -> bool:
        return self.argv == tuple(argv) and self.env == env and self.tracker == tracker


def _neutral_directory() -> Path:
    """Where a warm shell waits: a directory that always exists and no user deletes.

    Windows' system directory; nobody can plant a program or library there that
    the shell would load from its working directory.
    """
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        return Path(root) / "System32"
    return Path("/")


class PipeLauncher:
    """Start command processes on pipes; on Windows keep one warm shell ready.

    ``CommandManager``'s launcher (``CommandLauncher``). *warm* turns the warm
    shell on (by default on Windows); *start* starts a process
    (``launch_pipe_process``) and *sleep* measures the idle time; both are seams
    for tests.
    """

    def __init__(
        self,
        *,
        warm: bool | None = None,
        start: ShellStart = launch_pipe_process,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        idle_seconds: float = WARM_SHELL_IDLE_SECONDS,
    ) -> None:
        self._warm_enabled = sys.platform == "win32" if warm is None else warm
        self._start = start
        self._sleep = sleep
        self._idle_seconds = idle_seconds
        self._warm: _WarmShell | None = None
        self._warned: set[str] = set()
        self._closed = False

    async def launch(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        tracker: ProcessTreeTracker,
        request: bytes | None = None,
    ) -> CommandProcess:
        """Start *argv* in *cwd* with exactly *env*, tracked by *tracker* from creation.

        *request*, when given, is written to the process's stdin, which then
        ends. The process can be the warm shell, started earlier with the same
        *argv*, *env* and *tracker* in another directory: the request carries
        everything the call adds to the shell, the workdir included. Raises
        OSError (or the tracker's error) when the process cannot be started or
        tracked; nothing then keeps running.
        """
        if request is None:
            return await self._start(argv, cwd, env, tracker)
        shell = self._claim(argv, env, tracker) if self._warm_enabled else None
        if shell is None:
            shell = await self._start(argv, cwd, env, tracker, request_pipe=True)
        shell.send(request)
        if self._warm_enabled:
            self._prepare(argv, env, tracker)
        return shell

    async def aclose(self) -> None:
        """End the warm shell without delay; no launch starts another one."""
        self._closed = True
        warm, self._warm = self._warm, None
        if warm is None:
            return
        self._end(warm)
        waits = [warm.task] if warm.expiry is None else [warm.task, warm.expiry]
        await asyncio.gather(*waits, return_exceptions=True)

    def close(self) -> None:
        """``aclose`` without an Event Loop: a warm shell still starting ends with its start."""
        self._closed = True
        warm, self._warm = self._warm, None
        if warm is not None:
            self._end(warm)

    def _claim(
        self, argv: Sequence[str], env: Mapping[str, str], tracker: ProcessTreeTracker
    ) -> _Shell | None:
        warm = self._warm
        if warm is None:
            return None
        if not warm.serves(argv, env, tracker):
            self._warm = None
            self._end(warm)
            return None
        if not warm.task.done():
            # Still being created: its start is no further than a new one, and
            # claiming it would start the next warm shell beside the launches
            # of a burst, slowing each of them. It stays for a later launch.
            return None
        self._warm = None
        if warm.expiry is not None:
            warm.expiry.cancel()
        if warm.task.cancelled() or warm.task.exception() is not None:
            # Its start failed; ``_started`` logged why.
            return None
        shell = warm.task.result()
        if not shell.running():
            self._warn("exited", "The warm shell %s exited before a command claimed it", shell.pid)
            shell.kill()
            return None
        return shell

    def _prepare(
        self, argv: Sequence[str], env: Mapping[str, str], tracker: ProcessTreeTracker
    ) -> None:
        """Start the next warm shell in the background, unless one is ready or starting."""
        if self._closed or self._warm is not None:
            return
        task = asyncio.ensure_future(
            self._start(list(argv), _neutral_directory(), dict(env), tracker, request_pipe=True)
        )
        warm = _WarmShell(tuple(argv), dict(env), tracker, task)
        self._warm = warm
        task.add_done_callback(partial(self._started, warm))
        warm.expiry = asyncio.ensure_future(self._expire(warm))

    def _started(self, warm: _WarmShell, task: asyncio.Future[_Shell]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        self._warn("start", "A warm shell could not start: %s", task.exception())
        if self._warm is warm:
            self._warm = None
            if warm.expiry is not None:
                warm.expiry.cancel()

    async def _expire(self, warm: _WarmShell) -> None:
        await self._sleep(self._idle_seconds)
        if self._warm is warm:
            self._warm = None
            self._end(warm)

    def _end(self, warm: _WarmShell) -> None:
        # A closed Event Loop refuses the cancellations; its tasks never run again.
        if warm.expiry is not None:
            with contextlib.suppress(RuntimeError):
                warm.expiry.cancel()
        if not warm.task.done():
            # A cancelled start ends the process it created.
            with contextlib.suppress(RuntimeError):
                warm.task.cancel()
            return
        if not warm.task.cancelled() and warm.task.exception() is None:
            warm.task.result().kill()

    def _warn(self, kind: str, message: str, *args: object) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            _LOGGER.warning(message + "; commands start their own shell", *args)


__all__ = [
    "WARM_SHELL_IDLE_SECONDS",
    "PipeCommandProcess",
    "PipeLauncher",
    "launch_pipe_process",
]
