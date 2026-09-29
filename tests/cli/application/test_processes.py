import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import processes
from cli.application.state import Installation
from cli.server_management import HealthProbeResult, WebUIProbeResult
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR


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
    ("version_id", "command", "verification", "expected"),
    [
        pytest.param("rel_one", ["-m", "server.main"], False, True, id="normal"),
        pytest.param(
            "rel_one", ["-m", "server.main", "--verification-only"], True, True, id="verification"
        ),
        pytest.param(
            "rel_one",
            ["-m", "server.main", "--verification-only"],
            False,
            False,
            id="verification-is-not-normal",
        ),
        pytest.param("rel_other", ["-m", "server.main"], False, False, id="other-version"),
    ],
)
def test_running_server_match_proves_executable_process_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_id: str,
    command: list[str],
    verification: bool,
    expected: bool,
) -> None:
    install = _install(tmp_path)
    executable = _runtime_interpreter(tmp_path, version_id, "Server")
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
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
    monkeypatch.setattr(
        processes, "probe_webui", lambda _instance: WebUIProbeResult(available=True)
    )
    result = (
        processes.start(install, breakaway=False)
        if independent_parent
        else processes.start(install)
    )
    assert result.ok


def test_server_start_keeps_only_process_output_in_a_bounded_startup_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    startup_log = tmp_path / "logs" / "server-startup.log"
    startup_log.parent.mkdir()
    startup_log.write_bytes(b"old crash output")
    monkeypatch.setattr(processes, "STARTUP_LOG_ROTATE_BYTES", 8)
    monkeypatch.setattr(
        processes, "probe_health", lambda _instance: SimpleNamespace(reachable=False)
    )
    monkeypatch.setattr(processes, "running_server_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        processes, "probe_webui", lambda _instance: WebUIProbeResult(available=True)
    )
    spawned: list[dict] = []

    def spawn(_args, **kwargs):
        spawned.append(kwargs)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(processes.subprocess, "Popen", spawn)

    assert processes.start(install).ok
    # Log lines go to the daily log only; the startup file receives raw process output.
    assert spawned[0]["env"][CONSOLE_LOGGING_ENV_VAR] == "0"
    assert spawned[0]["stdout"].name == str(startup_log)
    assert startup_log.read_bytes() == b""
    assert startup_log.with_name("server-startup.log.1").read_bytes() == b"old crash output"


def test_already_running_result_preserves_health_for_cli_output(tmp_path, monkeypatch):
    from cli._output import print_command_result

    install = _install(tmp_path)
    health = HealthProbeResult(reachable=True, is_vbot=True)
    monkeypatch.setattr(processes, "probe_health", lambda _: health)
    monkeypatch.setattr(processes, "probe_webui", lambda _: WebUIProbeResult(available=True))
    monkeypatch.setattr(processes, "running_server_matches", lambda *a, **kw: True)
    result = processes.start(install)
    assert result.ok and result.health is health
    assert result.webui.available
    print_command_result("start", result)
