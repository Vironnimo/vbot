"""OS process containment, windowless launch, program presence and confirmed tree termination."""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from core.utils.logging import get_logger


class ProcessIdentity(Protocol):
    @property
    def pid(self) -> int: ...


_LOGGER = get_logger("processes")

HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_LIMIT_INFORMATION_CLASS = 2
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_WINDOWS_SERVER_JOB_HANDLE: int | None = None
_POSIX_LIFETIME_READ_FD: int | None = None
_POSIX_LIFETIME_WRITE_FD: int | None = None


@dataclass(frozen=True, slots=True)
class GuardedProcessLaunch:
    """Exact argv plus inherited descriptors for a contained child launch."""

    argv: tuple[str, ...]
    pass_fds: tuple[int, ...] = ()


def activate_process_containment(*, platform_name: str = os.name) -> None:
    """Make this server process the OS-level lifetime owner of later children."""

    if platform_name == "nt":
        _activate_windows_process_job()
        return
    _activate_posix_lifetime_pipe()


def guarded_process_launch(
    argv: Sequence[str], *, platform_name: str = os.name
) -> GuardedProcessLaunch:
    """Wrap a POSIX child with the active server-lifetime guardian when enabled."""

    if not argv:
        raise ValueError("Process argv must not be empty")
    if platform_name == "nt" or _POSIX_LIFETIME_READ_FD is None:
        return GuardedProcessLaunch(tuple(argv))
    return GuardedProcessLaunch(
        (
            sys.executable,
            "-m",
            "core.utils.process_guardian",
            "--lifetime-fd",
            str(_POSIX_LIFETIME_READ_FD),
            "--",
            *argv,
        ),
        (_POSIX_LIFETIME_READ_FD,),
    )


def _activate_posix_lifetime_pipe() -> None:
    global _POSIX_LIFETIME_READ_FD, _POSIX_LIFETIME_WRITE_FD

    if _POSIX_LIFETIME_READ_FD is not None:
        return
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)
    _POSIX_LIFETIME_READ_FD = read_fd
    _POSIX_LIFETIME_WRITE_FD = write_fd


def _activate_windows_process_job() -> None:
    """Assign the server to a kill-on-close Windows Job Object."""

    global _WINDOWS_SERVER_JOB_HANDLE

    if _WINDOWS_SERVER_JOB_HANDLE is not None:
        return
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise windows_ctypes.WinError(windows_ctypes.get_last_error())
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    if not kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = windows_ctypes.WinError(windows_ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = windows_ctypes.WinError(windows_ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    _WINDOWS_SERVER_JOB_HANDLE = int(job)


def subprocess_creation_flags(
    *,
    new_process_group: bool = False,
    breakaway: bool = False,
    platform_name: str = os.name,
) -> int:
    """Return platform flags for a windowless child process."""
    if platform_name != "nt":
        return 0

    flags = int(cast(Any, subprocess).CREATE_NO_WINDOW)
    if new_process_group:
        flags |= int(cast(Any, subprocess).CREATE_NEW_PROCESS_GROUP)
    if breakaway and _windows_explicit_breakaway_allowed():
        flags |= int(cast(Any, subprocess).CREATE_BREAKAWAY_FROM_JOB)
    return flags


def _windows_explicit_breakaway_allowed() -> bool:
    """Return whether the immediate Windows Job permits explicit breakaway."""
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.LPBOOL]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    in_job = wintypes.BOOL()
    if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
        raise windows_ctypes.WinError(windows_ctypes.get_last_error())
    if not in_job.value:
        return False
    limits = BasicLimitInformation()
    if not kernel32.QueryInformationJobObject(
        None,
        _JOB_OBJECT_BASIC_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
        None,
    ):
        raise windows_ctypes.WinError(windows_ctypes.get_last_error())
    limit_flags = int(limits.LimitFlags)
    if limit_flags & _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK:
        return False
    return bool(limit_flags & _JOB_OBJECT_LIMIT_BREAKAWAY_OK)


TASKKILL_TREE_TIMEOUT_SECONDS = 5


def windows_taskkill_tree(pid: int, *, targets: list[Any] | None = None) -> bool:
    """Best-effort blocking ``taskkill`` of a whole Windows process tree.

    Returns True only after the captured tree has terminated. ``targets``
    retains process identities across failed attempts, including orphaned
    children. Each blocking phase has ``TASKKILL_TREE_TIMEOUT_SECONDS``, so event-loop
    contexts must run it through :func:`kill_process_tree_async` (worker
    thread) instead of calling it directly.
    """
    import psutil  # type: ignore[import-untyped]

    try:
        processes = targets if targets is not None else []
        root = processes[-1] if processes else psutil.Process(pid)
        root_alive = root.is_running()
        if root_alive:
            descendants = root.children(recursive=True)
            processes[:] = (
                [
                    *[child for child in descendants if child not in processes],
                    *processes,
                ]
                if processes
                else [*descendants, root]
            )
    except psutil.NoSuchProcess:
        if not processes:
            return True
        root_alive = False
    except psutil.Error:
        return False
    try:
        if not root_alive:
            raise ProcessLookupError
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TASKKILL_TREE_TIMEOUT_SECONDS,
            check=False,
            creationflags=subprocess_creation_flags(),
        )
        taskkill_succeeded = completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        taskkill_succeeded = False
    if not taskkill_succeeded:
        _LOGGER.warning("taskkill failed for pid=%s; terminating the captured process tree", pid)
        # Capture identity-bearing Process objects before root exit can orphan
        # its children. Process.kill also protects against PID reuse.
        for process in reversed(processes):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                return False
    _gone, alive = psutil.wait_procs(processes, timeout=TASKKILL_TREE_TIMEOUT_SECONDS)
    return not alive


