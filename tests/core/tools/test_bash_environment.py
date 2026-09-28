"""The shell environment: probing the login shell, caching it, and the Windows PATH."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from typing import Any

import pytest

import core.tools._bash_environment as bash_environment
import core.tools.bash as bash_module
from tests.core.tools.bash_test_support import shell_env_cache as shell_env_cache


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Real PowerShell environment probe")
async def test_windows_shell_env_probe_round_trips_non_ascii_and_multiline_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Legacy console code pages cannot represent these characters.
    value = "C:/Users/J\u00fcrgen \u00c4rger/\u20ac \u65e5\u672c \U0001f600"
    monkeypatch.setenv("VBOT_PROBE_NON_ASCII", value)
    monkeypatch.setenv("VBOT_PROBE_MULTILINE", "first\nsecond=still value")

    env = await bash_environment._probe_shell_env()

    assert env["VBOT_PROBE_NON_ASCII"] == value
    assert env["VBOT_PROBE_MULTILINE"] == "first\nsecond=still value"


@pytest.mark.asyncio
async def test_malformed_windows_probe_output_keeps_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GarbledProbe:
        pid = 1
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"J\x81rgen=not base64\r\n", b""

    async def create_probe(*_args: Any, **_kwargs: Any) -> GarbledProbe:
        return GarbledProbe()

    monkeypatch.setattr(bash_environment.sys, "platform", "win32")
    monkeypatch.setattr(bash_environment.asyncio, "create_subprocess_exec", create_probe)
    monkeypatch.setattr(bash_environment, "_read_registry_path", lambda: None)
    monkeypatch.setenv("VBOT_PROBE_FALLBACK", "J\u00fcrgen")

    env = await bash_environment._probe_shell_env()

    assert env == dict(os.environ)
    assert env["VBOT_PROBE_FALLBACK"] == "J\u00fcrgen"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX login-shell probe decoding")
async def test_posix_shell_env_probe_preserves_undecodable_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Probe:
        pid = 1
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"RAW=J\xfcrgen\x00UTF8=J\xc3\xbcrgen\x00", b""

    async def create_probe(*_args: Any, **_kwargs: Any) -> Probe:
        return Probe()

    monkeypatch.setattr(bash_environment.asyncio, "create_subprocess_exec", create_probe)

    env = await bash_environment._probe_shell_env()

    assert os.fsencode(env["RAW"]) == b"J\xfcrgen"
    assert env["UTF8"] == "J\u00fcrgen"


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
@pytest.mark.parametrize(
    ("ttl", "age", "reset", "expected"),
    [
        (60.0, 0.0, False, "cached"),
        (60.0, 61.0, False, "probed"),
        # A TTL of zero disables the refresh.
        (0, 10_000.0, False, "cached"),
        # Runtime.reload_shell_env() resets the cache before the TTL elapses.
        (999.0, 0.0, True, "probed"),
    ],
    ids=["fresh", "expired", "ttl-zero", "reset"],
)
async def test_shell_env_cache_is_reused_until_its_ttl_or_a_reset(
    monkeypatch: pytest.MonkeyPatch, ttl: float, age: float, reset: bool, expected: str
) -> None:
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return {"PATH": "probed"}

    monkeypatch.setattr(bash_environment, "_probe_shell_env", probe_shell_env)
    monkeypatch.setattr(bash_environment, "SHELL_ENV_CACHE_TTL_SECONDS", ttl)
    monkeypatch.setattr(bash_environment, "_cached_shell_env", {"PATH": "cached"})
    monkeypatch.setattr(bash_environment, "_shell_env_cache_time", time.monotonic() - age)
    if reset:
        bash_module.reset_shell_env_cache()

    assert await bash_module.get_shell_env() == {"PATH": expected}
    # A fresh probe result is cached for the next call.
    assert await bash_module.get_shell_env() == {"PATH": expected}
    assert probe_calls == (1 if expected == "probed" else 0)


def _fake_winreg(machine: str | Exception, user: str) -> Any:
    class FakeKey:
        def __init__(self, path_value: str) -> None:
            self.path_value = path_value

        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    def open_key(hkey: int, subkey: str) -> FakeKey:
        assert subkey
        if isinstance(machine, Exception):
            raise machine
        return FakeKey(machine if hkey == winreg.HKEY_LOCAL_MACHINE else user)

    def query_value_ex(key: FakeKey, name: str) -> tuple[str, int]:
        assert name == "PATH"
        return key.path_value, winreg.REG_EXPAND_SZ

    winreg: Any = types.ModuleType("winreg")
    winreg.HKEY_LOCAL_MACHINE = 1
    winreg.HKEY_CURRENT_USER = 2
    winreg.REG_EXPAND_SZ = 2
    winreg.ExpandEnvironmentStrings = lambda value: value.replace("%SystemRoot%", "C:\\windows")
    winreg.OpenKey = open_key
    winreg.QueryValueEx = query_value_ex
    return winreg


@pytest.mark.parametrize(
    ("platform", "machine", "user", "expected"),
    [
        # Machine and user segments are joined and expanded.
        (
            "win32",
            "C:\\system32;%SystemRoot%",
            "C:\\user\\bin",
            "C:\\system32;C:\\windows;C:\\user\\bin",
        ),
        ("win32", "", "", None),
        ("win32", OSError("registry unavailable"), "", None),
        # Other platforms never import winreg.
        ("linux", OSError("never opened"), "", None),
    ],
    ids=["machine-and-user", "both-empty", "unavailable", "posix"],
)
def test_registry_path_joins_machine_and_user_segments(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    machine: str | Exception,
    user: str,
    expected: str | None,
) -> None:
    monkeypatch.setattr(bash_module.sys, "platform", platform)
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(machine, user))

    assert bash_environment._read_registry_path() == expected


@pytest.mark.parametrize(
    ("registry_path", "expected_path"),
    [
        ("C:\\new;C:\\fresh", "C:\\new;C:\\fresh"),
        # Without a registry value (always so off Windows) the inherited PATH stays.
        (None, "C:\\old"),
    ],
    ids=["registry", "no-registry-value"],
)
def test_registry_path_replaces_the_inherited_path(
    monkeypatch: pytest.MonkeyPatch, registry_path: str | None, expected_path: str
) -> None:
    monkeypatch.setattr(bash_environment, "_read_registry_path", lambda: registry_path)

    result = bash_environment._overlay_registry_path({"PATH": "C:\\old", "OTHER": "keep"})

    assert result == {"PATH": expected_path, "OTHER": "keep"}
