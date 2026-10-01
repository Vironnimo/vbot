"""Update-worker recovery tests use only temporary versions and injected seams."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
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
    canonical_database_path,
    create_update_snapshot,
    find_update_snapshot,
    open_database,
    read_incident,
    read_maintenance,
    write_bootstrap_marker,
)
from core.database.snapshots import SNAPSHOT_MANIFEST_NAME, snapshot_root
from core.model_tasks.decision_store import decision_database_spec
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
    (root / "active-version").write_text("rel_old\n", encoding="ascii")
    return install


@pytest.fixture(autouse=True)
def _exact_installed_server(monkeypatch: pytest.MonkeyPatch):
    """The previous version's normal server runs and answers; no other server exists."""
    monkeypatch.setattr(
        worker.processes,
        "server_state",
        lambda _install, *, version_id=None, verification=False: (
            "running" if version_id in {None, "rel_old"} and not verification else "absent"
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
    _write_document(data_dir, "extension-data/mcp/connections.json", '{"format_version": 1}\n')


def _target_data(monkeypatch: pytest.MonkeyPatch, install: Installation) -> None:
    assert install.server_data_directory is not None
    data_dir = Path(install.server_data_directory)
    monkeypatch.setattr(
        worker.processes, "target", lambda _install: SimpleNamespace(data_dir=data_dir)
    )


def _patch_server_update(monkeypatch: pytest.MonkeyPatch, install: Installation) -> None:
    """A running previous server that stops cleanly; the test decides every start."""
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    _target_data(monkeypatch, install)
    monkeypatch.setattr(worker, "quiesce", lambda *_args: None)
    monkeypatch.setattr(worker.processes, "stop", lambda _install, **_kwargs: _ok())


def test_client_only_execution_never_targets_snapshots_or_starts_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path, shape="desktop-client")
    operation = Operation(
        id="upd_client", previous_version="rel_old", package="release.zip", local_package=True
    )
    calls: list[str] = []
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
    monkeypatch.setattr(worker, "stage_package", lambda *a, **kw: "rel_old")
    monkeypatch.setattr(worker, "quiesce", lambda *a: pytest.fail("no maintenance or continuation"))
    monkeypatch.setattr(worker.processes, "target", lambda *a: pytest.fail("no server action"))
    worker.execute(install, operation)
    assert operation.phase == "completed"
    assert operation.previous_version == operation.candidate_version == install.version().name


def test_a_channel_publishing_the_active_version_completes_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(id="upd_current", previous_version="rel_old")
    monkeypatch.setattr(worker, "download_release", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker, "stage_package", lambda *_args, **_kwargs: pytest.fail("must not stage")
    )
    monkeypatch.setattr(worker, "quiesce", lambda *a: pytest.fail("no maintenance"))
    monkeypatch.setattr(worker.processes, "target", lambda _install: pytest.fail("must not target"))

    worker.execute(install, operation)

    assert operation.phase == "completed"
    assert operation.candidate_version == "rel_old"
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
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    _target_data(monkeypatch, install)
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

    def stop(_install, **_kwargs):
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


@pytest.mark.parametrize("state", ["unresponsive", "foreign"])
def test_a_server_the_update_cannot_drain_fails_it_before_anything_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_unclear", previous_version="rel_old", package="release.zip", local_package=True
    )
    operation.save(install)
    calls: list[str] = []
    monkeypatch.setattr(worker, "stage_package", lambda *_args, **_kwargs: "rel_new")
    _target_data(monkeypatch, install)
    monkeypatch.setattr(worker.processes, "server_state", lambda _install, **_kwargs: state)
    monkeypatch.setattr(worker, "quiesce", lambda *_args: pytest.fail("must not drain"))
    monkeypatch.setattr(worker.processes, "stop", lambda *_a, **_k: pytest.fail("must not stop"))
    monkeypatch.setattr(worker.processes, "start", lambda *_a, **_k: pytest.fail("must not start"))
    monkeypatch.setattr(
        worker, "create_update_snapshot", lambda *_a, **_k: pytest.fail("must not snapshot")
    )

    def control(_install, method, _operation, **_extra):
        calls.append(method)
        return {}

    monkeypatch.setattr(worker, "control", control)

    worker.run(install, operation.id)

    saved = load_operation(install, operation.id)
    assert saved.phase == "failed"
    assert saved.error
    assert install.version().name == "rel_old"
    assert "maintenance_begin" not in calls


