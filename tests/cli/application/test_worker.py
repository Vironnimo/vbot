"""Update-worker recovery tests use only temporary versions and injected seams."""

from __future__ import annotations

import threading
from contextlib import ExitStack
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
from core.database import (
    DatabaseUnavailableError,
    begin_maintenance,
    create_update_snapshot,
    find_update_snapshot,
    open_database,
    read_incident,
    read_maintenance,
    write_bootstrap_marker,
)
from core.database.snapshots import snapshot_root
from core.utils.server_control import server_control_claim
from tests.core.database.database_test_support import add_note, notes_spec, stored_bodies

_THREAD_COORDINATION_TIMEOUT_SECONDS = 10.0


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
        (version / "runtime").mkdir()
        (version / "runtime" / "vBot.GUI.exe").write_bytes(version_id.encode())
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


_SETTINGS = '{"format_version": 1, "theme": "dark"}\n'


def _write_document(data_dir: Path, relative: str, text: str) -> None:
    path = data_dir.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _server_data(install: Installation) -> Path:
    """The stopped server's data: one registered database and one JSON document."""
    assert install.server_data_directory is not None
    data_dir = Path(install.server_data_directory)
    data_dir.mkdir(parents=True, exist_ok=True)
    write_bootstrap_marker(data_dir)
    database = open_database(notes_spec(data_dir))
    add_note(database, "before the update")
    database.close()
    _write_document(data_dir, "settings.json", _SETTINGS)
    return data_dir


def _candidate_writes(data_dir: Path) -> None:
    database = open_database(notes_spec(data_dir))
    add_note(database, "written by the candidate")
    database.close()
    _write_document(data_dir, "settings.json", '{"format_version": 2}\n')
    _write_document(data_dir, "mcp/connections.json", '{"format_version": 1}\n')


def _target_data(monkeypatch: pytest.MonkeyPatch, install: Installation) -> None:
    assert install.server_data_directory is not None
    data_dir = Path(install.server_data_directory)
    monkeypatch.setattr(
        worker.processes, "target", lambda _install: SimpleNamespace(data_dir=data_dir)
    )


def _patch_server_update(monkeypatch: pytest.MonkeyPatch, install: Installation) -> None:
    """A running previous server that stops cleanly; the test decides every start."""
    _patch_carry_forward(monkeypatch)
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    _target_data(monkeypatch, install)
    monkeypatch.setattr(
        worker,
        "probe_health",
        lambda _target: SimpleNamespace(is_vbot=True, reachable=True),
    )
    monkeypatch.setattr(worker, "quiesce", lambda *_args: None)
    monkeypatch.setattr(worker.processes, "stop", lambda _install: _ok())


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
        worker, "create_update_snapshot", lambda *_args, **_kwargs: calls.append("snapshot")
    )
    monkeypatch.setattr(
        worker, "validate_release", lambda *_args, **_kwargs: calls.append("validate")
    )

    worker.execute(install, operation)

    assert operation.phase == "completed"
    assert install.version().name == "rel_new"
    assert calls == ["validate"]
    assert (install.root / "vBot.GUI.exe").read_bytes() == b"rel_new"


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
    assert not (install.root / "vBot.GUI.exe").exists()


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
    assert (install.root / "vBot.GUI.exe").read_bytes() == b"rel_old"


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
    _target_data(monkeypatch, install)
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
    monkeypatch.setattr(
        worker,
        "create_update_snapshot",
        lambda *_args, **_kwargs: sequence.append("snapshot"),
    )

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
    # The snapshot is taken only once no server can write any more.
    assert sequence.index("stop") < sequence.index("snapshot")


def test_candidate_failure_restores_the_update_snapshot_before_the_previous_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    data_dir = _server_data(install)
    operation = Operation(
        id="upd_rollback", previous_version="rel_old", package="release.zip", local_package=True
    )
    starts: list[tuple[str | None, bool]] = []
    _patch_server_update(monkeypatch, install)

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        assert breakaway is False
        starts.append((version_id, verification))
        if version_id == "rel_new":
            _candidate_writes(data_dir)
            return SimpleNamespace(ok=False, message="candidate failed")
        # The previous version is probed and started only on the restored data.
        assert stored_bodies(notes_spec(data_dir)) == ["before the update"]
        assert (data_dir / "settings.json").read_text(encoding="utf-8") == _SETTINGS
        return _ok()

    monkeypatch.setattr(worker.processes, "start", start)
    worker.execute(install, operation)

    snapshot_id = find_update_snapshot(data_dir, "upd_rollback")
    assert snapshot_id is not None
    assert operation.phase == "rolled_back"
    assert operation.error == "candidate failed"
    assert f"restored from snapshot {snapshot_id}" in operation.message
    assert starts == [("rel_new", True), ("rel_old", True), ("rel_old", False)]
    assert install.version().name == "rel_old"
    assert not (data_dir / "mcp" / "connections.json").exists()
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["restored_snapshot_id"] == snapshot_id


