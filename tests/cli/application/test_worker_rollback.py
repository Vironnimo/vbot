"""A user-selected return keeps live data or restores exactly the approved checkpoint."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import operations, rollback, worker
from cli.application.state import Operation, load_operation
from core.database import DatabaseFormatError, Migration, open_database, read_marker
from core.database.snapshots import create_data_snapshot, read_manifest, snapshot_root
from tests.cli.application.test_worker import (
    _install,
    _ok,
    _patch_server_update,
    _server_data,
)
from tests.core.database.database_test_support import add_note, notes_spec, stored_bodies


@pytest.fixture
def switched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install = _install(tmp_path)
    for version in ("rel_old", "rel_new"):
        (install.version(version) / "release.json").write_text(
            json.dumps({"version_switch_protocol": 1}), encoding="utf-8"
        )
    data_dir = _server_data(install)
    _patch_server_update(monkeypatch, install)
    monkeypatch.setattr(worker.processes, "server_state", lambda *_a, **_kw: "running")
    monkeypatch.setattr(worker.processes, "start", lambda *_a, **_kw: _ok())
    monkeypatch.setattr(worker, "validate_release", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        rollback, "validate_release", lambda *_a, **_kw: {"version_switch_protocol": 1}
    )
    monkeypatch.setattr(worker, "retire_unneeded", lambda *_a: [])
    initial = Operation(
        id="upd_first", previous_version="rel_old", package="package.zip", local_package=True
    )
    worker.execute(install, initial)
    assert initial.phase == "completed"
    assert initial.data_snapshot is not None
    monkeypatch.setattr(worker, "stage_package", lambda *_a, **_kw: "rel_old")
    return install, data_dir, initial


def _request(install, point: Operation, *, restore: bool = False) -> Operation:
    operation = Operation(
        id="upd_back",
        previous_version="rel_new",
        package=str(install.version("rel_old")),
        local_package=True,
        rollback=True,
        rollback_source=point.id,
        restore_snapshot=point.data_snapshot if restore else None,
    )
    operation.save(install)
    return operation


@pytest.mark.parametrize(
    "state, prior_outcome",
    [
        ("running", "completed"),
        ("unresponsive", "completed"),
        ("absent", "completed"),
        ("absent", "needs_attention"),
    ],
)
def test_return_keeps_current_data_and_works_without_a_healthy_server(
    switched, monkeypatch, state, prior_outcome
):
    install, data_dir, point = switched
    point.phase = prior_outcome
    point.save(install)
    database = open_database(notes_spec(data_dir))
    add_note(database, "after the update")
    database.close()
    monkeypatch.setattr(worker.processes, "server_state", lambda *_a, **_kw: state)
    starts: list[bool] = []
    stops: list[None] = []
    monkeypatch.setattr(
        worker.processes,
        "start",
        lambda *_a, verification=False, **_kw: starts.append(verification) or _ok(),
    )
    monkeypatch.setattr(worker.processes, "stop", lambda *_a, **_kw: stops.append(None) or _ok())
    operation = _request(install, point)
    worker.run(install, operation.id)
    result = load_operation(install, operation.id)
    assert result.phase == "completed"
    assert install.version().name == "rel_old"
    assert stored_bodies(notes_spec(data_dir)) == ["before the update", "after the update"]
    assert starts == (
        [True] if state == "absent" and prior_outcome == "completed" else [True, False]
    )
    assert len(stops) == (1 if state == "absent" else 2)
    # The next return is now to rel_new with its current, not original, data.
    assert rollback.previous_update(install).id == result.id
    pins = json.loads((snapshot_root(data_dir) / "retention.json").read_text())
    expected_pins = {result.id: result.data_snapshot}
    if prior_outcome == "needs_attention":
        expected_pins[point.id] = point.data_snapshot
    assert pins["owners"] == expected_pins


def _newer_data(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    spec = notes_spec(data_dir, migrations=(Migration("notes.new-format", breaks_older=True),))
    database = open_database(spec)
    add_note(database, "after the update")
    database.close()
    extra = notes_spec(data_dir, name="tasks")
    database = open_database(extra)
    add_note(database, "a newer database")
    database.close()
    monkeypatch.setattr(worker, "_database_specs", lambda _dir: (spec, extra))
    return spec, extra


@pytest.mark.parametrize("fallback", ["healthy", "verification-fails", "normal-start-fails"])
def test_incompatible_return_preserves_current_data_and_the_retained_recovery_point(
    switched, monkeypatch, fallback
):
    install, data_dir, point = switched
    spec, extra = _newer_data(data_dir, monkeypatch)

    def start(_install, *, version_id=None, verification=False, **_kw):
        try:
            open_database(notes_spec(data_dir) if version_id == "rel_old" else spec).close()
        except DatabaseFormatError:
            return SimpleNamespace(ok=False, message="incompatible data")
        if fallback == "verification-fails" or (
            fallback == "normal-start-fails" and not verification
        ):
            return SimpleNamespace(ok=False, message="current version still fails to start")
        return _ok()

    monkeypatch.setattr(worker.processes, "start", start)
    operation = _request(install, point)
    worker.run(install, operation.id)
    result = load_operation(install, operation.id)
    assert result.phase == ("rolled_back" if fallback == "healthy" else "needs_attention")
    assert f"rollback --restore-data {point.data_snapshot} --yes" in (
        result.message if fallback == "healthy" else result.error
    )
    assert install.version().name == "rel_new"
    assert stored_bodies(spec) == ["before the update", "after the update"]
    assert stored_bodies(extra) == ["a newer database"]
    assert rollback.previous_update(install).id == point.id
    assert (snapshot_root(data_dir) / point.data_snapshot).is_dir()


@pytest.mark.parametrize("candidate_ok", [True, False])
def test_approved_restore_saves_current_data_and_recovers_it_if_the_target_fails(
    switched, monkeypatch, candidate_ok
):
    install, data_dir, point = switched
    spec, extra = _newer_data(data_dir, monkeypatch)

    def start(_install, *, version_id=None, verification=False, **_kw):
        if version_id == "rel_old":
            assert stored_bodies(notes_spec(data_dir)) == ["before the update"]
            assert "tasks" not in read_marker(data_dir).databases
            return SimpleNamespace(ok=candidate_ok, message="target result")
        if version_id == "rel_new":
            assert stored_bodies(spec) == ["before the update", "after the update"]
            assert stored_bodies(extra) == ["a newer database"]
        return _ok()

    monkeypatch.setattr(worker.processes, "start", start)
    operation = _request(install, point, restore=True)
    worker.run(install, operation.id)
    result = load_operation(install, operation.id)
    assert result.phase == ("completed" if candidate_ok else "rolled_back")
    assert result.data_snapshot is not None and result.data_snapshot != point.data_snapshot
    assert install.version().name == ("rel_old" if candidate_ok else "rel_new")
    safety = snapshot_root(data_dir) / result.data_snapshot
    manifest = read_manifest(data_dir, safety)
    assert manifest is not None and "tasks" in manifest.members
    if candidate_ok:
        assert stored_bodies(notes_spec(data_dir)) == ["before the update"]
        # Arbitrary ordinary snapshots cannot prune the saved current data.
        for _ in range(6):
            assert create_data_snapshot(data_dir, reason="operator") is not None
        assert safety.is_dir()


def test_interrupted_explicit_restore_is_never_replayed_or_started_automatically(
    switched, monkeypatch
):
    install, _data_dir, point = switched
    operation = _request(install, point, restore=True)
    operation.phase = "activating"
    operation.data_restore_started = True
    operation.data_snapshot = point.data_snapshot
    operation.save(install)
    monkeypatch.setattr(
        worker.processes, "start", lambda *_a, **_kw: pytest.fail("uncertain data stays offline")
    )
    worker.run(install, operation.id)
    assert load_operation(install, operation.id).phase == "needs_attention"
    assert install.version().name == "rel_new"


@pytest.mark.parametrize("writer_still_live", [True, False])
def test_restore_rechecks_writers_after_validating_the_selected_snapshot(
    switched, monkeypatch, writer_still_live
):
    from core.database import recovery

    install, data_dir, point = switched
    restore = recovery.restore_data_snapshot
    checked = False

    def preflight(*args, check_only=False, **kwargs):
        nonlocal checked
        if not check_only:
            return restore(*args, **kwargs)
        result = restore(*args, check_only=True, **kwargs)
        database = open_database(notes_spec(data_dir))
        add_note(database, "written during preflight")
        database.close()
        checked = True
        return result

    monkeypatch.setattr(recovery, "restore_data_snapshot", preflight)
    monkeypatch.setattr(
        worker,
        "_live_servers",
        lambda _dir: "another server is running" if checked and writer_still_live else None,
    )
    operation = _request(install, point, restore=True)
    worker.run(install, operation.id)
    result = load_operation(install, operation.id)
    assert result.phase == "failed"
    assert not result.data_restore_started
    assert "The data was not restored" in result.message
    assert install.version().name == "rel_new"
    assert stored_bodies(notes_spec(data_dir)) == ["before the update", "written during preflight"]


@pytest.mark.parametrize("writer_still_live", [True, False])
def test_safety_restore_also_refuses_changes_during_its_planning(
    switched, monkeypatch, writer_still_live
):
    from core.database import recovery

    install, data_dir, point = switched
    _newer_data(data_dir, monkeypatch)
    restore = recovery.restore_data_snapshot
    changed = False

    def preflight(*args, **kwargs):
        nonlocal changed
        if kwargs.get("cause") == "failed user-selected application rollback":
            database = open_database(notes_spec(data_dir))
            add_note(database, "written during recovery")
            database.close()
            changed = True
        return restore(*args, **kwargs)

    monkeypatch.setattr(recovery, "restore_data_snapshot", preflight)
    monkeypatch.setattr(
        worker.processes,
        "start",
        lambda *_a, **_kw: SimpleNamespace(ok=False, message="candidate failed"),
    )
    monkeypatch.setattr(
        worker,
        "_live_servers",
        lambda _dir: "another server is running" if changed and writer_still_live else None,
    )
    operation = _request(install, point, restore=True)
    worker.run(install, operation.id)
    result = load_operation(install, operation.id)
    assert result.phase == "needs_attention"
    assert f"safety snapshot {result.data_snapshot} was not restored" in result.error
    assert install.version().name == "rel_new"
    assert stored_bodies(notes_spec(data_dir)) == ["before the update", "written during recovery"]


def test_predecessor_tray_cannot_begin_a_new_worker_transaction(switched, monkeypatch):
    install, data_dir, point = switched
    operation = Operation(id="upd_from_old_tray", previous_version="rel_new")
    operation.save(install)
    (install.root / "host.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(operations, "host_running", lambda _install: True)
    monkeypatch.setattr(worker.processes, "stop", lambda *_a, **_kw: pytest.fail("no server stop"))
    worker.run(install, operation.id)
    result = load_operation(install, operation.id)
    assert result.phase == "failed"
    assert "tray is still running" in result.error
    assert result.data_snapshot is None
    assert install.version().name == "rel_new"
    assert rollback.previous_update(install).id == point.id
    assert stored_bodies(notes_spec(data_dir)) == ["before the update"]