def test_a_stopped_server_is_neither_stopped_nor_started_by_the_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    _server_data(install)
    operation = Operation(
        id="upd_stopped", previous_version="rel_old", package="release.zip", local_package=True
    )
    calls: list[object] = []
    _patch_server_update(monkeypatch, install)
    monkeypatch.setattr(worker.processes, "server_state", lambda _install, **_kwargs: "absent")
    monkeypatch.setattr(worker, "quiesce", lambda *_args: pytest.fail("nothing to drain"))

    def stop(_install, **_kwargs):
        calls.append("stop")
        return _ok()

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        calls.append((version_id, verification))
        return _ok()

    monkeypatch.setattr(worker.processes, "stop", stop)
    monkeypatch.setattr(worker.processes, "start", start)
    monkeypatch.setattr(worker, "validate_release", lambda *_args, **_kwargs: None)

    worker.execute(install, operation)

    assert operation.phase == "completed"
    assert operation.server_was_running is False
    assert install.version().name == "rel_new"
    # Only the candidate's verification server is started and stopped again.
    assert calls == [("rel_new", True), "stop"]


def test_candidate_failure_restores_the_update_snapshot_before_the_previous_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
    with caplog.at_level(logging.DEBUG, logger="vbot.application.update"):
        worker.execute(install, operation)

    snapshot_id = find_update_snapshot(data_dir, "upd_rollback")
    assert snapshot_id is not None
    assert operation.phase == "rolled_back"
    # The operator log names every phase change once, and the rollback with its
    # restored snapshot as warnings.
    logged = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "vbot.application.update" and record.levelno >= logging.INFO
    ]
    phases = [
        (level, message.split(" to=")[1].split()[0])
        for level, message in logged
        if " to=" in message
    ]
    assert phases == [
        (logging.INFO, "preparing"),
        (logging.INFO, "stopping"),
        (logging.INFO, "activating"),
        (logging.WARNING, "rolled_back"),
    ]
    assert all("operation=upd_rollback" in message for _level, message in logged)
    assert "candidate_version=rel_new" in logged[-1][1] and "candidate failed" in logged[-1][1]
    assert any(
        level == logging.WARNING and snapshot_id in message and " to=" not in message
        for level, message in logged
    )
    assert operation.error == "candidate failed"
    assert f"restored from snapshot {snapshot_id}" in operation.message
    assert starts == [("rel_new", True), ("rel_old", True), ("rel_old", False)]
    assert install.version().name == "rel_old"
    assert not (data_dir / "extension-data" / "mcp" / "connections.json").exists()
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


def _foreign_server_opens_the_data(data_dir: Path, stack: ExitStack) -> dict[str, int]:
    # Another server on another port opened the same data directory.
    stack.enter_context(server_control_claim(data_dir, 9999))
    return {}


def _tamper_with_a_snapshot_document(data_dir: Path, _stack: ExitStack) -> dict[str, int]:
    snapshot_id = find_update_snapshot(data_dir, "upd_refused")
    assert snapshot_id is not None
    copy = snapshot_root(data_dir) / snapshot_id / "documents" / "settings.json"
    copy.write_bytes(copy.read_bytes().replace(b"dark", b"pale"))
    return {}