def test_a_candidate_that_changed_nothing_needs_no_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    data_dir = _server_data(install)
    operation = Operation(
        id="upd_untouched", previous_version="rel_old", package="release.zip", local_package=True
    )
    _patch_server_update(monkeypatch, install)
    monkeypatch.setattr(
        worker, "restore_update_snapshot", lambda *_a, **_k: pytest.fail("nothing to restore")
    )
    monkeypatch.setattr(
        worker.processes,
        "start",
        lambda _install, *, version_id=None, verification=False, breakaway=True: SimpleNamespace(
            ok=version_id != "rel_new", message="port occupied"
        ),
    )

    worker.execute(install, operation)

    assert operation.phase == "rolled_back"
    assert "left the data unchanged" in operation.message
    assert read_incident(data_dir, "notes") is None


def test_a_server_running_on_the_data_after_the_failure_blocks_the_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    data_dir = _server_data(install)
    operation = Operation(
        id="upd_foreign", previous_version="rel_old", package="release.zip", local_package=True
    )
    _patch_server_update(monkeypatch, install)
    monkeypatch.setattr(worker, "_CLAIM_SETTLE_SECONDS", 0.0)
    with ExitStack() as foreign:

        def start(_install, *, version_id=None, verification=False, breakaway=True):
            if version_id == "rel_new":
                _candidate_writes(data_dir)
                # Another server on another port opened the same data directory.
                foreign.enter_context(server_control_claim(data_dir, 9999))
                return SimpleNamespace(ok=False, message="candidate failed")
            return _ok()

        monkeypatch.setattr(worker.processes, "start", start)
        worker.execute(install, operation)

    assert operation.phase == "rolled_back"
    assert "was not restored automatically" in operation.message
    assert "port 9999" in operation.message
    assert "written by the candidate" in stored_bodies(notes_spec(data_dir))
    assert read_incident(data_dir, "notes") is None


def test_a_snapshot_that_no_longer_verifies_is_not_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    data_dir = _server_data(install)
    operation = Operation(
        id="upd_tampered", previous_version="rel_old", package="release.zip", local_package=True
    )
    _patch_server_update(monkeypatch, install)

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        if version_id == "rel_new":
            _candidate_writes(data_dir)
            snapshot_id = find_update_snapshot(data_dir, "upd_tampered")
            assert snapshot_id is not None
            copy = snapshot_root(data_dir) / snapshot_id / "documents" / "settings.json"
            copy.write_bytes(copy.read_bytes().replace(b"dark", b"pale"))
            return SimpleNamespace(ok=False, message="candidate failed")
        return _ok()

    monkeypatch.setattr(worker.processes, "start", start)
    worker.execute(install, operation)

    assert operation.phase == "rolled_back"
    assert "was not restored automatically" in operation.message
    assert "written by the candidate" in stored_bodies(notes_spec(data_dir))
    assert read_maintenance(data_dir) is None


def test_an_interrupted_restore_needs_attention_with_the_repeat_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    data_dir = _server_data(install)
    operation = Operation(
        id="upd_restore_fails",
        previous_version="rel_old",
        package="release.zip",
        local_package=True,
    )
    operation.save(install)
    starts: list[tuple[str | None, bool]] = []
    _patch_server_update(monkeypatch, install)

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        starts.append((version_id, verification))
        if version_id == "rel_new":
            _candidate_writes(data_dir)
            return SimpleNamespace(ok=False, message="candidate failed")
        return _ok()

    def interrupted_restore(*_args, **_kwargs):
        raise DatabaseUnavailableError("injected restore failure")

    monkeypatch.setattr(worker.processes, "start", start)
    monkeypatch.setattr(worker, "restore_update_snapshot", interrupted_restore)
    monkeypatch.setattr(worker, "control", lambda *_args, **_kwargs: {})
    worker.run(install, operation.id)

    saved = load_operation(install, operation.id)
    snapshot_id = find_update_snapshot(data_dir, "upd_restore_fails")
    assert saved.phase == "needs_attention"
    assert saved.error is not None
    assert "candidate failed" in saved.error
    assert f"vbot data-store snapshot restore {snapshot_id} --all --yes" in saved.error
    assert starts == [("rel_new", True)]
    assert install.version().name == "rel_old"


def test_a_failed_snapshot_after_the_stop_restarts_the_previous_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_no_snapshot", previous_version="rel_old", package="release.zip", local_package=True
    )
    starts: list[tuple[str | None, bool]] = []
    _patch_server_update(monkeypatch, install)

    def failed_snapshot(*_args, **_kwargs):
        raise DatabaseUnavailableError("insufficient snapshot reserve")

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        starts.append((version_id, verification))
        return _ok()

    monkeypatch.setattr(worker, "create_update_snapshot", failed_snapshot)
    monkeypatch.setattr(worker.processes, "start", start)
    worker.execute(install, operation)

    assert operation.phase == "failed"
    assert "insufficient snapshot reserve" in operation.message
    assert "previous version is still active" in operation.message
    assert starts == [("rel_old", False)]
    assert install.version().name == "rel_old"


