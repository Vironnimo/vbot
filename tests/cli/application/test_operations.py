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
from core.utils import processes as core_processes


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


@pytest.mark.parametrize("in_service", [False, True], ids=["shell", "systemd-service"])
def test_worker_spawn_is_detached_and_strips_run_caller_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_service: bool
):
    install = _install(tmp_path)
    monkeypatch.setattr(core_processes, "_service_cgroup", lambda: in_service)
    operation = Operation(id="upd_spawn")
    captured: dict[str, object] = {}
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "session-secret")
    monkeypatch.setenv("PYTHONPATH", "caller-path")
    monkeypatch.setenv("VBOT_HOST_SUCCESSOR", "1")

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
    assert "VBOT_HOST_SUCCESSOR" not in environment
    assert environment["VBOT_INSTALL_ROOT"] == str(install.root)
    assert captured["creationflags"] == 73
    assert captured["stdin"] is operations.subprocess.DEVNULL
    assert load_operation(install, operation.id).worker_pid == 123
    # A worker started inside a service, such as the server's systemd unit, leaves
    # it, so that stopping the server does not end the worker too.
    arguments = cast(list[str], captured["arguments"])
    scoped = in_service and os.name != "nt"
    assert arguments[0] == ("systemd-run" if scoped else str(install.interpreter(role="Update")))


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


def test_agent_handoff_requires_a_server_owned_ticket_in_this_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    client = Installation(tmp_path, "desktop-client", "127.0.0.1", 8420, None)
    foreign = tmp_path / "elsewhere" / "runtime" / "update-handoffs" / "ticket.json"
    minted: list[dict[str, object]] = []

    def rpc_call(_target: object, method: str, params: dict[str, object]) -> SimpleNamespace:
        assert method == "application.update_handoff_mint"
        minted.append(params)
        return SimpleNamespace(ok=True, data={"handoff_ticket": str(foreign)}, message="")

    monkeypatch.setattr("cli.rpc_client.rpc_call", rpc_call)
    monkeypatch.setattr("cli.application.processes.target", lambda _install: SimpleNamespace())
    monkeypatch.setattr(operations, "spawn_worker", lambda *_args: pytest.fail("must not spawn"))

    with pytest.raises(ApplicationError, match="client-only"):
        operations.request_update(client, handoff_token="opaque-token")
    assert minted == []

    with pytest.raises(ApplicationError, match="not valid for this installation's data directory"):
        operations.request_update(install, handoff_token="opaque-token")
    assert minted == [{"handoff_token": "opaque-token"}]
    assert operations.operations(install) == []


def test_observer_reports_the_newest_operation_and_rereads_only_changed_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    observer = operations.OperationObserver(install)
    assert observer.latest() is None
    Operation(
        id="upd_older", phase="completed", message="done", created_at="2026-01-01T00:00:00+00:00"
    ).save(install)
    newer = Operation(
        id="upd_newer",
        phase="preparing",
        message="Checking",
        created_at="2026-02-01T00:00:00+00:00",
    )
    newer.save(install)
    loads: list[str] = []
    load = operations.load_operation

    def counted(install: Installation, identifier: str) -> Operation:
        loads.append(identifier)
        return load(install, identifier)

    monkeypatch.setattr(operations, "load_operation", counted)

    assert cast(Operation, observer.latest()).id == "upd_newer"
    assert sorted(loads) == ["upd_newer", "upd_older"]
    loads.clear()
    assert cast(Operation, observer.latest()).phase == "preparing"
    assert loads == []

    newer.transition(install, "completed", "Updated")
    latest = cast(Operation, observer.latest())
    assert (latest.phase, latest.message) == ("completed", "Updated")
    assert loads == ["upd_newer"]

    (install.root / "operations" / "upd_broken.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ApplicationError):
        observer.latest()
