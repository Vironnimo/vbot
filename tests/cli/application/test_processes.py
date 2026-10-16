import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import server_management
from cli.application import operations, processes
from cli.application.state import Installation
from cli.server_management import HealthProbeResult, WebUIProbeResult
from core.utils import outside_job
from core.utils import processes as core_processes
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR
from core.utils.processes import ForeignJob


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


_VBOT = HealthProbeResult(reachable=True, is_vbot=True)
_BUSY = HealthProbeResult(reachable=False, is_vbot=False, timed_out=True, unresponsive=True)
_CLOSED = HealthProbeResult(reachable=False, is_vbot=False)
_NORMAL = ("-m", "server.main")
_VERIFICATION = ("-m", "server.main", "--verification-only")


def _record_server(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    version_id: str = "rel_one",
    command: tuple[str, ...] = _NORMAL,
    recorded: Callable[[], bool] = lambda: True,
) -> None:
    """Publish a control record naming one live server process of *version_id*."""
    executable = _runtime_interpreter(root, version_id, "Server")
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    monkeypatch.setattr(
        server_management,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=12, process_create_time=34.0) if recorded() else None,
    )
    monkeypatch.setattr(
        server_management.psutil,
        "Process",
        lambda _pid: SimpleNamespace(
            create_time=lambda: 34.0,
            exe=lambda: str(executable),
            cmdline=lambda: [executable.name, *command],
        ),
    )


# How identity, liveness and health combine is ``classify_server``'s contract
# (tests/cli/test_server_management.py); these cases pin the exact version and mode.
@pytest.mark.parametrize(
    ("health", "server", "verification", "expected"),
    [
        pytest.param(_VBOT, {}, False, "running", id="normal"),
        pytest.param(_VBOT, {"command": _VERIFICATION}, True, "running", id="verification"),
        pytest.param(_BUSY, {}, False, "unresponsive", id="busy-event-loop"),
        pytest.param(
            _VBOT, {"command": _VERIFICATION}, False, "foreign", id="verification-is-not-normal"
        ),
        pytest.param(_VBOT, {"version_id": "rel_other"}, False, "foreign", id="other-version"),
    ],
)
def test_server_state_requires_the_exact_version_and_startup_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    health: HealthProbeResult,
    server: dict,
    verification: bool,
    expected: str,
) -> None:
    install = _install(tmp_path)
    _record_server(monkeypatch, tmp_path, **server)
    monkeypatch.setattr(processes, "probe_health", lambda *_a, **_k: pytest.fail("quick probe"))
    monkeypatch.setattr(processes, "probe_health_patiently", lambda _instance: health)

    assert (
        processes.server_state(install, version_id="rel_one", verification=verification) == expected
    )


def _spawned_server_becomes_ready(
    monkeypatch: pytest.MonkeyPatch, root: Path, spawned: list[dict]
) -> None:
    """No server runs until the test's spawn; the spawned one then answers as vBot."""
    _record_server(monkeypatch, root, recorded=lambda: bool(spawned))
    monkeypatch.setattr(processes, "probe_health_patiently", lambda _instance: _CLOSED)
    monkeypatch.setattr(processes, "probe_health", lambda _instance: _VBOT)
    monkeypatch.setattr(
        processes, "probe_webui", lambda _instance: WebUIProbeResult(available=True)
    )


@pytest.mark.parametrize("independent_parent", [False, True])
def test_server_start_detaches_only_when_parent_is_not_already_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, independent_parent: bool
) -> None:
    install = _install(tmp_path)
    spawned: list[dict] = []
    _spawned_server_becomes_ready(monkeypatch, tmp_path, spawned)

    def flags(*, new_process_group, breakaway):
        assert new_process_group is True
        assert breakaway is not independent_parent
        return 32 if breakaway else 0

    def spawn(_args, **kwargs):
        assert kwargs["creationflags"] == (0 if independent_parent else 32)
        spawned.append(kwargs)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(processes, "subprocess_creation_flags", flags)
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)
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
    spawned: list[dict] = []
    _spawned_server_becomes_ready(monkeypatch, tmp_path, spawned)

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


