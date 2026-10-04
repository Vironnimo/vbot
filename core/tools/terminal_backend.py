"""Private PTY/ConPTY transport, Windows command lines, and process-tree control."""

from __future__ import annotations

import codecs
import contextlib
import errno
import functools
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from core.utils.processes import guarded_process_launch, kill_process_tree

_WINDOWS_INTERACTIVE_SHELLS = ("pwsh.exe", "powershell.exe")
TERMINAL_READ_TIMEOUT_SECONDS = 0.2
# A POSIX child's exit status follows the end of its output by moments; closing
# the PTY gives its session this long to end on the hangup before it is killed.
_EXIT_STATUS_GRACE_SECONDS = 0.5
_CLOSE_GRACE_SECONDS = 0.1
# How long a Windows program's output must pause after its exit before a read
# reports the exit; ConPTY delivered all output before the exit in measurements.
_EXIT_OUTPUT_QUIET_SECONDS = 0.02
_CLOSE_READER_SECONDS = 2.0
# cmd.exe metacharacters, escaped with ^ in a typed command line.
_CMD_META = re.compile(r'([()%!^"<>&|])')
# Characters cmd acts on outside quotes when it parses a line again.
_CMD_ACTIVE = re.compile(r"[()^<>&|]")


class TerminalAdapter(Protocol):
    """Blocking terminal-process adapter used through worker threads."""

    @property
    def pid(self) -> int: ...

    def read(self, size: int) -> str: ...

    def write(self, text: str) -> None: ...

    def resize(self, rows: int, columns: int) -> None: ...

    def is_alive(self) -> bool: ...

    def exit_code(self) -> int | None: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class TerminalAdapterFactory(Protocol):
    """Start a terminal process.

    *command_line* is, on Windows, the exact command line after the
    executable, for a program that does not parse its arguments with the C
    runtime rules (cmd.exe); *argv* then names only the executable.
    """

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        rows: int,
        columns: int,
        *,
        command_line: str | None = None,
    ) -> TerminalAdapter: ...


def default_terminal_argv(env: Mapping[str, str] | None = None) -> list[str]:
    """Return the host user's default interactive shell command."""
    environment = os.environ if env is None else env
    login_shell = None if os.name == "nt" else _posix_login_shell()
    return _select_default_terminal_argv(
        os.name,
        environment,
        executable_lookup=shutil.which,
        posix_login_shell=login_shell,
    )


def _select_default_terminal_argv(
    platform_name: str,
    environment: Mapping[str, str],
    *,
    executable_lookup: Callable[..., str | None],
    posix_login_shell: str | None,
) -> list[str]:
    if platform_name == "nt":
        search_path = environment.get("PATH", "")
        for command in _WINDOWS_INTERACTIVE_SHELLS:
            if executable_lookup(command, path=search_path) is not None:
                return [command]
        return [environment.get("COMSPEC") or "cmd.exe"]

    environment_shell = environment.get("SHELL")
    return [environment_shell or posix_login_shell or "/bin/sh"]


def _posix_login_shell() -> str | None:
    try:
        import pwd

        get_user_id = getattr(os, "getuid", None)
        get_password_entry = getattr(pwd, "getpwuid", None)
        if not callable(get_user_id) or not callable(get_password_entry):
            return None
        shell = getattr(get_password_entry(get_user_id()), "pw_shell", None)
    except ImportError, KeyError, OSError:
        return None
    return str(shell) if shell else None


