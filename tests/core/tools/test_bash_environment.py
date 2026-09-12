"""Bash: environment behavior."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

import core.tools._bash_environment as bash_environment
import core.tools.bash as bash_module
from core.tools.bash import (
    bash_handler,
)
from core.tools.process_manager import ProcessManager
from core.utils.processes import subprocess_creation_flags
from tests.core.tools.bash_helpers import (
    kill_background,
    make_context,
    python_command,
)
from tests.core.tools.bash_helpers import (
    manager as manager,
)
from tests.core.tools.bash_helpers import (
    shell_env_cache as shell_env_cache,
)


def test_shell_detection_uses_native_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    assert bash_module._shell_argv("Write-Output hello") == [
        "pwsh",
        "-NonInteractive",
        "-Command",
        "Write-Output hello",
    ]

    monkeypatch.setattr(bash_module.sys, "platform", "linux")

    assert bash_module._shell_argv("echo hello") == ["bash", "-c", "echo hello"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell-specific regression")
async def test_windows_unknown_pipeline_command_exits_non_interactively(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_environment, "_cached_shell_env", dict(os.environ))
    context = make_context(tmp_path, nesting_depth=1)

    result = await asyncio.wait_for(
        bash_handler(
            context,
            {
                "command": "Get-ChildItem . | __vbot_missing_pipeline_command__",
                "mode": "foreground",
            },
            manager,
        ),
        timeout=5,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] != 0
    assert "__vbot_missing_pipeline_command__" in result["data"]["output"]


def test_shell_env_probe_requests_windowless_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def creation_flags(*, new_process_group: bool = False) -> int:
        calls.append(new_process_group)
        return 123

    monkeypatch.setattr(bash_environment, "subprocess_creation_flags", creation_flags)

    assert bash_environment._probe_creationflags() == 123
    assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "auto", "background"])
async def test_all_modes_use_managed_windowless_process_group_spawn(
    mode: str,
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, int]] = []

    def creation_flags(
        *,
        new_process_group: bool = False,
        platform_name: str = os.name,
    ) -> int:
        flags = subprocess_creation_flags(
            new_process_group=new_process_group,
            platform_name=platform_name,
        )
        calls.append((new_process_group, flags))
        return flags

    monkeypatch.setattr(
        "core.tools.process_manager.subprocess_creation_flags",
        creation_flags,
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    arguments: dict[str, Any] = {
        "command": ("print('done')" if mode == "foreground" else "import time; time.sleep(30)"),
        "mode": mode,
    }
    if mode == "auto":
        arguments["background_after_seconds"] = 0.01

    result: dict[str, Any] | None = None
    try:
        result = await bash_handler(make_context(tmp_path), arguments, manager)

        expected_flags = subprocess_creation_flags(new_process_group=True)
        assert result["ok"] is True
        assert calls == [(True, expected_flags)]
    finally:
        if result is not None and result["ok"] is True and result["data"]["status"] == "running":
            await kill_background(manager, result)


@pytest.mark.asyncio
async def test_shell_env_probe_timeout_terminates_and_reaps_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed_process_groups: list[tuple[int, int]] = []

    class HungProbe:
        pid = 12345
        returncode = None

        def __init__(self) -> None:
            self.communicate_calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.Future()
            self.returncode = -9
            return b"", b""

    probe = HungProbe()

    async def create_probe(*_args: Any, **_kwargs: Any) -> HungProbe:
        return probe

    monkeypatch.setattr(bash_module.sys, "platform", "linux")
    monkeypatch.setattr(bash_environment.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(bash_environment, "SHELL_ENV_PROBE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(bash_module.asyncio, "create_subprocess_exec", create_probe)
    monkeypatch.setattr(
        bash_module.os,
        "killpg",
        lambda process_group_id, signal_number: killed_process_groups.append(
            (process_group_id, signal_number)
        ),
        raising=False,
    )
    monkeypatch.setenv("VBOT_PROBE_FALLBACK", "fallback")

    env = await bash_environment._probe_shell_env()

    assert env["VBOT_PROBE_FALLBACK"] == "fallback"
    assert killed_process_groups == [(12345, 9)]
    assert probe.communicate_calls == 2


@pytest.mark.asyncio
async def test_concurrent_shell_env_requests_share_one_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        probe_started.set()
        await release_probe.wait()
        return {"PATH": "probed-path"}

    monkeypatch.setattr(bash_environment, "_cached_shell_env", None)
    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe_shell_env)

    first = asyncio.create_task(bash_module.get_shell_env())
    second = asyncio.create_task(bash_module.get_shell_env())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert probe_calls == 1

    release_probe.set()
    first_env, second_env = await asyncio.gather(first, second)

    assert first_env == {"PATH": "probed-path"}
    assert second_env == {"PATH": "probed-path"}
    assert first_env is not second_env
    assert bash_environment._cached_shell_env == {"PATH": "probed-path"}
    assert bash_environment._shell_env_probe_task is None


@pytest.mark.asyncio
async def test_cancelling_shell_env_waiter_keeps_shared_probe_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        probe_started.set()
        await release_probe.wait()
        return {"PATH": "probed-path"}

    monkeypatch.setattr(bash_environment, "_cached_shell_env", None)
    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe_shell_env)

    cancelled_waiter = asyncio.create_task(bash_module.get_shell_env())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    surviving_waiter = asyncio.create_task(bash_module.get_shell_env())
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    assert probe_calls == 1
    assert bash_environment._shell_env_probe_task is not None
    assert not bash_environment._shell_env_probe_task.cancelled()

    release_probe.set()

    assert await surviving_waiter == {"PATH": "probed-path"}
    assert bash_environment._cached_shell_env == {"PATH": "probed-path"}
    assert bash_environment._shell_env_probe_task is None


@pytest.mark.asyncio
async def test_shell_env_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale cache triggers a fresh re-probe on the next call."""
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return {"PATH": f"probe-{probe_calls}"}

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe_shell_env)
    monkeypatch.setattr(bash_environment, "SHELL_ENV_CACHE_TTL_SECONDS", 0.01)

    # Seed the cache with a pre-existing value and a fresh timestamp.
    monkeypatch.setattr(bash_environment, "_cached_shell_env", {"PATH": "old"})
    monkeypatch.setattr(bash_environment, "_shell_env_cache_time", time.monotonic())

    env_first = await bash_module.get_shell_env()
    assert env_first == {"PATH": "old"}
    assert probe_calls == 0  # cache still fresh

    await asyncio.sleep(0.02)  # exceed TTL

    env_second = await bash_module.get_shell_env()
    assert env_second == {"PATH": "probe-1"}
    assert probe_calls == 1  # re-probed after expiry