def _alter_a_recorded_owner_fact(data_dir: Path, _stack: ExitStack) -> dict[str, int]:
    snapshot_id = find_update_snapshot(data_dir, "upd_refused")
    assert snapshot_id is not None
    manifest_path = snapshot_root(data_dir) / snapshot_id / SNAPSHOT_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    facts = manifest["members"]["decisions"]["facts"]
    recorded = dict(facts)
    # A recorded fact the copy disagrees with is caught only by the declaration.
    facts["experiment_count"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return recorded


@pytest.mark.parametrize(
    ("interfere", "reason", "recorded_facts"),
    [
        pytest.param(_foreign_server_opens_the_data, "port 9999", {}, id="foreign-server"),
        pytest.param(_tamper_with_a_snapshot_document, "", {}, id="tampered-document"),
        # The snapshot records and verifies the facts of core owners.
        pytest.param(
            _alter_a_recorded_owner_fact,
            "",
            {"evaluation_count": 0, "experiment_count": 0},
            id="altered-owner-fact",
        ),
    ],
)
def test_a_restore_that_cannot_be_proven_safe_keeps_the_candidate_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interfere: Callable[[Path, ExitStack], dict[str, int]],
    reason: str,
    recorded_facts: dict[str, int],
) -> None:
    install = _install(tmp_path)
    data_dir = _server_data(install)
    open_database(decision_database_spec(canonical_database_path(data_dir, "decisions"))).close()
    operation = Operation(
        id="upd_refused", previous_version="rel_old", package="release.zip", local_package=True
    )
    _patch_server_update(monkeypatch, install)
    monkeypatch.setattr(worker, "_CLAIM_SETTLE_SECONDS", 0.0)
    recorded: dict[str, int] = {}
    with ExitStack() as stack:

        def start(_install, *, version_id=None, verification=False, breakaway=True):
            if version_id == "rel_new":
                _candidate_writes(data_dir)
                recorded.update(interfere(data_dir, stack))
                return SimpleNamespace(ok=False, message="candidate failed")
            return _ok()

        monkeypatch.setattr(worker.processes, "start", start)
        worker.execute(install, operation)

    assert recorded == recorded_facts
    assert operation.phase == "rolled_back"
    assert "was not restored automatically" in operation.message
    assert reason in operation.message
    assert "written by the candidate" in stored_bodies(notes_spec(data_dir))
    assert read_incident(data_dir, "notes") is None
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


def _snapshot_fails(monkeypatch: pytest.MonkeyPatch, _data_dir: Path) -> None:
    def failed_snapshot(*_args, **_kwargs):
        raise DatabaseUnavailableError("insufficient snapshot reserve")

    monkeypatch.setattr(worker, "create_update_snapshot", failed_snapshot)


def _server_claims_cannot_be_read(monkeypatch: pytest.MonkeyPatch, _data_dir: Path) -> None:
    def unreadable(_data_dir):
        raise PermissionError("access denied")

    monkeypatch.setattr(worker, "live_server_ports", unreadable)
    monkeypatch.setattr(
        worker, "create_update_snapshot", lambda *_a, **_k: pytest.fail("no snapshot")
    )


def _data_changes_after_the_snapshot(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    real_snapshot = worker.create_update_snapshot

    def snapshot_then_foreign_write(*args, **kwargs):
        taken = real_snapshot(*args, **kwargs)
        _write_document(data_dir, "settings.json", '{"format_version": 1, "theme": "light"}\n')
        return taken

    monkeypatch.setattr(worker, "create_update_snapshot", snapshot_then_foreign_write)


@pytest.mark.parametrize(
    ("arrange", "reasons", "theme"),
    [
        pytest.param(
            _snapshot_fails,
            ["insufficient snapshot reserve", "previous version is still active"],
            "dark",
            id="snapshot-fails",
        ),
        pytest.param(
            _server_claims_cannot_be_read, ["could not be checked"], "dark", id="claims-unknown"
        ),
        pytest.param(
            _data_changes_after_the_snapshot, ["settings.json changed"], "light", id="data-changed"
        ),
    ],
)
def test_a_failed_data_fence_restarts_the_previous_version_without_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[pytest.MonkeyPatch, Path], None],
    reasons: list[str],
    theme: str,
) -> None:
    install = _install(tmp_path)
    data_dir = _server_data(install)
    operation = Operation(
        id="upd_fenced", previous_version="rel_old", package="release.zip", local_package=True
    )
    starts: list[tuple[str | None, bool]] = []
    _patch_server_update(monkeypatch, install)
    arrange(monkeypatch, data_dir)

    def start(_install, *, version_id=None, verification=False, breakaway=True):
        starts.append((version_id, verification))
        return _ok()

    monkeypatch.setattr(worker.processes, "start", start)
    worker.execute(install, operation)

    assert operation.phase == "failed"
    assert all(reason in operation.message for reason in reasons)
    assert starts == [("rel_old", False)]
    assert install.version().name == "rel_old"
    # The fence never rolls data back, not even a foreign write.
    assert f'"theme": "{theme}"' in (data_dir / "settings.json").read_text(encoding="utf-8")


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