def test_an_unverifiable_server_check_keeps_the_previous_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    _server_data(install)
    operation = Operation(
        id="upd_probe", previous_version="rel_old", package="release.zip", local_package=True
    )
    starts: list[tuple[str | None, bool]] = []
    _patch_server_update(monkeypatch, install)

    def unreadable(_data_dir):
        raise PermissionError("access denied")

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        starts.append((version_id, verification))
        return _ok()

    monkeypatch.setattr(worker, "live_server_ports", unreadable)
    monkeypatch.setattr(
        worker, "create_update_snapshot", lambda *_a, **_k: pytest.fail("no snapshot")
    )
    monkeypatch.setattr(worker.processes, "start", start)
    worker.execute(install, operation)

    assert operation.phase == "failed"
    assert "could not be checked" in operation.message
    assert starts == [("rel_old", False)]
    assert install.version().name == "rel_old"


def test_a_write_after_the_snapshot_keeps_the_candidate_from_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    data_dir = _server_data(install)
    operation = Operation(
        id="upd_fenced", previous_version="rel_old", package="release.zip", local_package=True
    )
    starts: list[tuple[str | None, bool]] = []
    _patch_server_update(monkeypatch, install)
    real_snapshot = worker.create_update_snapshot

    def snapshot_then_foreign_write(*args, **kwargs):
        taken = real_snapshot(*args, **kwargs)
        _write_document(data_dir, "settings.json", '{"format_version": 1, "theme": "light"}\n')
        return taken

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        starts.append((version_id, verification))
        return _ok()

    monkeypatch.setattr(worker, "create_update_snapshot", snapshot_then_foreign_write)
    monkeypatch.setattr(worker.processes, "start", start)
    worker.execute(install, operation)

    assert operation.phase == "failed"
    assert "settings.json changed" in operation.message
    assert starts == [("rel_old", False)]
    assert install.version().name == "rel_old"
    assert "light" in (data_dir / "settings.json").read_text(encoding="utf-8")


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
    data_dir = _server_data(install)
    starts: list[tuple[str | None, bool]] = []
    _patch_server_update(monkeypatch, install)
    monkeypatch.setattr(worker, "control", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        worker,
        "restore_update_snapshot",
        lambda *_args, **_kwargs: pytest.fail("a verified candidate's snapshot is stale"),
    )

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        assert breakaway is False
        starts.append((version_id, verification))
        if version_id == "rel_new" and verification:
            _candidate_writes(data_dir)
        return SimpleNamespace(ok=verification, message="normal startup failed")

    monkeypatch.setattr(worker.processes, "start", start)
    monkeypatch.setattr(worker, "validate_release", lambda *_args, **_kwargs: None)
    worker.run(install, operation.id)

    assert load_operation(install, operation.id).phase == "needs_attention"
    assert install.version().name == "rel_new"
    assert ("rel_old", False) not in starts
    assert "written by the candidate" in stored_bodies(notes_spec(data_dir))
    assert read_incident(data_dir, "notes") is None


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


def test_recovery_after_an_interrupted_data_restore_never_starts_a_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    data_dir = _server_data(install)
    _target_data(monkeypatch, install)
    snapshot = create_update_snapshot(data_dir, operation_id="upd_lost")
    assert snapshot is not None
    begin_maintenance(data_dir, "restore")
    operation = Operation(
        id="upd_lost",
        phase="activating",
        previous_version="rel_old",
        candidate_version="rel_new",
        server_was_running=True,
    )
    monkeypatch.setattr(
        worker.processes, "start", lambda *_args, **_kwargs: pytest.fail("must not start")
    )

    assert worker.recover_interrupted(install, operation) is True

    assert operation.phase == "needs_attention"
    assert (
        f"vbot data-store snapshot restore {snapshot.snapshot_id} --all --yes" in operation.message
    )


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
        assert release.wait(_THREAD_COORDINATION_TIMEOUT_SECONDS)

    monkeypatch.setattr(worker, "execute", blocked_execute)
    thread = threading.Thread(target=worker.run, args=(install, operation.id))
    thread.start()
    assert entered.wait(_THREAD_COORDINATION_TIMEOUT_SECONDS)
    try:
        assert load_operation(install, operation.id).id == operation.id
        with (
            pytest.raises(ApplicationError, match="Another application operation"),
            exclusive(install.root, "operation"),
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=_THREAD_COORDINATION_TIMEOUT_SECONDS)
    assert not thread.is_alive()
