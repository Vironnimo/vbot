"""Durable removal ownership blocks mutation until explicit verified recovery."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import command, host, integration, operations
from cli.application.state import ApplicationError, Installation, Operation, exclusive
from cli.parser import parse_args


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    (root / "versions" / "rel_current").mkdir(parents=True)
    (root / "active-version").write_text("rel_current\n", encoding="ascii")
    (root / "unins000.exe").write_bytes(b"")
    (root / "unins000.dat").write_bytes(b"")
    return install


def _removal_record(install: Installation, *, pid: int = 44, created: float = 12.5) -> Path:
    path = install.root / "removal-pending.json"
    path.write_text(
        json.dumps({"schema_version": 1, "pid": pid, "process_created": created}),
        encoding="utf-8",
    )
    return path


def test_removal_marker_blocks_lifecycle_and_update_dispatch_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    _removal_record(install)

    with (
        pytest.raises(ApplicationError, match="removal is pending"),
        exclusive(install.root, "operation"),
    ):
        pass
    with (
        pytest.raises(ApplicationError, match="removal is pending"),
        exclusive(install.root, "host"),
    ):
        pass
    monkeypatch.setattr(
        host.processes, "stop", lambda _install: SimpleNamespace(ok=True, message="stopped")
    )
    host.ApplicationFacade(install).stop_server()

    monkeypatch.setattr(
        operations, "spawn_worker", lambda *_args: pytest.fail("must not spawn an updater")
    )
    with pytest.raises(ApplicationError, match="removal is pending"):
        operations.request_update(install)


def test_begin_removal_claims_dispatch_and_operation_with_actual_parent_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    locks: list[tuple[str, int, bool]] = []

    @contextmanager
    def tracked_lock(
        _root: Path, name: str = "operation", *, timeout: int = 0, allow_removal: bool = False
    ):
        locks.append((name, timeout, allow_removal))
        yield

    first_phase = SimpleNamespace(
        pid=516,
        exe=lambda: str(install.root / "unins000.exe"),
        info={"exe": str(install.root / "unins000.exe")},
    )
    pseudo_processes = [
        SimpleNamespace(pid=132, info={"exe": "Registry"}),
        SimpleNamespace(pid=4, info={"exe": "System"}),
    ]
    parent = SimpleNamespace(
        pid=517,
        create_time=lambda: 42.25,
        exe=lambda: str(tmp_path / "is-fixture-uninstall.tmp" / "_unins.tmp"),
        cmdline=lambda: ["_unins.tmp", f"/SECONDPHASE={install.root / 'unins000.exe'}"],
        parent=lambda: first_phase,
    )

    def process(pid: int) -> SimpleNamespace:
        assert pid == os.getppid()
        return parent

    monkeypatch.setattr(integration, "exclusive", tracked_lock)
    monkeypatch.setattr(integration, "operations", lambda _install: [])
    monkeypatch.setattr(integration.processes, "target", lambda _install: object())
    monkeypatch.setattr(
        integration, "probe_health", lambda _target: SimpleNamespace(reachable=False)
    )
    monkeypatch.chdir(install.root)
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(
            Error=OSError,
            Process=process,
            process_iter=lambda _attrs: [first_phase, *pseudo_processes],
        ),
    )

    assert integration.begin_removal(install) == {"ok": True, "removal_pending": True}
    assert locks == [("dispatch", 15, False), ("operation", 0, False)]
    assert json.loads((install.root / "removal-pending.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "pid": 517,
        "process_created": 42.25,
    }


def test_begin_removal_rejects_pending_update_before_writing_a_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    monkeypatch.setattr(integration, "operations", lambda _install: [Operation(id="upd_busy")])

    with pytest.raises(ApplicationError, match="update is still pending"):
        integration.begin_removal(install)
    assert not (install.root / "removal-pending.json").exists()


def test_begin_removal_rejects_an_unverified_process_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    parent = SimpleNamespace(
        pid=517,
        exe=lambda: str(tmp_path / "is-fixture-uninstall.tmp" / "_unins.tmp"),
        cmdline=lambda: ["_unins.tmp", f"/SECONDPHASE={install.root / 'unins000.exe'}"],
        parent=lambda: SimpleNamespace(pid=516, exe=lambda: str(tmp_path / "other.exe")),
    )

    monkeypatch.setattr(integration, "operations", lambda _install: [])
    monkeypatch.setattr(integration.processes, "target", lambda _install: object())
    monkeypatch.setattr(
        integration, "probe_health", lambda _target: SimpleNamespace(reachable=False)
    )
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(
            Error=OSError,
            Process=lambda _pid: parent,
            process_iter=lambda _attrs: [],
        ),
    )

    with pytest.raises(ApplicationError, match="registered vBot uninstaller"):
        integration.begin_removal(install)
    assert not (install.root / "removal-pending.json").exists()


def test_begin_removal_rejects_a_mismatched_second_phase_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    parent = SimpleNamespace(
        pid=517,
        exe=lambda: str(tmp_path / "is-fixture-uninstall.tmp" / "_unins.tmp"),
        cmdline=lambda: ["_unins.tmp", f"/SECONDPHASE={tmp_path / 'other-unins.exe'}"],
        parent=lambda: SimpleNamespace(pid=516, exe=lambda: str(install.root / "unins000.exe")),
    )

    monkeypatch.setattr(integration, "operations", lambda _install: [])
    monkeypatch.setattr(integration.processes, "target", lambda _install: object())
    monkeypatch.setattr(
        integration, "probe_health", lambda _target: SimpleNamespace(reachable=False)
    )
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(
            Error=OSError,
            Process=lambda _pid: parent,
            process_iter=lambda _attrs: [],
        ),
    )

    with pytest.raises(ApplicationError, match="registered vBot uninstaller"):
        integration.begin_removal(install)
    assert not (install.root / "removal-pending.json").exists()


def test_begin_removal_rejects_a_live_owned_desktop_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    desktop = install.root / "versions" / "rel_current" / "runtime" / "vBot.Desktop.exe"
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b"")

    class Process:
        pid = 888
        info = {"exe": str(desktop)}

    monkeypatch.setattr(integration, "operations", lambda _install: [])
    monkeypatch.setattr(integration.processes, "target", lambda _install: object())
    monkeypatch.setattr(
        integration, "probe_health", lambda _target: SimpleNamespace(reachable=False)
    )
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(
            Error=OSError,
            Process=lambda _pid: SimpleNamespace(
                pid=777,
                create_time=lambda: 1.0,
                exe=lambda: str(tmp_path / "is-fixture-uninstall.tmp" / "_unins.tmp"),
                cmdline=lambda: [
                    "_unins.tmp",
                    f"/SECONDPHASE={install.root / 'unins000.exe'}",
                ],
                parent=lambda: SimpleNamespace(
                    pid=776, exe=lambda: str(install.root / "unins000.exe")
                ),
            ),
            process_iter=lambda _attrs: [Process()],
        ),
    )

    with pytest.raises(ApplicationError, match="Desktop windows and commands"):
        integration.begin_removal(install)
    assert not (install.root / "removal-pending.json").exists()


def test_reset_removal_refuses_live_or_malformed_ownership_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    path = _removal_record(install)

    class NoSuchProcessError(Exception):
        pass

    class LiveProcess:
        def __init__(self, pid: int) -> None:
            assert pid == 44

        def create_time(self) -> float:
            return 12.5

        def is_running(self) -> bool:
            return True

    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=LiveProcess, NoSuchProcess=NoSuchProcessError),
    )
    with pytest.raises(ApplicationError, match="uninstaller is still running"):
        integration.reset_removal(install)
    assert path.exists()

    path.write_text(
        json.dumps({"schema_version": 1, "pid": True, "process_created": 12.5}),
        encoding="utf-8",
    )
    with pytest.raises(ApplicationError, match="ownership is invalid"):
        integration.reset_removal(install)
    assert path.exists()


@pytest.mark.parametrize("owner", ["dead", "reused"])
def test_reset_removal_revalidates_before_clearing_dead_or_reused_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    install = _install(tmp_path / "app")
    path = _removal_record(install)

    class NoSuchProcessError(Exception):
        pass

    class ReusedProcess:
        def __init__(self, pid: int) -> None:
            assert pid == 44

        def create_time(self) -> float:
            return 99.0

        def is_running(self) -> bool:
            return True

    process = (
        (lambda _pid: (_ for _ in ()).throw(NoSuchProcessError()))
        if owner == "dead"
        else ReusedProcess
    )
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=process, NoSuchProcess=NoSuchProcessError),
    )
    validations: list[tuple[Path, str, bool]] = []

    def failed_validation(_version: Path, *, shape: str, remove_bytecode_caches: bool) -> None:
        validations.append((_version, shape, remove_bytecode_caches))
        raise ApplicationError("release inventory is incomplete")

    monkeypatch.setattr("cli.application.packages.validate_release", failed_validation)
    with pytest.raises(ApplicationError, match="inventory is incomplete"):
        integration.reset_removal(install)
    assert path.exists()
    assert validations == [(install.version(), "server", True)]

    monkeypatch.setattr(
        "cli.application.packages.validate_release",
        lambda version, *, shape, remove_bytecode_caches: validations.append(
            (version, shape, remove_bytecode_caches)
        ),
    )
    assert integration.reset_removal(install) == {
        "ok": True,
        "changed": True,
        "removal_pending": False,
    }
    assert not path.exists()
    assert validations == [(install.version(), "server", True)] * 2


@pytest.mark.parametrize(
    ("name", "expected"),
    [("removal-begin", "begin"), ("removal-reset", "reset")],
)
def test_application_removal_commands_dispatch_to_the_durable_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, expected: str
) -> None:
    install = _install(tmp_path / "app")
    calls: list[str] = []

    def begin(_install: Installation) -> dict[str, bool]:
        calls.append("begin")
        return {"ok": True}

    def reset(_install: Installation) -> dict[str, bool]:
        calls.append("reset")
        return {"ok": True}

    monkeypatch.setattr(command, "discover", lambda: install)
    monkeypatch.setattr(integration, "begin_removal", begin)
    monkeypatch.setattr(integration, "reset_removal", reset)

    assert command.dispatch(parse_args(["application", name])) == 0
    assert calls == [expected]
