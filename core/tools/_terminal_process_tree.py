"""Every process a command starts: liveness, CPU time, child exit codes and the kill.

A command's shell is the root of a process tree whose members can outlive it,
detach from it (``Start-Process``, ``nohup``, ``&``) or fail while the shell
reports success. ``track_process_tree`` captures the tree right after the
root starts:

- Windows: a Job Object holds the root and, by inheritance, every process it
  creates; children that explicitly break away (``CREATE_BREAKAWAY_FROM_JOB``)
  leave it, which the vBot update handoff needs. Job notifications record the
  exit codes of the root's direct children, so a native program's failure is a
  fact even when PowerShell reduces it to exit code 1. A process the root
  creates in the moment between its start and the job assignment escapes.
- POSIX: the root is a session leader; the tree is every process of its
  session. A process that starts its own session (``setsid``, daemons) leaves.

Facts are best effort where the platform is: Windows does not guarantee job
notifications, so a missing child exit code proves nothing. Liveness and CPU
time come from authoritative queries.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Protocol, cast

import psutil  # type: ignore[import-untyped]

_TERMINATE_CONFIRM_SECONDS = 5.0
_TERMINATE_POLL_SECONDS = 0.05
_MAX_LISTED_PROCESSES = 64


@dataclass(frozen=True, slots=True)
class RunningProcess:
    pid: int
    name: str


@dataclass(frozen=True, slots=True)
class ProgramExit:
    """A direct child of the root that exited with a non-zero code."""

    name: str
    exit_code: int

    def describe(self) -> str:
        code = f"{self.exit_code}"
        if self.exit_code >= 0x80000000:
            code += f" (0x{self.exit_code:08X})"
        return f"{self.name} exited with code {code}"


@dataclass(frozen=True, slots=True)
class ProcessTreeFacts:
    # Processes of the tree still running, the root included while it runs.
    running: tuple[RunningProcess, ...]
    # Total CPU time the tree has used so far, in seconds.
    cpu_seconds: float
    # Processes ever started in the tree; a change means new activity.
    started: int
    nonzero_exits: tuple[ProgramExit, ...]


class ProcessTree(Protocol):
    """The process tree of one command."""

    def facts(self) -> ProcessTreeFacts: ...

    def exit_count(self) -> int:
        """How many failed child exits are recorded so far; does not block."""

    def terminate(self) -> None:
        """Kill every process of the tree; raise OSError when that is not confirmed."""

    def close(self) -> None:
        """Release tracking resources once the tree has ended or was terminated.

        On Windows a process still in the tree is killed (kill-on-close job).
        """


ProcessTreeTracker = Callable[[int], ProcessTree]


def track_process_tree(root_pid: int) -> ProcessTree:
    """Start tracking the tree of the just-started *root_pid*."""
    if sys.platform == "win32":
        return _WindowsJobTree(root_pid)
    return _PosixSessionTree(root_pid)


# POSIX


class _PosixSessionTree:
    def __init__(self, root_pid: int) -> None:
        self._sid = root_pid
        self._started: set[int] = set()

    def _members(self) -> list[Any]:
        members = []
        getsid = cast(Any, os).getsid
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if getsid(process.pid) == self._sid:
                    members.append(process)
            except OSError:
                continue
        return members

    def facts(self) -> ProcessTreeFacts:
        running: list[RunningProcess] = []
        cpu = 0.0
        for process in self._members():
            try:
                times = process.cpu_times()
                if process.status() == psutil.STATUS_ZOMBIE:
                    continue
            except psutil.Error:
                continue
            cpu += float(times.user) + float(times.system)
            self._started.add(process.pid)
            running.append(RunningProcess(process.pid, str(process.info.get("name") or "")))
        return ProcessTreeFacts(
            running=tuple(running[:_MAX_LISTED_PROCESSES]),
            cpu_seconds=cpu,
            started=len(self._started),
            nonzero_exits=(),
        )

    def exit_count(self) -> int:
        return 0

    def terminate(self) -> None:
        kill = cast(Any, os).killpg
        hard = getattr(signal, "SIGKILL", signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(self._sid, hard)
        deadline = time.monotonic() + _TERMINATE_CONFIRM_SECONDS
        while True:
            members = self._live_members()
            if not members:
                return
            for process in members:
                with contextlib.suppress(Exception):
                    process.kill()
            if time.monotonic() >= deadline:
                raise OSError(f"{len(members)} processes of the command survived the kill")
            time.sleep(_TERMINATE_POLL_SECONDS)

    def _live_members(self) -> list[Any]:
        live = []
        for process in self._members():
            with contextlib.suppress(psutil.Error):
                if process.status() != psutil.STATUS_ZOMBIE:
                    live.append(process)
        return live

    def close(self) -> None:
        return


# Windows

_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_BASIC_ACCOUNTING = 1
_JOB_BASIC_PROCESS_ID_LIST = 3
_JOB_ASSOCIATE_COMPLETION_PORT = 7
_JOB_EXTENDED_LIMITS = 9
_MSG_ACTIVE_PROCESS_ZERO = 4
_MSG_NEW_PROCESS = 6
_MSG_EXIT_PROCESS = 7
_MSG_ABNORMAL_EXIT_PROCESS = 8
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259


class _Kernel:
    """kernel32/ntdll entry points with explicit signatures, loaded once."""

    _instance: _Kernel | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        from ctypes import wintypes

        windll = cast(Any, ctypes).WinDLL
        self.kernel32 = k = windll("kernel32", use_last_error=True)
        self.ntdll = n = windll("ntdll")
        handle = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k.CreateJobObjectW.restype = handle
        k.SetInformationJobObject.argtypes = (handle, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = (
            handle,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = (handle, handle)
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = (handle, wintypes.UINT)
        k.TerminateJobObject.restype = wintypes.BOOL
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = handle
        k.GetExitCodeProcess.argtypes = (handle, ctypes.POINTER(wintypes.DWORD))
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = (
            handle,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = (handle,)
        k.CloseHandle.restype = wintypes.BOOL
        k.CreateIoCompletionPort.argtypes = (handle, handle, ctypes.c_size_t, wintypes.DWORD)
        k.CreateIoCompletionPort.restype = handle
        k.GetQueuedCompletionStatus.argtypes = (
            handle,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        )
        k.GetQueuedCompletionStatus.restype = wintypes.BOOL
        n.NtQueryInformationProcess.argtypes = (
            handle,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        )
        n.NtQueryInformationProcess.restype = ctypes.c_long

    @classmethod
    def get(cls) -> _Kernel:
        with cls._lock:
            if cls._instance is None:
                cls._instance = _Kernel()
            return cls._instance

    def error(self) -> OSError:
        windows_ctypes = cast(Any, ctypes)
        return cast(OSError, windows_ctypes.WinError(windows_ctypes.get_last_error()))


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _AssociateCompletionPort(ctypes.Structure):
    _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", ctypes.c_void_p)]


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_long),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_size_t),
        ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    ]


class _JobNotifications:
    """One completion port and thread that route job messages to their trees."""

    _instance: _JobNotifications | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._kernel = _Kernel.get()
        port = self._kernel.kernel32.CreateIoCompletionPort(ctypes.c_void_p(-1), None, 0, 1)
        if not port:
            raise self._kernel.error()
        self.port = port
        self._trees: dict[int, _WindowsJobTree] = {}
        self._next_key = 1
        self._trees_lock = threading.Lock()
        threading.Thread(target=self._run, name="vbot-command-jobs", daemon=True).start()

    @classmethod
    def get(cls) -> _JobNotifications:
        with cls._lock:
            if cls._instance is None:
                cls._instance = _JobNotifications()
            return cls._instance

    def register(self, tree: _WindowsJobTree) -> int:
        with self._trees_lock:
            key = self._next_key
            self._next_key += 1
            self._trees[key] = tree
            return key

    def unregister(self, key: int) -> None:
        with self._trees_lock:
            self._trees.pop(key, None)

    def _run(self) -> None:
        from ctypes import wintypes

        message = wintypes.DWORD()
        key = ctypes.c_size_t()
        value = ctypes.c_void_p()
        while True:
            if not self._kernel.kernel32.GetQueuedCompletionStatus(
                self.port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(value), _INFINITE
            ):
                continue
            with self._trees_lock:
                tree = self._trees.get(int(key.value))
            if tree is not None:
                with contextlib.suppress(Exception):
                    tree.notify(int(message.value), int(value.value or 0))


class _WindowsJobTree:
    def __init__(self, root_pid: int) -> None:
        self._kernel = kernel = _Kernel.get()
        self._k = k = kernel.kernel32
        self._root_pid = root_pid
        self._lock = threading.Lock()
        self._children: dict[int, tuple[Any, str]] = {}
        self._exits: list[ProgramExit] = []
        self._empty = threading.Event()
        self._closed = False
        job = k.CreateJobObjectW(None, None)
        if not job:
            raise kernel.error()
        self._job = job
        self._notifications = _JobNotifications.get()
        self._key = self._notifications.register(self)
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
            )
            self._set(_JOB_EXTENDED_LIMITS, limits)
            association = _AssociateCompletionPort(self._key, self._notifications.port)
            self._set(_JOB_ASSOCIATE_COMPLETION_PORT, association)
            root = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, root_pid)
            if not root:
                raise kernel.error()
            try:
                if not k.AssignProcessToJobObject(job, root):
                    raise kernel.error()
            finally:
                k.CloseHandle(root)
        except BaseException:
            self._notifications.unregister(self._key)
            k.CloseHandle(job)
            raise

    def _set(self, information_class: int, value: ctypes.Structure) -> None:
        if not self._k.SetInformationJobObject(
            self._job, information_class, ctypes.byref(value), ctypes.sizeof(value)
        ):
            raise self._kernel.error()

    def notify(self, message: int, pid: int) -> None:
        if message == _MSG_NEW_PROCESS:
            self._remember_child(pid)
        elif message in (_MSG_EXIT_PROCESS, _MSG_ABNORMAL_EXIT_PROCESS):
            self._record_exit(pid)
        elif message == _MSG_ACTIVE_PROCESS_ZERO:
            self._empty.set()

    def _remember_child(self, pid: int) -> None:
        if pid == self._root_pid:
            return
        handle = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
        if not handle:
            return
        information = _ProcessBasicInformation()
        status = self._kernel.ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(information), ctypes.sizeof(information), None
        )
        if status != 0 or int(information.InheritedFromUniqueProcessId) != self._root_pid:
            self._k.CloseHandle(handle)
            return
        name = _image_name(self._k, handle) or f"process {pid}"
        with self._lock:
            if self._closed:
                self._k.CloseHandle(handle)
                return
            self._children[pid] = (handle, name)

    def _record_exit(self, pid: int) -> None:
        from ctypes import wintypes

        with self._lock:
            child = self._children.pop(pid, None)
        if child is None:
            return
        handle, name = child
        code = wintypes.DWORD()
        try:
            if self._k.GetExitCodeProcess(handle, ctypes.byref(code)):
                value = int(code.value)
                if value not in (0, _STILL_ACTIVE):
                    with self._lock:
                        self._exits.append(ProgramExit(name, value))
        finally:
            self._k.CloseHandle(handle)

    def facts(self) -> ProcessTreeFacts:
        accounting = _BasicAccounting()
        if not self._k.QueryInformationJobObject(
            self._job,
            _JOB_BASIC_ACCOUNTING,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise self._kernel.error()
        running = self._running() if accounting.ActiveProcesses else ()
        with self._lock:
            exits = tuple(self._exits)
        return ProcessTreeFacts(
            running=running,
            cpu_seconds=(accounting.TotalUserTime + accounting.TotalKernelTime) / 10_000_000,
            started=int(accounting.TotalProcesses),
            nonzero_exits=exits,
        )

    def exit_count(self) -> int:
        with self._lock:
            return len(self._exits)

    def _running(self) -> tuple[RunningProcess, ...]:
        capacity = _MAX_LISTED_PROCESSES

        class ProcessIdList(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", ctypes.c_uint32),
                ("NumberOfProcessIdsInList", ctypes.c_uint32),
                ("ProcessIdList", ctypes.c_size_t * capacity),
            ]

        listing = ProcessIdList()
        # A job with more processes than the list holds fails with
        # ERROR_MORE_DATA after filling the list, which is enough here.
        self._k.QueryInformationJobObject(
            self._job,
            _JOB_BASIC_PROCESS_ID_LIST,
            ctypes.byref(listing),
            ctypes.sizeof(listing),
            None,
        )
        processes = []
        for index in range(min(int(listing.NumberOfProcessIdsInList), capacity)):
            pid = int(listing.ProcessIdList[index])
            handle = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            name = ""
            if handle:
                try:
                    name = _image_name(self._k, handle)
                finally:
                    self._k.CloseHandle(handle)
            processes.append(RunningProcess(pid, name or f"process {pid}"))
        return tuple(processes)

    def terminate(self) -> None:
        if not self._k.TerminateJobObject(self._job, 1):
            raise self._kernel.error()
        deadline = time.monotonic() + _TERMINATE_CONFIRM_SECONDS
        while self._active() and time.monotonic() < deadline:
            self._empty.wait(_TERMINATE_POLL_SECONDS)
        if self._active():
            raise OSError("Processes of the command survived the job termination")

    def _active(self) -> int:
        accounting = _BasicAccounting()
        if not self._k.QueryInformationJobObject(
            self._job,
            _JOB_BASIC_ACCOUNTING,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise self._kernel.error()
        return int(accounting.ActiveProcesses)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            children, self._children = self._children, {}
        self._notifications.unregister(self._key)
        for handle, _name in children.values():
            self._k.CloseHandle(handle)
        self._k.CloseHandle(self._job)


def _image_name(kernel32: Any, handle: Any) -> str:
    from ctypes import wintypes

    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return ""
    return PurePath(buffer.value).name


__all__ = [
    "ProcessTree",
    "ProcessTreeFacts",
    "ProcessTreeTracker",
    "ProgramExit",
    "RunningProcess",
    "track_process_tree",
]