async def kill_process_tree_async(
    proc: ProcessIdentity, *, targets: list[Any] | None = None
) -> None:
    """Kill a whole process tree without blocking the event loop.

    Same contract as ``kill_process_tree`` - including
    propagating ``ProcessLookupError`` on POSIX - but the Windows ``taskkill``
    subprocess runs in a worker thread so the loop never stalls behind it.
    """
    if os.name == "nt":
        killed = await asyncio.to_thread(windows_taskkill_tree, proc.pid, targets=targets)
        if not killed:
            raise OSError("taskkill could not confirm process-tree termination")
        return

    _kill_process_tree_posix(proc)


def _kill_process_tree_posix(proc: ProcessIdentity) -> None:
    """Kill a POSIX process group; re-raises ProcessLookupError."""
    kill_process_group = cast(Any, os).__dict__["killpg"]
    kill_process_group(proc.pid, HARD_KILL_SIGNAL)


def kill_process_tree(proc: ProcessIdentity, *, targets: list[Any] | None = None) -> None:
    """Kill the owned process tree; raise when termination cannot be confirmed."""
    if os.name == "nt":
        if windows_taskkill_tree(proc.pid, targets=targets):
            return
        raise OSError("taskkill could not confirm process-tree termination")
    _kill_process_tree_posix(proc)


# Script hosts that run an installed command-line program from a script
# argument, such as ``node .../node_modules/@openai/codex/bin/codex.js``.
_SCRIPT_HOSTS = frozenset({"node", "nodejs", "bun"})
_COMMAND_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".js", ".mjs", ".cjs")
_PATH_SEPARATORS = re.compile(r"[\\/]")


def process_tree_runs(pid: int, program: str) -> bool:
    """Whether *program* runs, not stopped, as process *pid* or one of its descendants.

    A process runs *program* when its name, its executable, or its first
    argument names it, or, for a script host such as ``node``, when its script
    or the npm package holding the script does. A name matches exactly or as
    ``<program>-...``: ``codex``, ``codex.exe``, ``codex.js`` and ``claude-code``
    name their programs. Returns ``False`` when the tree cannot be inspected, so
    callers fail closed.
    """
    import psutil  # type: ignore[import-untyped]

    wanted = program.strip().casefold()
    if not wanted:
        return False
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return False
    return any(_process_runs(process, wanted) for process in processes)


def _process_runs(process: Any, wanted: str) -> bool:
    import psutil  # type: ignore[import-untyped]

    stopped = {psutil.STATUS_STOPPED, psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    try:
        with process.oneshot():
            if process.status() in stopped:
                return False
            name = process.name()
            exe = _readable(process.exe, "")
            cmdline = _readable(process.cmdline, [])
    except psutil.Error:
        return False
    return _names_program(name, exe, cmdline, wanted)


def _names_program(name: str, exe: str, cmdline: Sequence[str], wanted: str) -> bool:
    """Whether a process with this name, executable and command line runs *wanted*."""
    return any(
        candidate == wanted or candidate.startswith(f"{wanted}-")
        for candidate in _command_names(name, exe, cmdline)
    )


def _readable(read: Any, default: Any) -> Any:
    """One process attribute, or *default* when the OS denies reading it."""
    import psutil  # type: ignore[import-untyped]

    try:
        return read() or default
    except psutil.AccessDenied:
        return default


def _command_names(name: str, exe: str, cmdline: Sequence[str]) -> set[str]:
    """The lowercase program names one process runs under, without executable suffixes."""
    names = {_command_name(name), _command_name(exe)}
    if cmdline:
        names.add(_command_name(cmdline[0]))
    if names & _SCRIPT_HOSTS:
        script = next((argument for argument in cmdline[1:] if not argument.startswith("-")), "")
        names.add(_command_name(script))
        parts = [part.casefold() for part in _PATH_SEPARATORS.split(script) if part]
        if "node_modules" in parts:
            package = parts[len(parts) - parts[::-1].index("node_modules") :][:2]
            if len(package) == 2 and package[0].startswith("@"):
                names.add(package[1])
            elif package:
                names.add(package[0])
    names.discard("")
    return names


def _command_name(path: str) -> str:
    base = _PATH_SEPARATORS.split(path.strip())[-1].casefold()
    for suffix in _COMMAND_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base