class _WindowsTerminalAdapter:
    """A ConPTY and its program, read by a dedicated thread.

    pywinpty's ``PtyProcess`` relays output through a socket thread that
    sleeps a millisecond after every read, about one line per 1.5 ms, so
    large output reached the reader seconds after the program ended. ConPTY
    also never ends its output when the program exits. This adapter reads
    the ConPTY directly and wakes a waiting read once the program has exited
    and its output paused, so the reader learns of the exit at once.
    """

    def __init__(self, pty: Any) -> None:
        self._pty = pty
        self._pid = int(pty.pid)
        self._condition = threading.Condition()
        self._chunks: deque[str] = deque()
        self._last_output_at = time.monotonic()
        self._reading = True
        self._closed = False
        self._exit_wake = False
        kernel = _windows_kernel()
        self._process_handle = kernel.open_process(self._pid)
        self._close_event = kernel.create_event()
        self._pump = threading.Thread(target=self._read_output, name="vbot-conpty", daemon=True)
        self._pump.start()
        threading.Thread(target=self._watch_exit, name="vbot-conpty-exit", daemon=True).start()

    @property
    def pid(self) -> int:
        return self._pid

    def _read_output(self) -> None:
        try:
            while not self._closed:
                text = self._pty.read(blocking=True)
                if text:
                    with self._condition:
                        self._chunks.append(text)
                        self._last_output_at = time.monotonic()
                        self._condition.notify_all()
                elif self._pty.iseof():
                    break
        except Exception:
            # close() cancels the pending read; a broken pipe ends the output too.
            pass
        finally:
            with self._condition:
                self._reading = False
                self._condition.notify_all()

    def _watch_exit(self) -> None:
        kernel = _windows_kernel()
        try:
            if not kernel.wait_for_either(self._process_handle, self._close_event):
                return
            # ConPTY delivers a program's output before its exit completes
            # (measured); the pause still catches a late final frame.
            with self._condition:
                while not self._closed:
                    quiet = time.monotonic() - self._last_output_at
                    if quiet >= _EXIT_OUTPUT_QUIET_SECONDS:
                        break
                    self._condition.wait(_EXIT_OUTPUT_QUIET_SECONDS - quiet)
                self._exit_wake = True
                self._condition.notify_all()
        finally:
            kernel.close_handle(self._process_handle)
            kernel.close_handle(self._close_event)

    def read(self, size: int) -> str:
        with self._condition:
            if not (self._chunks or self._exit_wake or self._closed or not self._reading):
                self._condition.wait(TERMINAL_READ_TIMEOUT_SECONDS)
            if self._chunks:
                return self._take(size)
            if self._closed or not self._reading:
                raise EOFError
            # After the program's exit, the reader checks it now, not after a timeout.
            self._exit_wake = False
            raise TimeoutError

    def _take(self, size: int) -> str:
        parts: list[str] = []
        remaining = size
        while self._chunks and remaining > 0:
            chunk = self._chunks.popleft()
            if len(chunk) > remaining:
                self._chunks.appendleft(chunk[remaining:])
                chunk = chunk[:remaining]
            parts.append(chunk)
            remaining -= len(chunk)
        return "".join(parts)

    def write(self, text: str) -> None:
        if self._closed or not self._pty.isalive():
            raise EOFError
        self._pty.write(text)

    def resize(self, rows: int, columns: int) -> None:
        self._pty.set_size(columns, rows)

    def is_alive(self) -> bool:
        return bool(self._pty.isalive())

    def exit_code(self) -> int | None:
        value = self._pty.get_exitstatus()
        return int(value) if isinstance(value, int) else None

    def terminate(self) -> None:
        if self._pty.isalive():
            with contextlib.suppress(OSError):
                os.kill(self._pid, signal.SIGTERM)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        _windows_kernel().set_event(self._close_event)
        # A cancel reaches only a read in progress: repeat it until the pump ends.
        deadline = time.monotonic() + _CLOSE_READER_SECONDS
        while self._pump.is_alive() and time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                self._pty.cancel_io()
            self._pump.join(_CLOSE_GRACE_SECONDS / 10)
        # As with pywinpty's close, a program that outlived its tree kill ends here.
        self.terminate()


class _WindowsKernel:
    """The kernel32 calls the ConPTY adapter waits with."""

    _SYNCHRONIZE = 0x00100000
    _INFINITE = 0xFFFFFFFF

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.WaitForMultipleObjects.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._ctypes = ctypes
        self._handle_type = wintypes.HANDLE
        self._kernel32 = kernel32

    def open_process(self, pid: int) -> int:
        # The ConPTY holds the program's own handle, so its pid stays unique.
        handle = self._kernel32.OpenProcess(self._SYNCHRONIZE, False, pid)
        if not handle:
            raise self._ctypes.WinError(self._ctypes.get_last_error())  # type: ignore[attr-defined]
        return int(handle)

    def create_event(self) -> int:
        handle = self._kernel32.CreateEventW(None, True, False, None)
        if not handle:
            raise self._ctypes.WinError(self._ctypes.get_last_error())  # type: ignore[attr-defined]
        return int(handle)

    def set_event(self, handle: int) -> None:
        self._kernel32.SetEvent(handle)

    def wait_for_either(self, process: int, event: int) -> bool:
        """Wait until *process* exits (True) or *event* is set (False)."""
        handles = (self._handle_type * 2)(process, event)
        result = self._kernel32.WaitForMultipleObjects(2, handles, False, self._INFINITE)
        return bool(result == 0)

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)