@pytest.mark.asyncio
async def test_shell_env_cache_ttl_zero_never_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTL of zero means the cache never expires (disables refresh)."""
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return {"PATH": "fresh"}

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe_shell_env)
    monkeypatch.setattr(bash_environment, "SHELL_ENV_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(bash_environment, "_cached_shell_env", {"PATH": "cached"})
    monkeypatch.setattr(bash_environment, "_shell_env_cache_time", 0.0)

    env = await bash_module.get_shell_env()
    assert env == {"PATH": "cached"}
    assert probe_calls == 0


def test_reset_shell_env_cache_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_shell_env_cache sets the cache to None so the next call re-probes."""
    monkeypatch.setattr(bash_environment, "_cached_shell_env", {"PATH": "stale"})
    monkeypatch.setattr(bash_environment, "_shell_env_cache_time", time.monotonic())

    bash_module.reset_shell_env_cache()

    assert bash_environment._cached_shell_env is None
    assert bash_environment._shell_env_cache_time == 0.0


@pytest.mark.asyncio
async def test_reset_shell_env_cache_forces_reprobe_on_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After reset, the next get_shell_env call re-probes even if TTL hasn't elapsed."""
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return {"PATH": f"probe-{probe_calls}"}

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe_shell_env)
    monkeypatch.setattr(bash_environment, "SHELL_ENV_CACHE_TTL_SECONDS", 999.0)
    monkeypatch.setattr(bash_environment, "_cached_shell_env", None)

    env_first = await bash_module.get_shell_env()
    assert env_first == {"PATH": "probe-1"}
    assert probe_calls == 1

    # Without reset, the cache is fresh (TTL=999) so no re-probe.
    env_cached = await bash_module.get_shell_env()
    assert env_cached == {"PATH": "probe-1"}
    assert probe_calls == 1

    bash_module.reset_shell_env_cache()
    env_after_reset = await bash_module.get_shell_env()
    assert env_after_reset == {"PATH": "probe-2"}
    assert probe_calls == 2


def test_overlay_registry_path_overwrites_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_overlay_registry_path replaces PATH with the registry value when available."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")
    monkeypatch.setattr(bash_environment, "_read_registry_path", lambda: "C:\\new;C:\\fresh")

    env = {"PATH": "C:\\old", "OTHER": "keep"}
    result = bash_environment._overlay_registry_path(env)

    assert result["PATH"] == "C:\\new;C:\\fresh"
    assert result["OTHER"] == "keep"