@pytest.mark.parametrize(
    ("health", "server", "ok"),
    [
        pytest.param(_VBOT, {}, True, id="already-running"),
        pytest.param(_BUSY, {}, False, id="busy-event-loop"),
        pytest.param(_VBOT, {"version_id": "rel_other"}, False, id="other-version"),
    ],
)
def test_server_start_never_spawns_next_to_an_existing_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    health: HealthProbeResult,
    server: dict,
    ok: bool,
) -> None:
    from cli._output import print_command_result

    install = _install(tmp_path)
    _record_server(monkeypatch, tmp_path, **server)
    monkeypatch.setattr(processes, "probe_health_patiently", lambda _instance: health)
    monkeypatch.setattr(processes, "probe_webui", lambda _: WebUIProbeResult(available=True))
    monkeypatch.setattr(processes.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned"))

    result = processes.start(install)

    assert result.ok is ok
    # The CLI renders the running state from the result's health.
    assert result.health is health
    assert (result.webui is not None) is ok
    print_command_result("start", result)


def test_a_systemd_unit_owns_the_normal_server_but_never_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path)
    commands: list[list[str]] = []
    spawned: list[dict] = []
    _spawned_server_becomes_ready(monkeypatch, tmp_path, spawned)
    monkeypatch.setattr(processes, "owned_unit", lambda _install: tmp_path / "vbot.service")

    def run(command: list[str]) -> SimpleNamespace:
        commands.append(command)
        spawned.append({})  # The unit's server publishes its control record.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def spawn(_args, **kwargs):
        spawned.append(kwargs)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(processes, "run_command", run)
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        processes,
        "stop_server",
        lambda instance, **_kwargs: server_management.CommandResult(
            ok=True, message="stopped", instance=instance
        ),
    )

    assert processes.start(install).message == "started via systemd"
    assert processes.stop(install).ok
    assert commands == [
        ["systemctl", "--user", "start", "vbot.service"],
        # Stopping the unit also cancels a restart it might schedule.
        ["systemctl", "--user", "stop", "vbot.service"],
    ]

    commands.clear()
    spawned.clear()
    _record_server(monkeypatch, tmp_path, command=_VERIFICATION, recorded=lambda: bool(spawned))
    assert processes.start(install, verification=True).message == "started"
    assert commands == []


_CONFINED = ForeignJob("codex.exe", 4711, confines_children=True)


@pytest.mark.parametrize("broker", ["starts", "refuses"])
def test_confined_server_start_leaves_the_foreign_job_or_names_its_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broker: str,
) -> None:
    from cli._output import print_command_result

    install = _install(tmp_path)
    spawned: list[dict] = []
    brokered: list[dict] = []
    _spawned_server_becomes_ready(monkeypatch, tmp_path, spawned)
    monkeypatch.setattr(core_processes, "foreign_job", lambda: _CONFINED)

    def start_outside(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if broker == "refuses":
            raise OSError("Windows Management Instrumentation refused: access denied")
        brokered.append({"argv": argv, **kwargs})
        spawned.append({})
        return SimpleNamespace(pid=321, poll=lambda: None)

    def spawn(_args, **kwargs):
        spawned.append(kwargs)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(outside_job, "start_outside_job", start_outside)
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)

    result = processes.start(install)
    print_command_result("start", result)

    assert result.ok
    output = capsys.readouterr().out
    if broker == "starts":
        (started,) = brokered
        assert started["argv"][1:3] == ["-m", "server.main"]
        assert started["cwd"] == install.version() / "app"
        env = started["env"]
        assert isinstance(env, dict)
        assert env[CONSOLE_LOGGING_ENV_VAR] == "0"
        assert env["VBOT_INSTALL_ROOT"] == str(install.root)
        assert (result.process_id, result.attention) == (321, ())
        assert "warning:" not in output
    else:
        # The server still starts, inside the Job, and the caller learns what that means.
        assert result.process_id == 123
        (note,) = result.attention
        assert "codex.exe (pid 4711)" in note
        assert "ends when that application closes" in note
        assert "access denied" in note
        assert "Start menu" in note
        assert f"warning: {note}" in output


@pytest.mark.parametrize(
    ("route", "command"),
    [
        pytest.param("tray-answers", "restart", id="confined-tray-answers"),
        pytest.param("tray-declines", "restart", id="confined-tray-declines"),
        pytest.param("no-tray", "start", id="confined-no-tray"),
        pytest.param("unconfined", "restart", id="unconfined-with-tray"),
    ],
)
def test_cli_start_or_restart_hands_a_confined_caller_to_the_running_tray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, command: str
) -> None:
    install = _install(tmp_path)
    calls: list[str] = []
    if route != "unconfined":
        monkeypatch.setattr(core_processes, "foreign_job", lambda: _CONFINED)
    monkeypatch.setattr(operations, "host_running", lambda _install: route != "no-tray")

    def request_server(_install: Installation, requested: str) -> dict | None:
        calls.append(f"tray:{requested}")
        if route == "tray-declines":
            return None
        return {"state": "done", "ok": True, "message": "restarted", "process_id": 77}

    def local(name: str) -> Callable[..., server_management.CommandResult]:
        def run(target: Installation, **_kwargs: object) -> server_management.CommandResult:
            calls.append(name)
            return server_management.CommandResult(
                ok=True, message=name, instance=processes.target(target)
            )

        return run

    monkeypatch.setattr(operations, "request_server", request_server)
    monkeypatch.setattr(processes, "start", local("start"))
    monkeypatch.setattr(processes, "stop", local("stop"))
    monkeypatch.setattr(processes, "probe_health", lambda _instance: _VBOT)
    monkeypatch.setattr(processes, "probe_webui", lambda _: WebUIProbeResult(available=True))

    result = processes.start_or_restart(install, command)

    assert result.ok
    if route == "tray-answers":
        assert calls == ["tray:restart"]
        assert (result.message, result.process_id) == ("restarted", 77)
        assert result.health is _VBOT
    else:
        local_calls = ["stop", "start"] if command == "restart" else ["start"]
        handed = [f"tray:{command}"] if route == "tray-declines" else []
        assert calls == handed + local_calls
