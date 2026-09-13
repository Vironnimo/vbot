import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import processes
from cli.application.state import Installation


def _install(root: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    executable = _runtime_interpreter(root, "rel_one", "Server")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    (root / "active-version").write_text("rel_one\n", encoding="ascii")
    return install


def _runtime_interpreter(root: Path, version_id: str, role: str) -> Path:
    runtime = root / "versions" / version_id / "runtime"
    return runtime / (f"vBot.{role}.exe" if os.name == "nt" else "bin/python3")


@pytest.mark.parametrize(
    ("command", "verification", "expected"),
    [
        (["-m", "server.main"], False, True),
        (["-m", "server.main", "--verification-only"], True, True),
        (["-m", "server.main", "--verification-only"], False, False),
    ],
)
def test_running_server_match_proves_executable_process_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    verification: bool,
    expected: bool,
) -> None:
    install = _install(tmp_path)
    executable = install.interpreter("rel_one", "Server")
    monkeypatch.setattr(processes, "probe_health", lambda _instance: SimpleNamespace(is_vbot=True))
    monkeypatch.setattr(
        processes,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=12, process_create_time=34.0),
    )
    monkeypatch.setattr(
        processes.psutil,
        "Process",
        lambda _pid: SimpleNamespace(
            create_time=lambda: 34.0,
            exe=lambda: str(executable),
            cmdline=lambda: [executable.name, *command],
        ),
    )

    assert (
        processes.running_server_matches(install, version_id="rel_one", verification=verification)
        is expected
    )


def test_running_server_match_rejects_other_version_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    other = _runtime_interpreter(tmp_path, "rel_other", "Server")
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    monkeypatch.setattr(processes, "probe_health", lambda _instance: SimpleNamespace(is_vbot=True))
    monkeypatch.setattr(
        processes,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=12, process_create_time=34.0),
    )
    monkeypatch.setattr(
        processes.psutil,
        "Process",
        lambda _pid: SimpleNamespace(
            create_time=lambda: 34.0,
            exe=lambda: str(other),
            cmdline=lambda: [str(other), "-m", "server.main"],
        ),
    )

    assert not processes.running_server_matches(install, version_id="rel_one")


@pytest.mark.parametrize("independent_parent", [False, True])
def test_server_start_detaches_only_when_parent_is_not_already_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, independent_parent: bool
) -> None:
    install = _install(tmp_path)
    monkeypatch.setattr(
        processes, "probe_health", lambda _instance: SimpleNamespace(reachable=False)
    )
    monkeypatch.setattr(processes, "running_server_matches", lambda *_args, **_kwargs: True)

    def flags(*, new_process_group, breakaway):
        assert new_process_group is True
        assert breakaway is not independent_parent
        return 32 if breakaway else 0

    def spawn(_args, **kwargs):
        assert kwargs["creationflags"] == (0 if independent_parent else 32)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(processes, "subprocess_creation_flags", flags)
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)
    result = (
        processes.start(install, breakaway=False)
        if independent_parent
        else processes.start(install)
    )
    assert result.ok
