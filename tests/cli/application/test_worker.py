"""Update-worker recovery tests use only temporary versions and injected seams."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import worker
from cli.application.state import (
    ApplicationError,
    Installation,
    Operation,
    exclusive,
    load_operation,
)


def _install(root: Path, *, shape: str = "server") -> Installation:
    install = Installation(
        root,
        shape,
        None if shape == "desktop-client" else "127.0.0.1",
        None if shape == "desktop-client" else 8420,
        None if shape == "desktop-client" else str((root / "data").resolve()),
    )
    for version_id in ("rel_old", "rel_new"):
        version = root / "versions" / version_id
        version.mkdir(parents=True)
        (version / "release.json").write_text("{}", encoding="utf-8")
    (root / "active-version").write_text("rel_old\n", encoding="ascii")
    return install


def _patch_carry_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cli.application.customize.carry_forward", lambda _install, candidate: candidate
    )
    monkeypatch.setattr(
        "cli.application.customize.finalize_activation", lambda _install, _candidate: None
    )


@pytest.fixture(autouse=True)
def _exact_installed_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        worker.processes,
        "running_server_matches",
        lambda _install, *, version_id=None, verification=False: (
            version_id in {None, "rel_old"} and not verification
        ),
    )


def _ok() -> SimpleNamespace:
    return SimpleNamespace(ok=True, message="ok")


def test_client_only_execution_never_targets_snapshots_or_starts_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path, shape="desktop-client")
    operation = Operation(
        id="upd_client", previous_version="rel_old", package="release.zip", local_package=True
    )
    calls: list[str] = []
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    monkeypatch.setattr(worker.processes, "target", lambda _install: pytest.fail("must not target"))
    monkeypatch.setattr(
        worker.processes, "start", lambda *_args, **_kwargs: pytest.fail("must not start")
    )
    monkeypatch.setattr(
        worker.processes, "stop", lambda *_args, **_kwargs: pytest.fail("must not stop")
    )
    monkeypatch.setattr(
        worker, "_ensure_update_session_snapshot", lambda _target: calls.append("snapshot")
    )
    monkeypatch.setattr(
        worker, "validate_release", lambda *_args, **_kwargs: calls.append("validate")
    )

    worker.execute(install, operation)

    assert operation.phase == "completed"
    assert install.version().name == "rel_new"
    assert calls == ["validate"]


def test_no_restart_prepares_without_changing_the_active_pointer_or_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_prepare",
        previous_version="rel_old",
        restart=False,
        package="release.zip",
        local_package=True,
    )
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    monkeypatch.setattr(worker.processes, "target", lambda _install: pytest.fail("must not target"))

    worker.execute(install, operation)

    assert operation.phase == "prepared"
    assert install.version().name == "rel_old"


@pytest.mark.parametrize("restart", [True, False])
def test_current_version_finishes_without_maintenance_snapshot_or_handoff(
    tmp_path, monkeypatch, restart
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_current",
        previous_version="rel_old",
        package="same.zip",
        restart=restart,
        handoff_ticket="ticket.json",
    )
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(worker, "stage_package", lambda *a, **kw: "rel_old")
    monkeypatch.setattr(worker, "quiesce", lambda *a: pytest.fail("no maintenance or continuation"))
    monkeypatch.setattr(worker.processes, "target", lambda *a: pytest.fail("no server action"))
    worker.execute(install, operation)
    assert operation.phase == "completed"
    assert operation.previous_version == operation.candidate_version == install.version().name


def test_bound_source_update_replaces_download_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(id="upd_source", previous_version="rel_old", restart=False)
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(
        "cli.application.source_updates.read_binding", lambda _install: {"checkout": "source"}
    )
    monkeypatch.setattr(
        "cli.application.source_updates.prepare_update",
        lambda _install, operation_id, **kwargs: (
            "rel_new" if operation_id == "upd_source" else "wrong"
        ),
    )
    monkeypatch.setattr(worker, "download_release", lambda *_args: pytest.fail("must not download"))
    monkeypatch.setattr(
        worker, "stage_package", lambda *_args, **_kwargs: pytest.fail("must not stage")
    )
    monkeypatch.setattr(worker.processes, "target", lambda _install: pytest.fail("must not target"))

    worker.execute(install, operation)

    assert operation.phase == "prepared"
    assert operation.candidate_version == "rel_new"
    assert install.version().name == "rel_old"


def test_stage_failure_leaves_the_prior_active_version_and_marks_the_operation_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(id="upd_bad_stage", previous_version="rel_old")
    operation.save(install)
    monkeypatch.setattr(
        worker,
        "stage_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("hash failed")),
    )

    worker.run(install, operation.id)

    assert install.version().name == "rel_old"
    assert load_operation(install, operation.id).phase == "failed"


def test_busy_server_quiesces_before_stopping_after_waiting_for_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_busy",
        previous_version="rel_old",
        package="release.zip",
        local_package=True,
        handoff_ticket="ticket.json",
    )
    sequence: list[str] = []
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    monkeypatch.setattr(worker.processes, "target", lambda _install: SimpleNamespace())
    monkeypatch.setattr(
        worker,
        "probe_health",
        lambda _target: SimpleNamespace(is_vbot=True, reachable=True),
    )
    ready = iter((False, True))

    def control(_install, method, _operation, **_extra):
        sequence.append(method)
        return {"safe_to_stop": next(ready)} if method == "maintenance_status" else {}

    monkeypatch.setattr(worker, "control", control)
    monkeypatch.setattr(worker, "wait_for_handoff", lambda *_args: "ticket-one")
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: sequence.append("wait"))
    monkeypatch.setattr(worker, "_ensure_update_session_snapshot", lambda _target: _ok())

    def stop(_install):
        sequence.append("stop")
        return _ok()

    monkeypatch.setattr(worker.processes, "stop", stop)

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        assert breakaway is False
        return _ok()

    monkeypatch.setattr(worker.processes, "start", start)
    monkeypatch.setattr(worker, "validate_release", lambda *_args, **_kwargs: None)
    original_transition = Operation.transition

    def record_transition(self: Operation, *args, **kwargs):
        sequence.append(args[1])
        return original_transition(self, *args, **kwargs)

    monkeypatch.setattr(Operation, "transition", record_transition)
    worker.execute(install, operation)

    assert sequence.index("waiting_for_idle") < sequence.index("maintenance_begin")
    assert sequence.index("update_continuation") < sequence.index("maintenance_begin")
    assert sequence.index("wait") < sequence.index("stop")


def test_candidate_failure_rolls_back_only_after_previous_version_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_rollback", previous_version="rel_old", package="release.zip", local_package=True
    )
    starts: list[tuple[str | None, bool]] = []
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    monkeypatch.setattr(worker.processes, "target", lambda _install: SimpleNamespace())
    monkeypatch.setattr(
        worker,
        "probe_health",
        lambda _target: SimpleNamespace(is_vbot=True, reachable=True),
    )
    monkeypatch.setattr(worker, "quiesce", lambda *_args: None)
    monkeypatch.setattr(worker, "_ensure_update_session_snapshot", lambda _target: _ok())
    monkeypatch.setattr(worker.processes, "stop", lambda _install: _ok())

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        assert breakaway is False
        starts.append((version_id, verification))
        return SimpleNamespace(ok=version_id != "rel_new", message="candidate failed")

    monkeypatch.setattr(worker.processes, "start", start)
    worker.execute(install, operation)

    assert operation.phase == "rolled_back"
    assert operation.error == "candidate failed"
    assert starts == [("rel_new", True), ("rel_old", True), ("rel_old", False)]
    assert install.version().name == "rel_old"


def test_post_pointer_normal_start_failure_needs_attention_without_data_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_start_failure",
        previous_version="rel_old",
        package="release.zip",
        local_package=True,
    )
    operation.save(install)
    starts: list[tuple[str | None, bool]] = []
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    monkeypatch.setattr(worker.processes, "target", lambda _install: SimpleNamespace())
    monkeypatch.setattr(
        worker,
        "probe_health",
        lambda _target: SimpleNamespace(is_vbot=True, reachable=True),
    )
    monkeypatch.setattr(worker, "quiesce", lambda *_args: None)
    monkeypatch.setattr(worker, "_ensure_update_session_snapshot", lambda _target: _ok())
    monkeypatch.setattr(worker.processes, "stop", lambda _install: _ok())

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        assert breakaway is False
        starts.append((version_id, verification))
        return SimpleNamespace(ok=verification, message="normal startup failed")

    monkeypatch.setattr(worker.processes, "start", start)
    monkeypatch.setattr(worker, "validate_release", lambda *_args, **_kwargs: None)
    worker.run(install, operation.id)

    assert load_operation(install, operation.id).phase == "needs_attention"
    assert install.version().name == "rel_new"
    assert ("rel_old", False) not in starts


def test_interrupted_activation_and_terminal_operations_are_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    install.activate("rel_new")
    interrupted = Operation(
        id="upd_interrupted",
        phase="activating",
        previous_version="rel_old",
        candidate_version="rel_new",
    )
    assert worker.recover_interrupted(install, interrupted) is True
    assert interrupted.phase == "needs_attention"

    terminal = Operation(id="upd_terminal", phase="completed")
    terminal.save(install)
    monkeypatch.setattr(
        worker, "execute", lambda *_args: pytest.fail("must not replay terminal operation")
    )
    worker.run(install, terminal.id)


@pytest.mark.parametrize("survived", [True, False])
def test_interrupted_stop_recovers_old_server_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, survived: bool
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_interrupted_stop",
        phase="stopping",
        previous_version="rel_old",
        candidate_version="rel_new",
        server_was_running=True,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        worker.processes,
        "running_server_matches",
        lambda _install, *, version_id=None, verification=False: (
            survived and version_id == "rel_old" and not verification
        ),
    )

    def record_control(_install, method, _operation, **_extra):
        calls.append(method)
        return {}

    monkeypatch.setattr(worker, "control", record_control)

    def start(_install, *, version_id=None, breakaway=True):
        assert not survived
        assert version_id == "rel_old" and breakaway is False
        calls.append("restart")
        return _ok()

    monkeypatch.setattr(worker.processes, "start", start)

    assert worker.recover_interrupted(install, operation) is True

    assert operation.phase == "rolled_back"
    assert calls == (["maintenance_end"] if survived else ["restart"])


def test_worker_holds_operation_lock_for_the_entire_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(id="upd_locked", previous_version="rel_old")
    operation.save(install)
    entered = threading.Event()
    release = threading.Event()

    def blocked_execute(_install: Installation, _operation: Operation) -> None:
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(worker, "execute", blocked_execute)
    thread = threading.Thread(target=worker.run, args=(install, operation.id))
    thread.start()
    assert entered.wait(2)
    try:
        assert load_operation(install, operation.id).id == operation.id
        with (
            pytest.raises(ApplicationError, match="Another application operation"),
            exclusive(install.root, "operation"),
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
