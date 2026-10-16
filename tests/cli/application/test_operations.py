"""Dispatch remains durable and independent of the caller that requested it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psutil  # type: ignore[import-untyped]
import pytest

from cli.application import operations
from cli.application.state import ApplicationError, Installation, Operation, load_operation
from core.utils import processes as core_processes
from core.utils.processes import ForeignJob
from core.utils.server_control import process_started


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


def _running_tray(install: Installation) -> None:
    """Record a live process other than this one as the installation's tray host."""

    parent = psutil.Process(os.getppid())
    (install.root / "host.json").write_text(
        json.dumps(
            {"schema_version": 1, "pid": parent.pid, "process_created": process_started(parent)}
        ),
        encoding="utf-8",
    )


def _tray_dispatches(install: Installation, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Let the tray consume the dispatch request while the requester waits for it."""

    spawned: list[str] = []

    def spawn(_install: Installation, operation: Operation) -> None:
        spawned.append(operation.id)
        operation.worker_pid, operation.worker_created = 4242, 1.0
        operation.save(install)

    monkeypatch.setattr(operations, "spawn_worker", spawn)
    monkeypatch.setattr(
        operations, "_sleep", lambda _seconds: operations.dispatch_requested(install)
    )
    return spawned


_CONFINED = ForeignJob("codex.exe", 4711, confines_children=True)


@pytest.mark.parametrize(
    "route", ["running-tray", "confined-starts-tray", "tray-itself", "breakaway-allowed"]
)
def test_update_worker_starts_outside_a_foreign_job_of_the_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    install = _install(tmp_path)
    spawned = _tray_dispatches(install, monkeypatch)
    brokered: list[Path] = []
    monkeypatch.setattr(
        operations, "_start_host_outside_job", lambda target: brokered.append(target.root)
    )
    if route == "running-tray":
        _running_tray(install)
    elif route == "breakaway-allowed":
        monkeypatch.setattr(
            core_processes, "foreign_job", lambda: ForeignJob("codex.exe", 4711, False)
        )
    else:
        monkeypatch.setattr(core_processes, "foreign_job", lambda: _CONFINED)

    accepted = operations.request_update(install, host=route == "tray-itself")

    # The worker always runs; only a requester outside the tray hands it over.
    assert spawned == [accepted.id]
    assert accepted.worker_pid == 4242
    assert brokered == ([install.root] if route == "confined-starts-tray" else [])
    assert not (install.root / operations.DISPATCH_REQUEST).exists()


@pytest.mark.parametrize("confined", [False, True], ids=["unconfined", "confined"])
def test_handoff_no_tray_takes_is_settled_by_the_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confined: bool
) -> None:
    install = _install(tmp_path)
    _running_tray(install)
    monkeypatch.setattr(operations, "HOST_DISPATCH_SECONDS", 0.0)
    monkeypatch.setattr(operations, "_sleep", lambda _seconds: pytest.fail("deadline passed"))
    spawned: list[str] = []
    monkeypatch.setattr(
        operations, "spawn_worker", lambda _install, operation: spawned.append(operation.id)
    )
    if confined:
        monkeypatch.setattr(core_processes, "foreign_job", lambda: _CONFINED)
        with pytest.raises(ApplicationError, match="codex.exe"):
            operations.request_update(install)
        failed = cast(Operation, operations.status(install))
        # Nothing ran: the request fails instead of a later tray running it by surprise.
        assert failed.phase == "failed"
        assert spawned == []
    else:
        # An older tray ignores the request; outside a foreign Job that is harmless.
        accepted = operations.request_update(install)
        assert spawned == [accepted.id]
    assert not (install.root / operations.DISPATCH_REQUEST).exists()


def test_confined_requester_refuses_when_no_tray_can_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    monkeypatch.setattr(core_processes, "foreign_job", lambda: _CONFINED)
    monkeypatch.setattr(operations, "spawn_worker", lambda *_args: pytest.fail("must not spawn"))

    def refuse(_install: Installation) -> None:
        raise OSError("Windows Management Instrumentation refused: access denied")

    monkeypatch.setattr(operations, "_start_host_outside_job", refuse)

    with pytest.raises(ApplicationError, match="could not be started"):
        operations.request_update(install)
    assert cast(Operation, operations.status(install)).phase == "failed"
    assert not (install.root / operations.DISPATCH_REQUEST).exists()


@pytest.mark.parametrize(
    ("tray", "expected"),
    [
        pytest.param("answers", {"ok": True, "message": "started", "process_id": 99}, id="answers"),
        pytest.param("fails", {"ok": False, "message": "port is occupied"}, id="fails"),
        # A tray of an older version drops the request it cannot read.
        pytest.param("older", None, id="older-tray"),
        pytest.param("silent", None, id="silent"),
        pytest.param("accepts-only", {"ok": False}, id="accepts-only"),
    ],
)
def test_server_request_returns_the_trays_answer_or_none_when_no_tray_took_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tray: str,
    expected: dict[str, object] | None,
) -> None:
    install = _install(tmp_path)
    request_file = install.root / operations.DISPATCH_REQUEST
    taken: list[operations.ServerRequest] = []

    def poll(_seconds: float) -> None:
        """One refresh of the tray while the requester waits."""

        if tray == "older":
            request_file.unlink(missing_ok=True)
            return
        if (request := operations.dispatch_requested(install)) is None:
            return
        taken.append(request)
        if tray == "answers":
            operations.answer_server_request(
                install, request, ok=True, message="started", process_id=99
            )
        elif tray == "fails":
            operations.answer_server_request(install, request, ok=False, message="port is occupied")

    monkeypatch.setattr(operations, "_sleep", poll)
    if tray == "silent":
        monkeypatch.setattr(operations, "HOST_DISPATCH_SECONDS", 0.0)
    if tray == "accepts-only":
        monkeypatch.setattr(operations, "HOST_SERVER_SECONDS", 0.0)

    answer = operations.request_server(install, "restart")

    if expected is None:
        assert answer is None
    else:
        assert answer is not None
        assert {key: answer.get(key) for key in expected} == expected
        assert [request.command for request in taken] == ["restart"]
    if tray == "accepts-only":
        assert answer is not None and "no result within" in answer["message"]
    # Nothing is left for a later tray to run by surprise, and no result file lingers.
    assert not request_file.exists()
    assert list(install.root.glob("host-server-result-*")) == []


def test_tray_consumes_dispatch_requests_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    request = install.root / operations.DISPATCH_REQUEST
    monkeypatch.setattr(operations, "spawn_worker", lambda *_args: pytest.fail("must not spawn"))
    # A request for a finished operation, or no request at all, starts nothing.
    Operation(id="upd_done", phase="completed", message="done").save(install)
    request.write_text('{"schema_version": 1, "operation": "upd_done"}', encoding="utf-8")
    operations.dispatch_requested(install)
    operations.dispatch_requested(install)
    assert not request.exists()
    # An invalid request is removed, so a tray does not fail on it every second.
    for invalid in (
        '{"schema_version": 2}',
        '{"schema_version": 1, "server": "stop", "request": "' + "a" * 32 + '"}',
        '{"schema_version": 1, "server": "start", "request": "../escape"}',
    ):
        request.write_text(invalid, encoding="utf-8")
        with pytest.raises(ApplicationError):
            operations.dispatch_requested(install)
        assert not request.exists()


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
