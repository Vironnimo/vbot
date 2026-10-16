"""Start processes that must outlive another application's Windows Job.

A process inside another application's Job (a coding agent's shell, say) cannot
start a child outside it when that Job forbids breakaway: the child ends when the
application closes. Windows Management Instrumentation creates processes from
its own provider host, outside every Job of the caller, in the caller's
interactive session (``tools/os-processes.md`` -> observed Windows behavior).
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.utils import processes
from core.utils.logging import get_logger
from core.utils.processes import ForeignJob, subprocess_creation_flags

_LOGGER = get_logger("processes")
_BROKER_TIMEOUT_SECONDS = 30.0
#: Reported as the exit code of a created process, which is not this process's child.
UNKNOWN_EXIT_CODE = 1

# The payload arrives base64-encoded on standard input: an environment can exceed
# the size of one environment variable, and base64 keeps it independent of the
# console code page. ``EnvironmentVariables`` replaces the whole environment;
# without it the process gets the user's default environment.
_BROKER_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try {
    $encoded = [Console]::In.ReadToEnd().Trim()
    $payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) |
        ConvertFrom-Json
    $properties = @{ CreateFlags = [uint32]$payload.flags }
    if ($null -ne $payload.environment) {
        $properties.EnvironmentVariables = [string[]]$payload.environment
    }
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property $properties
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = [string]$payload.command
        CurrentDirectory = [string]$payload.directory
        ProcessStartupInformation = $startup
    }
    if ($result.ReturnValue -ne 0) {
        [Console]::Out.WriteLine("Win32_Process.Create returned $($result.ReturnValue)")
        exit 1
    }
    [Console]::Out.WriteLine($result.ProcessId)
    exit 0
}
catch {
    [Console]::Out.WriteLine($_.Exception.Message)
    exit 1
}
"""


@dataclass(frozen=True, slots=True)
class Confinement:
    """A process had to start inside ``job`` after starting it outside failed (``reason``)."""

    job: ForeignJob
    reason: str


class OutsideJobProcess:
    """A process :func:`start_outside_job` created, observed like a ``subprocess.Popen``.

    It is not a child of this process, so its exit code is unknown: an exited
    process reports :data:`UNKNOWN_EXIT_CODE`. ``wait`` raises
    ``subprocess.TimeoutExpired`` like ``Popen.wait``.
    """

    def __init__(self, pid: int, process: Any | None) -> None:
        self.pid = pid
        self.returncode: int | None = None if process is not None else UNKNOWN_EXIT_CODE
        self._process: Any = process

    def poll(self) -> int | None:
        if self.returncode is None and not self._process.is_running():
            self.returncode = UNKNOWN_EXIT_CODE
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import psutil  # type: ignore[import-untyped]

        if self.poll() is None:
            try:
                self._process.wait(timeout=timeout)
            except psutil.TimeoutExpired as exc:
                raise subprocess.TimeoutExpired(str(self.pid), timeout or 0) from exc
        self.returncode = UNKNOWN_EXIT_CODE
        return self.returncode

    def terminate(self) -> None:
        self._signal("terminate")

    def kill(self) -> None:
        self._signal("kill")

    def _signal(self, method: str) -> None:
        import psutil

        if self.poll() is None:
            with suppress(psutil.NoSuchProcess):
                getattr(self._process, method)()


def confining_job() -> ForeignJob | None:
    """The foreign Windows Job that keeps even a breakaway child of this process inside.

    ``None`` outside Windows, outside such a Job, or when the Job cannot be
    inspected (logged).
    """

    try:
        job = processes.foreign_job()
    except OSError as exc:
        _LOGGER.warning("Inspecting the Windows Job of this process failed (error=%s)", exc)
        return None
    return job if job is not None and job.confines_children else None


def start_outside_job(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    creationflags: int,
    env: Mapping[str, str] | None = None,
) -> OutsideJobProcess:
    """Create ``argv`` through WMI, outside every Job of this process (Windows only).

    ``creationflags`` are the Win32 creation flags. Without ``env`` the process
    gets the user's default environment. Raises ``OSError`` when WMI refuses or
    does not answer (``TimeoutError``), for example under Defender's rule that
    blocks process creation from WMI.
    """

    import psutil

    payload = {
        "command": subprocess.list2cmdline(list(argv)),
        "directory": str(cwd),
        "flags": creationflags,
        "environment": None if env is None else [f"{key}={value}" for key, value in env.items()],
    }
    script = base64.b64encode(_BROKER_SCRIPT.encode("utf-16-le")).decode("ascii")
    requested = time.time()
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                script,
            ],
            input=base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_BROKER_TIMEOUT_SECONDS,
            creationflags=subprocess_creation_flags(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Windows Management Instrumentation did not answer") from exc
    detail = (result.stdout.strip().splitlines() or [f"exit code {result.returncode}"])[-1]
    if result.returncode != 0 or not detail.isdigit():
        raise OSError(f"Windows Management Instrumentation refused: {detail}")
    pid = int(detail)
    try:
        process = psutil.Process(pid)
        # A process that already exited can leave its pid to an unrelated older one.
        if process.create_time() < requested - 1.0:
            process = None
    except psutil.Error:
        process = None
    return OutsideJobProcess(pid, process)


def start_outside_foreign_job[P](
    argv: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    spawn: Callable[[], P],
) -> tuple[P | OutsideJobProcess, Confinement | None]:
    """Start a process that must outlive the application whose Windows Job holds this one.

    Outside a confining foreign Job (always on other platforms) ``spawn`` starts
    it as usual. Inside one, WMI creates ``argv`` outside the Job, windowless in
    its own process group with ``cwd`` and ``env``. When that fails, ``spawn``
    starts it inside the Job after all, and the returned :class:`Confinement`
    tells the caller to warn that the process ends with that application.
    """

    job = confining_job()
    if job is None:
        return spawn(), None
    try:
        process = start_outside_job(
            argv, cwd=cwd, env=env, creationflags=subprocess_creation_flags(new_process_group=True)
        )
    except OSError as exc:
        _LOGGER.warning(
            "Could not start a process outside a foreign Windows Job; it starts inside and "
            "ends when that application closes (owner=%s owner_pid=%s error=%s)",
            job.name,
            job.pid,
            exc,
        )
        return spawn(), Confinement(job, str(exc))
    _LOGGER.debug(
        "Started a process outside a foreign Windows Job (pid=%s owner=%s owner_pid=%s)",
        process.pid,
        job.name,
        job.pid,
    )
    return process, None
