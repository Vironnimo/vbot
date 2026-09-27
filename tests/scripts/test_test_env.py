"""Smoke test of the live-testing environment script (``scripts/test-env.py``).

Besides the smoke test, the fake Provider stop guard keeps its own test: the script terminates
processes by PID and must never kill an unrelated process that reused that PID.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_test_env_module() -> ModuleType:
    module_path = PROJECT_ROOT / "scripts" / "test-env.py"
    spec = importlib.util.spec_from_file_location("test_env", module_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TEST_ENV = _load_test_env_module()


def test_start_launches_the_seeded_fake_provider_before_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = json.loads(
        (PROJECT_ROOT / "tests" / "e2e" / "fake-provider-settings.json").read_text(encoding="utf-8")
    )
    settings["providers"]["custom"]["fake"]["base_url"] = "http://127.0.0.1:18422/v1"
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        sys, "argv", ["test-env.py", "start", "--port", "8422", "--data-dir", str(tmp_path)]
    )

    def start_fake_provider(provider: object) -> bool:
        calls.append(("provider", provider))
        return True

    def start_server(*args: object) -> int:
        calls.append(("server", args))
        return 0

    monkeypatch.setattr(TEST_ENV, "build_frontend", lambda: 0)
    monkeypatch.setattr(TEST_ENV, "start_fake_provider", start_fake_provider)
    monkeypatch.setattr(TEST_ENV, "start_server", start_server)

    assert TEST_ENV.main() == 0
    assert calls == [
        (
            "provider",
            TEST_ENV.FakeProviderInstance(
                host="127.0.0.1",
                port=18422,
                pid_path=tmp_path.resolve() / "processes" / "fake-provider.pid",
                log_path=tmp_path.resolve() / "processes" / "fake-provider.log",
            ),
        ),
        ("server", ("127.0.0.1", 8422, str(tmp_path))),
    ]


@pytest.mark.parametrize(
    ("create_time", "stopped"),
    [
        pytest.param(1234.5, True, id="owned-process"),
        pytest.param(9999.0, False, id="reused-pid"),
    ],
)
def test_stop_terminates_only_the_fake_provider_process_it_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, create_time: float, stopped: bool
) -> None:
    provider = TEST_ENV.FakeProviderInstance(
        host="127.0.0.1",
        port=18422,
        pid_path=tmp_path / "processes" / "fake-provider.pid",
        log_path=tmp_path / "processes" / "fake-provider.log",
    )
    provider.pid_path.parent.mkdir(parents=True)
    provider.pid_path.write_text(json.dumps({"pid": 4321, "create_time": 1234.5}), encoding="utf-8")
    terminated: list[int] = []

    def wait(*, timeout: float) -> None:
        return None

    process: Any = SimpleNamespace(
        create_time=lambda: create_time,
        cmdline=lambda: ["node", str(TEST_ENV.FAKE_PROVIDER_ENTRY)],
        terminate=lambda: terminated.append(4321),
        wait=wait,
    )
    monkeypatch.setattr(TEST_ENV.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(TEST_ENV, "_fake_provider_is_ready", lambda _instance: True)
    monkeypatch.setattr(TEST_ENV, "_wait_for_fake_provider", lambda _instance, *, ready: True)

    assert TEST_ENV.stop_fake_provider(provider) is stopped
    assert terminated == ([4321] if stopped else [])
    assert provider.pid_path.exists() is not stopped
