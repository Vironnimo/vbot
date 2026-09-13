"""Dispatch remains durable and independent of the caller that requested it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from cli.application import operations
from cli.application.state import ApplicationError, Installation, Operation, load_operation


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    version = root / "versions" / "rel_current"
    (version / "runtime").mkdir(parents=True)
    (version / "app").mkdir()
    (version / "release.json").write_text("{}", encoding="utf-8")
    executable = version / "runtime" / ("vBot.Update.exe" if os.name == "nt" else "bin/python3")
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    (root / "active-version").write_text("rel_current\n", encoding="ascii")
    return install


def test_wait_returns_the_saved_terminal_human_result(tmp_path: Path):
    install = _install(tmp_path)
    operation = Operation(id="upd_terminal", phase="completed", message="done")
    operation.save(install)
    observed: list[str] = []

    result = operations.wait(
        install, operation.id, progress=lambda value: observed.append(value.phase)
    )

    assert result.id == operation.id
    assert result.terminal is True
    assert observed == ["completed"]


def test_request_coalesces_a_live_operation_and_recovers_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    pending = Operation(id="upd_pending", phase="preparing")
    spawned: list[str] = []
    monkeypatch.setattr(operations, "operations", lambda _install: [pending])
    monkeypatch.setattr(
        operations, "spawn_worker", lambda _install, operation: spawned.append(operation.id)
    )

    monkeypatch.setattr(operations, "worker_alive", lambda _operation: True)
    assert operations.request_update(install) is pending
    assert spawned == []

    monkeypatch.setattr(operations, "worker_alive", lambda _operation: False)
    assert operations.request_update(install) is pending
    assert spawned == [pending.id]
    operations.recover_operations(install)
    assert spawned == [pending.id, pending.id]


def test_worker_spawn_is_detached_and_strips_run_caller_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(id="upd_spawn")
    captured: dict[str, object] = {}
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "session-secret")
    monkeypatch.setenv("PYTHONPATH", "caller-path")

    def running(*, timeout):
        raise subprocess.TimeoutExpired("updater", timeout)

    monkeypatch.setattr(operations, "subprocess_creation_flags", lambda **_kwargs: 73)
    monkeypatch.setattr(
        operations.subprocess,
        "Popen",
        lambda arguments, **kwargs: (
            captured.update(arguments=arguments, **kwargs)
            or SimpleNamespace(wait=running, poll=lambda: None, returncode=None, pid=123)
        ),
    )
    monkeypatch.setattr(
        operations.psutil,
        "Process",
        lambda _pid: SimpleNamespace(create_time=lambda: 456.0),
    )

    operations.spawn_worker(install, operation)

    environment = cast(dict[str, str], captured["env"])
    assert "VBOT_RUN_SESSION_ID" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["VBOT_INSTALL_ROOT"] == str(install.root)
    assert captured["creationflags"] == 73
    assert captured["stdin"] is operations.subprocess.DEVNULL
    assert load_operation(install, operation.id).worker_pid == 123


def test_pending_update_rejects_incompatible_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    pending = Operation(id="upd_pending", phase="preparing", restart=True)
    pending.save(install)
    monkeypatch.setattr(operations, "worker_alive", lambda _operation: True)

    with pytest.raises(ApplicationError, match="different options"):
        operations.request_update(install, restart=False)


def test_operation_is_persisted_before_spawn_and_spawn_failure_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    observed: list[str] = []

    def verify_persisted(_install: Installation, operation: Operation) -> None:
        observed.append(load_operation(install, operation.id).phase)

    monkeypatch.setattr(operations, "spawn_worker", verify_persisted)
    accepted = operations.request_update(install)
    assert observed == ["queued"]
    assert load_operation(install, accepted.id).phase == "queued"
    accepted.transition(install, "completed", "first request finished")

    monkeypatch.setattr(
        operations,
        "spawn_worker",
        lambda _install, _operation: (_ for _ in ()).throw(ApplicationError("spawn unavailable")),
    )
    with pytest.raises(ApplicationError, match="spawn unavailable"):
        operations.request_update(install)
    failed = operations.status(install)
    assert failed is not None
    assert failed.phase == "failed"
    assert failed.error == "spawn unavailable"


@pytest.mark.parametrize("exit_code", [0, 111])
def test_worker_failure_before_claim_is_saved_with_its_diagnostics(
    tmp_path, monkeypatch, exit_code
):
    install = _install(tmp_path)

    def spawn(args, **kwargs):
        kwargs["stdout"].write(b"native-runtime-failure")
        return SimpleNamespace(wait=lambda **kw: exit_code)

    monkeypatch.setattr(operations.subprocess, "Popen", spawn)
    with pytest.raises(ApplicationError):
        operations.request_update(install)
    failed = operations.status(install)
    assert failed.phase == "failed"
    startup_log = install.root / "logs" / f"{failed.id}-startup.log"
    assert str(startup_log) in failed.error
    assert startup_log.read_bytes() == b"native-runtime-failure"