@functools.cache
def _windows_kernel() -> _WindowsKernel:
    return _WindowsKernel()


class _PosixTerminalAdapter:
    """A PTY master and the session-leading child process that holds its slave."""

    def __init__(self, process: subprocess.Popen[bytes], master_fd: int) -> None:
        self._process = process
        self._fd = master_fd
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lock = threading.Lock()
        self._users = 0
        self._closed = False
        # Closes the master once, on close() or when the adapter is dropped
        # without it, such as a naturally finished terminal leaving the catalog.
        self._release = weakref.finalize(self, os.close, master_fd)
        os.set_blocking(master_fd, False)

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    @contextlib.contextmanager
    def _master(self) -> Iterator[int]:
        """Hold the master open for one operation, so its number cannot be reused meanwhile."""
        with self._lock:
            if self._closed:
                raise EOFError
            self._users += 1
        try:
            yield self._fd
        finally:
            with self._lock:
                self._users -= 1
                release = self._closed and not self._users
            if release:
                self._release()

    def read(self, size: int) -> str:
        with self._master() as fd:
            if not select.select([fd], [], [], TERMINAL_READ_TIMEOUT_SECONDS)[0]:
                raise TimeoutError
            try:
                data = os.read(fd, size)
            except BlockingIOError:
                raise TimeoutError from None
        if not data:
            raise EOFError
        return self._decoder.decode(data, final=False)

    def write(self, text: str) -> None:
        remaining = memoryview(text.encode("utf-8"))
        with self._master() as fd:
            while remaining:
                if self._closed:
                    raise EOFError
                try:
                    written = os.write(fd, remaining)
                except BlockingIOError:
                    select.select([], [fd], [], TERMINAL_READ_TIMEOUT_SECONDS)
                    continue
                if not written:
                    raise EOFError
                remaining = remaining[written:]

    def resize(self, rows: int, columns: int) -> None:
        with self._master() as fd:
            _set_window_size(fd, rows, columns)

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def exit_code(self) -> int | None:
        # Output ends while the kernel still finishes the child's exit.
        try:
            return_code = self._process.wait(timeout=_EXIT_STATUS_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return None
        return return_code if return_code >= 0 else 128 - return_code

    def terminate(self) -> None:
        self._process.kill()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            release = not self._users
        if release:
            self._release()
        # Closing the master hangs up the terminal, which signals its session.
        try:
            self._process.wait(timeout=_CLOSE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=_CLOSE_GRACE_SECONDS)


def _spawn_posix_terminal(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], rows: int, columns: int
) -> _PosixTerminalAdapter:
    """Start *argv* as a session leader on a new PTY, without Python code in the fork.

    A ``preexec_fn`` or ``os.forkpty`` would run Python in the child of this
    multi-threaded server, where a lock another thread held at fork can
    deadlock it. Without one, CPython starts the child in C. The PTY becomes the
    controlling terminal in the guardian, after exec.
    """
    if sys.platform == "win32":
        raise OSError("POSIX terminals are not available on Windows")
    _require_program(argv[0], cwd, env)
    launch = guarded_process_launch(argv, env=env, controlling_terminal=True)
    master_fd, slave_fd = os.openpty()
    try:
        _set_window_size(master_fd, rows, columns)
        process = subprocess.Popen(
            launch.argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
            pass_fds=launch.pass_fds,
        )
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return _PosixTerminalAdapter(process, master_fd)


def _require_program(program: str, cwd: Path, env: Mapping[str, str]) -> None:
    """Report a missing program at launch; the guardian would only find out inside the PTY."""
    if os.path.dirname(program):
        found = shutil.which(os.path.join(cwd, program))
    else:
        found = shutil.which(program, path=env.get("PATH", os.defpath))
    if found is None:
        raise FileNotFoundError(errno.ENOENT, "No executable program found", program)


