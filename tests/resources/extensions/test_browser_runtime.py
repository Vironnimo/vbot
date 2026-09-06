"""Cold setup, cached reuse, integrity, platform dependencies, and cancellation."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from resources.extensions.browser_use import runtime as module


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    runtime = module.BrowserRuntime(tmp_path)
    runtime.browser_cache = tmp_path / "cache"
    content = b"owned native fixture"
    key = "win32-x64.exe"
    monkeypatch.setattr(module, "asset_name", lambda: key)
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(module.ASSETS, key, hashlib.sha256(content).hexdigest())
    calls = []

    def download(url, target, expected, check):
        check()
        calls.append("download")
        target.write_bytes(content)

    def run(command, check, **kwargs):
        check()
        calls.append(command)
        if command[-1] == "install":
            path = runtime.browser_cache / "chrome-test/chrome.exe"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"browser fixture")
        return f"agent-browser {module.VERSION}"

    monkeypatch.setattr(runtime, "_download", download)
    monkeypatch.setattr(runtime, "_run", run)
    return runtime, calls, run, download


def test_cold_start_installs_owned_native_and_browser_then_reuses(prepared):
    runtime, calls, *_ = prepared
    result = runtime.ensure("managed", lambda: None)
    assert all(Path(path).is_file() for path in result)
    assert calls.count("download") == 1
    assert len([call for call in calls if call[-1] == "install"]) == 1
    before = list(calls)
    assert runtime.ensure("managed", lambda: None) == result
    assert calls == before
    assert json.loads((runtime.root / "installed.json").read_text())["client"] == module.VERSION


@pytest.mark.parametrize("mode", ["existing", "remote"])
def test_connected_modes_only_prepare_client(prepared, mode):
    runtime, calls, *_ = prepared
    native, chrome = runtime.ensure(mode, lambda: None)
    assert Path(native).is_file() and not chrome
    assert not any(call[-1] == "install" for call in calls)


def test_new_service_reuses_completed_installation_after_reload(prepared, monkeypatch):
    runtime, calls, run, download = prepared
    expected = runtime.ensure("managed", lambda: None)
    other = module.BrowserRuntime(runtime.root.parents[1])
    other.browser_cache = runtime.browser_cache
    monkeypatch.setattr(other, "_download", download)
    monkeypatch.setattr(other, "_run", run)
    assert other.ensure("managed", lambda: None) == expected
    assert calls.count("download") == 1
    assert len([call for call in calls if call[-1] == "install"]) == 1


def test_corrupt_binary_is_replaced_before_execution(prepared):
    runtime, calls, *_ = prepared
    path = runtime.root / module.VERSION / "agent-browser-win32-x64.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt")
    runtime.ensure("remote", lambda: None)
    assert calls[0] == "download"
    assert path.read_bytes() != b"corrupt"


def test_failed_setup_has_only_two_attempts(prepared, monkeypatch):
    runtime, _, _, _ = prepared
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise module.SetupError("network")

    monkeypatch.setattr(runtime, "_download", fail)
    for _ in range(4):
        with pytest.raises(module.SetupError, match="network"):
            runtime.ensure("managed", lambda: None)
    assert len(calls) == 2


def test_permission_or_cancel_after_download_prevents_install(prepared, monkeypatch):
    runtime, calls, _, original = prepared
    revoked = False

    def check():
        if revoked:
            raise RuntimeError("cancelled")

    def download(*args):
        nonlocal revoked
        original(*args)
        revoked = True

    monkeypatch.setattr(runtime, "_download", download)
    with pytest.raises(RuntimeError, match="cancelled"):
        runtime.ensure("managed", check)
    assert not any(call[-1] == "install" for call in calls)


def test_concurrent_installations_share_file_lock(prepared, monkeypatch):
    runtime, calls, run, download = prepared
    other = module.BrowserRuntime(runtime.root.parents[1])
    other.browser_cache = runtime.browser_cache
    monkeypatch.setattr(other, "_run", run)

    def delayed(*args):
        time.sleep(0.05)
        download(*args)

    monkeypatch.setattr(runtime, "_download", delayed)
    monkeypatch.setattr(other, "_download", delayed)
    results = []
    threads = [
        threading.Thread(
            target=lambda item=item: results.append(item.ensure("managed", lambda: None))
        )
        for item in (runtime, other)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert len(results) == 2 and results[0] == results[1]
    assert calls.count("download") == 1


def test_download_verifies_integrity_and_never_publishes_bad_binary(tmp_path, monkeypatch):
    runtime = module.BrowserRuntime(tmp_path)
    target = tmp_path / "native"

    def response(*args, **kwargs):
        return httpx.Response(
            200, content=b"untrusted", request=httpx.Request("GET", "https://example.com")
        )

    class Stream:
        def __enter__(self):
            return response()

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(module.httpx, "stream", lambda *args, **kwargs: Stream())
    with pytest.raises(module.SetupError, match="client_integrity"):
        runtime._download("https://example.com", target, "0" * 64, lambda: None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("win32", "AMD64", "win32-x64.exe"),
        ("win32", "ARM64", "win32-x64.exe"),
        ("linux", "aarch64", "linux-arm64"),
        ("linux", "x86_64", "linux-x64"),
        ("darwin", "arm64", "darwin-arm64"),
        ("darwin", "x86_64", "darwin-x64"),
    ],
)
def test_platform_asset_selection(monkeypatch, system, machine, expected):
    monkeypatch.setattr(module.sys, "platform", system)
    monkeypatch.setattr(module.platform, "machine", lambda: machine)
    monkeypatch.setattr(module.platform, "libc_ver", lambda: ("glibc", "2.36"))
    assert module.asset_name() == expected


def test_linux_arm64_prepares_system_chromium_without_interactive_prompt(prepared, monkeypatch):
    runtime, _, _, _ = prepared
    installed = False
    calls = []
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: (
            "/usr/bin/apt-get"
            if name == "apt-get"
            else "/usr/bin/chromium"
            if name == "chromium" and installed
            else None
        ),
    )

    def run(command, *args, **kwargs):
        nonlocal installed
        calls.append(command)
        if "install" in command:
            installed = True

    monkeypatch.setattr(runtime, "_run", run)
    assert runtime._browser(Path("native"), lambda: None) == "/usr/bin/chromium"
    assert calls == [
        ["sudo", "-n", "apt-get", "update", "-y"],
        ["sudo", "-n", "apt-get", "install", "-y", "chromium"],
    ]


def test_cancellation_terminates_owned_installer_process(tmp_path, monkeypatch):
    runtime = module.BrowserRuntime(tmp_path)
    runtime.root.mkdir(parents=True)
    checks = 0
    killed = []
    process = SimpleNamespace(
        pid=123, poll=lambda: None, kill=lambda: killed.append(True), wait=lambda **kwargs: 0
    )
    monkeypatch.setattr(module.os, "killpg", lambda *_: killed.append(True), raising=False)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)

    def check():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        runtime._run(["native", "install"], check)
    assert killed == [True]
