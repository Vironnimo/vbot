"""Cached host shell environment, login probing, and probe cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time

from core.utils.processes import subprocess_creation_flags, windows_taskkill_tree

SHELL_ENV_PROBE_TIMEOUT_SECONDS = 5.0
SHELL_ENV_PROBE_REAP_TIMEOUT_SECONDS = 1.0


# A freshly installed program does not appear in a shell spawned by Bash until
# the cached environment is re-probed. On Windows the vBot process never sees
# the registry-broadcast PATH change, and the one-time probe inherits that
# frozen PATH, so without re-probing the only remedy was a full restart. The
# TTL bounds staleness automatically; an explicit invalidation is exposed for
# the runtime and for the spawn-failure safety-net.
SHELL_ENV_CACHE_TTL_SECONDS = 300.0
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
_cached_shell_env: dict[str, str] | None = None
_shell_env_cache_time: float = 0.0
_shell_env_probe_task: asyncio.Task[dict[str, str]] | None = None


def reset_shell_env_cache() -> None:
    """Invalidate the cached shell environment so the next Bash call re-probes.

    Exposed as the seam behind ``Runtime.reload_shell_env()``. After a program
    is installed (or PATH otherwise mutates outside vBot), a call here makes the
    next Bash command see the fresh environment without restarting the server.
    """
    global _cached_shell_env, _shell_env_cache_time, _shell_env_probe_task
    _cached_shell_env = None
    _shell_env_cache_time = 0.0


async def get_shell_env() -> dict[str, str]:
    global _cached_shell_env, _shell_env_cache_time, _shell_env_probe_task

    if _cached_shell_env is not None and _is_shell_env_cache_fresh():
        return dict(_cached_shell_env)

    if _cached_shell_env is None:
        # First-ever probe — same concurrent-dedup logic as before.
        probe_task = _shell_env_probe_task
        if probe_task is None:
            probe_task = asyncio.create_task(_probe_shell_env())
            _shell_env_probe_task = probe_task
        try:
            probed_env = await asyncio.shield(probe_task)
        except asyncio.CancelledError:
            if probe_task.cancelled() and _shell_env_probe_task is probe_task:
                _shell_env_probe_task = None
            raise
        except BaseException:
            if _shell_env_probe_task is probe_task:
                _shell_env_probe_task = None
            raise
        if _cached_shell_env is None:
            _cached_shell_env = probed_env
            _shell_env_cache_time = time.monotonic()
        if _shell_env_probe_task is probe_task:
            _shell_env_probe_task = None
    else:
        # Cache exists but is stale — re-probe directly. The TTL makes this
        # infrequent (default 5 min), and concurrent callers each get their own
        # refresh copy. An in-flight first-probe task is left untouched.
        _cached_shell_env = await _probe_shell_env()
        _shell_env_cache_time = time.monotonic()

    return dict(_cached_shell_env)


def _is_shell_env_cache_fresh() -> bool:
    if SHELL_ENV_CACHE_TTL_SECONDS <= 0:
        return True
    return (time.monotonic() - _shell_env_cache_time) < SHELL_ENV_CACHE_TTL_SECONDS


async def _probe_shell_env() -> dict[str, str]:
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "pwsh",
                "-NoProfile",
                "-Command",
                'Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" }',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_probe_creationflags(),
                start_new_session=_probe_start_new_session(),
            )
            stdout = await _communicate_with_probe_timeout(proc)
            if proc.returncode != 0:
                return _overlay_registry_path(os.environ.copy())
            env = _parse_line_env(stdout.decode("utf-8", errors="replace"))
            return _overlay_registry_path(env)

        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-l",
            "-c",
            "env -0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_probe_creationflags(),
            start_new_session=_probe_start_new_session(),
        )
        stdout = await _communicate_with_probe_timeout(proc)
        if proc.returncode != 0:
            return os.environ.copy()
        return _parse_null_env(stdout.decode("utf-8", errors="replace"))
    except (OSError, TimeoutError):
        if sys.platform == "win32":
            return _overlay_registry_path(os.environ.copy())
        return os.environ.copy()


def _probe_creationflags() -> int:
    return subprocess_creation_flags(new_process_group=True)


def _probe_start_new_session() -> bool:
    return sys.platform != "win32"


def _overlay_registry_path(env: dict[str, str]) -> dict[str, str]:
    """Overlay the current Windows registry PATH onto a probed environment.

    On Windows, a headless process never receives the ``WM_SETTINGCHANGE``
    broadcast that follows a PATH edit, so ``os.environ['PATH']`` — and thus
    the probe — stays frozen at vBot start time. Reading the machine and user
    PATH values directly from the registry gives the probe the live value
    without a restart, mirroring what Chocolatey's ``refreshenv`` does.
    """
    registry_path = _read_registry_path()
    if registry_path is not None:
        env["PATH"] = registry_path
    return env


def _read_registry_path() -> str | None:
    """Read the effective PATH from the Windows registry (machine + user).

    Returns ``None`` when the registry cannot be read, so the caller keeps the
    probed value unchanged instead of clobbering it with an empty string.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    segments: list[str] = []
    with (
        contextlib.suppress(OSError),
        winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key,
    ):
        machine_path, _regtype = winreg.QueryValueEx(key, "PATH")
        if machine_path:
            segments.append(machine_path)
    with (
        contextlib.suppress(OSError),
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key,
    ):
        user_path, _regtype = winreg.QueryValueEx(key, "PATH")
        if user_path:
            segments.append(user_path)

    if not segments:
        return None
    return ";".join(segments)


async def _communicate_with_probe_timeout(proc: asyncio.subprocess.Process) -> bytes:
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=SHELL_ENV_PROBE_TIMEOUT_SECONDS,
        )
        return stdout
    except TimeoutError:
        await _terminate_probe_process(proc)
        raise


async def _terminate_probe_process(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            taskkill_succeeded = await asyncio.to_thread(windows_taskkill_tree, proc.pid)

            if not taskkill_succeeded:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        else:
            os.killpg(proc.pid, HARD_KILL_SIGNAL)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    try:
        await asyncio.wait_for(
            proc.communicate(),
            timeout=SHELL_ENV_PROBE_REAP_TIMEOUT_SECONDS,
        )
    except (ProcessLookupError, RuntimeError, TimeoutError):
        return


def _parse_line_env(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            env[key] = value
    return env


def _parse_null_env(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in output.split("\0"):
        key, separator, value = item.partition("=")
        if separator and key:
            env[key] = value
    return env