@pytest.mark.parametrize(
    ("candidate", "previous", "calls", "phase"),
    [
        pytest.param("absent", "running", ["maintenance_end"], "rolled_back", id="old-survived"),
        # A busy old server is released like an answering one, never duplicated.
        pytest.param("absent", "unresponsive", ["maintenance_end"], "rolled_back", id="old-busy"),
        pytest.param("absent", "absent", ["restart"], "rolled_back", id="old-stopped"),
        pytest.param(
            "running", "absent", ["stop", "restart"], "rolled_back", id="candidate-verifying"
        ),
        pytest.param("unresponsive", "foreign", [], "needs_attention", id="candidate-busy"),
    ],
)
def test_interrupted_stop_recovers_old_server_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    previous: str,
    calls: list[str],
    phase: str,
):
    install = _install(tmp_path)
    operation = Operation(
        id="upd_interrupted_stop",
        phase="stopping",
        previous_version="rel_old",
        candidate_version="rel_new",
        server_was_running=True,
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        worker.processes,
        "server_state",
        lambda _install, *, version_id=None, verification=False: (
            candidate if (version_id, verification) == ("rel_new", True) else previous
        ),
    )

    def record_control(_install, method, _operation, **_extra):
        recorded.append(method)
        return {}

    monkeypatch.setattr(worker, "control", record_control)

    def stop(_install, **_kwargs):
        recorded.append("stop")
        return _ok()

    def start(_install, *, version_id=None, breakaway=True):
        assert version_id == "rel_old" and breakaway is False
        recorded.append("restart")
        return _ok()

    monkeypatch.setattr(worker.processes, "stop", stop)
    monkeypatch.setattr(worker.processes, "start", start)

    assert worker.recover_interrupted(install, operation) is True

    assert operation.phase == phase
    assert recorded == calls


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


def test_a_completed_update_deletes_retired_versions_after_releasing_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    operation = Operation(id="upd_cleanup", previous_version="rel_old")
    operation.save(install)
    retired = [tmp_path / "staging" / "retired-versions-rel_older"]
    events: list[str] = []

    def completed(_install: Installation, current: Operation) -> None:
        current.transition(install, "completed", "Application update completed")

    def lock_state() -> str:
        try:
            with exclusive(install.root, "operation"):
                return "free"
        except ApplicationError:
            return "held"

    def retire(_install: Installation) -> list[Path]:
        events.append(f"retire:{lock_state()}")
        return retired

    def remove(paths: list[Path]) -> None:
        events.append(f"remove:{lock_state()}:{[path.name for path in paths]}")
        assert load_operation(install, operation.id).phase == "completed"

    monkeypatch.setattr(worker, "execute", completed)
    monkeypatch.setattr(worker, "retire_unneeded", retire)
    monkeypatch.setattr(worker, "remove_retired", remove)

    worker.run(install, operation.id)

    assert events == ["retire:held", "remove:free:['retired-versions-rel_older']"]


@pytest.mark.parametrize("outcome", ["prepared", "rolled_back", "failed", "raised"])
def test_an_update_that_did_not_complete_retires_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
):
    install = _install(tmp_path, shape="desktop-client")
    operation = Operation(id="upd_kept", previous_version="rel_old")
    operation.save(install)

    def finish(_install: Installation, current: Operation) -> None:
        if outcome == "raised":
            raise ApplicationError("preparation failed")
        current.transition(install, outcome, "Not activated")

    def retire(_install: Installation) -> list[Path]:
        raise AssertionError("only a completed update retires versions")

    monkeypatch.setattr(worker, "execute", finish)
    monkeypatch.setattr(worker, "retire_unneeded", retire)

    worker.run(install, operation.id)

    assert load_operation(install, operation.id).phase == (
        "failed" if outcome == "raised" else outcome
    )
    assert {path.name for path in (tmp_path / "versions").iterdir()} == {"rel_old", "rel_new"}