def test_overlay_registry_path_preserves_path_when_registry_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the registry read returns None, the existing PATH is kept."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")
    monkeypatch.setattr(bash_environment, "_read_registry_path", lambda: None)

    env = {"PATH": "C:\\original"}
    result = bash_environment._overlay_registry_path(env)

    assert result["PATH"] == "C:\\original"


def test_overlay_registry_path_is_noop_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-Windows, _overlay_registry_path returns the env unchanged."""
    monkeypatch.setattr(bash_module.sys, "platform", "linux")

    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/user"}
    result = bash_environment._overlay_registry_path(env)

    assert result == env


def test_read_registry_path_returns_none_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_registry_path returns None on non-Windows without importing winreg."""
    monkeypatch.setattr(bash_module.sys, "platform", "linux")
    assert bash_environment._read_registry_path() is None


def test_read_registry_path_combines_machine_and_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, _read_registry_path joins machine and user PATH segments."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    class FakeKey:
        def __init__(self, path_value: str) -> None:
            self._path_value = path_value

        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    fake_winreg: Any = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 1
    fake_winreg.HKEY_CURRENT_USER = 2

    open_key_calls: list[tuple[int, str]] = []

    def fake_open_key(hkey: int, subkey: str) -> FakeKey:
        open_key_calls.append((hkey, subkey))
        if hkey == fake_winreg.HKEY_LOCAL_MACHINE:
            return FakeKey("C:\\system32;C:\\windows")
        return FakeKey("C:\\user\\bin")

    def fake_query_value_ex(key: FakeKey, name: str) -> tuple[str, int]:
        assert name == "PATH"
        return key._path_value, 2  # REG_EXPAND_SZ

    fake_winreg.OpenKey = fake_open_key
    fake_winreg.QueryValueEx = fake_query_value_ex
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    result = bash_environment._read_registry_path()
    assert result == "C:\\system32;C:\\windows;C:\\user\\bin"
    assert len(open_key_calls) == 2


def test_read_registry_path_returns_none_when_both_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both machine and user PATH are empty, _read_registry_path returns None."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    class FakeKey:
        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    fake_winreg: Any = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 1
    fake_winreg.HKEY_CURRENT_USER = 2

    def fake_open_key(hkey: int, subkey: str) -> FakeKey:
        return FakeKey()

    def fake_query_value_ex(key: FakeKey, name: str) -> tuple[str, int]:
        return "", 2

    fake_winreg.OpenKey = fake_open_key
    fake_winreg.QueryValueEx = fake_query_value_ex
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert bash_environment._read_registry_path() is None


def test_read_registry_path_returns_none_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the registry key cannot be opened, _read_registry_path returns None."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    fake_winreg: Any = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 1
    fake_winreg.HKEY_CURRENT_USER = 2

    def fake_open_key(hkey: int, subkey: str) -> Any:
        raise OSError("registry unavailable")

    fake_winreg.OpenKey = fake_open_key
    fake_winreg.QueryValueEx = lambda *_args: ("", 2)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert bash_environment._read_registry_path() is None


@pytest.mark.asyncio
async def test_spawn_filenotfounderror_triggers_env_refresh_and_retry(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FileNotFoundError on spawn resets the env cache and retries once."""
    from core.tools.process_manager import ProcessManager as _ProcMgr

    spawn_calls = 0
    original_spawn = _ProcMgr.spawn

    async def tracking_spawn(self: _ProcMgr, *args: Any, **kwargs: Any) -> str:
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls == 1:
            raise FileNotFoundError("pwsh not found")
        return await original_spawn(self, *args, **kwargs)

    monkeypatch.setattr(_ProcMgr, "spawn", tracking_spawn)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    reset_calls: list[bool] = []
    original_reset = bash_module.reset_shell_env_cache

    def tracking_reset() -> None:
        reset_calls.append(True)
        original_reset()

    monkeypatch.setattr(bash_module, "reset_shell_env_cache", tracking_reset)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "print('recovered')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "recovered"
    assert spawn_calls == 2
    assert reset_calls == [True]


@pytest.mark.asyncio
async def test_spawn_filenotfounderror_retry_failure_returns_failure_envelope(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the retry also fails with FileNotFoundError, a failure envelope is returned."""
    from core.tools.process_manager import ProcessManager as _ProcMgr

    async def always_fail(self: _ProcMgr, *args: Any, **kwargs: Any) -> str:
        raise FileNotFoundError("still not found")

    monkeypatch.setattr(_ProcMgr, "spawn", always_fail)
    monkeypatch.setattr(bash_module, "reset_shell_env_cache", lambda: None)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "ignored", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "process_spawn_failed"