def _set_window_size(fd: int, rows: int, columns: int) -> None:
    if sys.platform == "win32":
        raise OSError("PTY window sizes exist only on POSIX")
    import fcntl
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def spawn_terminal_adapter(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    rows: int,
    columns: int,
    *,
    command_line: str | None = None,
    platform_name: str = os.name,
) -> TerminalAdapter:
    if platform_name == "nt":
        executable, arguments = _windows_command(argv, env, command_line)
        return _WindowsTerminalAdapter(
            _spawn_windows_process(executable, arguments, cwd, env, rows, columns)
        )
    if command_line is not None:
        raise ValueError("An exact command line exists only on Windows")
    return _spawn_posix_terminal(argv, cwd, env, rows, columns)


def terminate_process_tree(adapter: TerminalAdapter, *, targets: list[Any] | None = None) -> None:
    """Terminate the captured tree, retaining surviving identities for a retry."""
    if not adapter.is_alive() and not targets:
        return
    try:
        kill_process_tree(adapter, targets=targets)
    except ProcessLookupError:
        return


def windows_command_processor_line(command: str, arguments: Sequence[str]) -> str:
    """Render a program and its arguments as a cmd.exe command line.

    The executable keeps its quotes for cmd's command lookup; the argument
    syntax is escaped so cmd hands the native C-runtime command line to the
    program unchanged. Inside ``cmd /s /c "<line>"`` or ``/k`` the line works
    as if it were typed at the prompt.
    """
    return " ".join(
        [
            subprocess.list2cmdline([command]),
            *(_CMD_META.sub(r"^\1", _command_processor_word(value)) for value in arguments),
        ]
    )


def _command_processor_word(value: str) -> str:
    """Return the C runtime quoting of *value*, quoted if cmd could act on it.

    A batch file such as an npm program shim hands ``%*`` to cmd once more,
    which keeps metacharacters literal only inside quotes; ``list2cmdline``
    quotes only around whitespace.
    """
    word = subprocess.list2cmdline([value])
    if word.startswith('"') or not _CMD_ACTIVE.search(value):
        return word
    # Backslashes before the closing quote are doubled to stay literal.
    trailing = len(word) - len(word.rstrip("\\"))
    return f'"{word}{"\\" * trailing}"'


def _windows_command(
    argv: Sequence[str], env: Mapping[str, str], command_line: str | None
) -> tuple[str, str]:
    """Return the executable and the command line after it.

    Batch launchers (``.cmd``/``.bat``, such as npm's program shims) run
    through the command processor, whose own grammar the line follows; other
    programs get the C runtime quoting of their arguments.
    """
    executable = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    if command_line is not None:
        return executable, command_line
    if Path(executable).suffix.lower() not in {".bat", ".cmd"}:
        return executable, subprocess.list2cmdline(argv[1:])
    command_processor = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    line = windows_command_processor_line(executable, argv[1:])
    return (
        shutil.which(command_processor, path=env.get("PATH")) or command_processor,
        f'/d /s /c "{line}"',
    )


def _spawn_windows_process(
    executable: str, arguments: str, cwd: Path, env: Mapping[str, str], rows: int, columns: int
) -> Any:
    """Start *executable* with an exact command line behind ConPTY; return the PTY.

    ``PtyProcess.spawn`` would join an argument list with the C runtime rules,
    which cmd.exe does not use; the line is kept as given.
    """
    from winpty import PTY

    if not os.path.isfile(executable):
        raise FileNotFoundError(errno.ENOENT, "No executable program found", executable)
    _accept_console_interrupts()
    backend = os.environ.get("PYWINPTY_BACKEND")
    pty = PTY(columns, rows, backend=int(backend) if backend is not None else None)
    environment = "\0".join(f"{key}={value}" for key, value in env.items()) + "\0"
    if arguments:
        pty.spawn(executable, cwd=str(cwd), env=environment, cmdline=" " + arguments)
    else:
        pty.spawn(executable, cwd=str(cwd), env=environment)
    return pty


def _accept_console_interrupts() -> None:
    """Let Terminal programs receive Ctrl+C.

    A process started in a new process group, as the managed Server is,
    ignores Ctrl+C, and its children inherit that, ConPTY children included:
    Ctrl+C typed into a Terminal would then not stop a console program. The
    managed Server's own console is hidden, so it clears the inherited
    setting for itself and its children; a Server run in the foreground
    already processes Ctrl+C.
    """
    if sys.platform == "win32":
        import ctypes

        ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(None, False)


__all__ = [
    "TerminalAdapter",
    "TerminalAdapterFactory",
    "default_terminal_argv",
    "spawn_terminal_adapter",
    "terminate_process_tree",
    "windows_command_processor_line",
]
