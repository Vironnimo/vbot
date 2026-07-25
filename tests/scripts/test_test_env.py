import argparse
import importlib.util
import json
import sys
from pathlib import Path

from cli.server_management import CommandResult, HealthProbeResult, ServerInstance, WebUIProbeResult

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "test-env.py"
NPM_EXECUTABLE = "npm.cmd" if sys.platform == "win32" else "npm"


def _load_test_env_module():
    spec = importlib.util.spec_from_file_location("test_env", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_frontend_rebuilds_even_when_dist_exists(monkeypatch, tmp_path):
    module = _load_test_env_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(module, "WEBUI_DIR", tmp_path / "webui")
    monkeypatch.setattr(module, "WEBUI_DIST", tmp_path / "webui" / "dist" / "index.html")
    monkeypatch.setattr(module, "WEBUI_NODE_MODULES", tmp_path / "webui" / "node_modules")

    module.WEBUI_DIST.parent.mkdir(parents=True)
    module.WEBUI_DIST.write_text("built", encoding="utf-8")
    module.WEBUI_NODE_MODULES.mkdir(parents=True)

    def fake_run(cmd, *, cwd=None):
        commands.append(cmd)
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module, "_run", fake_run)

    assert module.build_frontend() == 0
    assert commands == [[NPM_EXECUTABLE, "run", "build"]]


def test_build_frontend_installs_dependencies_when_missing(monkeypatch, tmp_path):
    module = _load_test_env_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(module, "WEBUI_DIR", tmp_path / "webui")
    monkeypatch.setattr(module, "WEBUI_DIST", tmp_path / "webui" / "dist" / "index.html")
    monkeypatch.setattr(module, "WEBUI_NODE_MODULES", tmp_path / "webui" / "node_modules")

    module.WEBUI_DIR.mkdir(parents=True)

    def fake_run(cmd, *, cwd=None):
        commands.append(cmd)
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module, "_run", fake_run)

    assert module.build_frontend() == 0
    assert commands == [
        [NPM_EXECUTABLE, "install"],
        [NPM_EXECUTABLE, "run", "build"],
    ]


def test_start_server_uses_direct_cli_lifecycle(monkeypatch, tmp_path, capsys):
    module = _load_test_env_module()
    instance = ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=tmp_path,
        url="http://127.0.0.1:8420",
        log_path=tmp_path / "vbot.log",
    )
    calls = []

    def fake_resolve_instance(**kwargs):
        calls.append(("resolve", kwargs))
        return instance

    def fake_start_server(resolved_instance, **kwargs):
        calls.append(("start", resolved_instance, kwargs))
        return CommandResult(
            ok=True,
            message="started",
            instance=resolved_instance,
            health=HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            webui=WebUIProbeResult(available=True, status_code=200),
            process_id=123,
        )

    monkeypatch.setattr(module, "resolve_instance", fake_resolve_instance)
    monkeypatch.setattr(module, "start_server_command", fake_start_server)
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("start should not shell out")),
    )

    assert module.start_server("0.0.0.0", 9000, "C:/tmp/vbot") == 0

    captured = capsys.readouterr()
    assert "target..... http://127.0.0.1:8420" in captured.out
    assert "server..... yes" in captured.out
    assert "url........ http://127.0.0.1:8420" in captured.out
    assert "webui...... available" in captured.out
    assert f"log........ {instance.log_path}" in captured.out
    assert calls == [
        ("resolve", {"host": "0.0.0.0", "port": 9000, "data_dir": "C:/tmp/vbot"}),
        ("start", instance, {"startup_timeout_seconds": module.STARTUP_TIMEOUT_SECONDS}),
    ]


def test_stop_server_uses_direct_cli_lifecycle(monkeypatch, tmp_path, capsys):
    module = _load_test_env_module()
    instance = ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=tmp_path,
        url="http://127.0.0.1:8420",
        log_path=tmp_path / "vbot.log",
    )
    calls = []

    def fake_resolve_instance(**kwargs):
        calls.append(("resolve", kwargs))
        return instance

    def fake_stop_server(resolved_instance):
        calls.append(("stop", resolved_instance))
        return CommandResult(
            ok=True,
            message="not running",
            instance=resolved_instance,
            health=HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
            webui=WebUIProbeResult(available=False),
        )

    monkeypatch.setattr(module, "resolve_instance", fake_resolve_instance)
    monkeypatch.setattr(module, "stop_server_command", fake_stop_server)
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stop should not shell out")),
    )

    assert module.stop_server("127.0.0.1", None, None) == 0

    captured = capsys.readouterr()
    assert "stop....... no" in captured.out
    assert calls == [
        ("resolve", {"host": "127.0.0.1", "port": None, "data_dir": None}),
        ("stop", instance),
    ]


def test_start_server_reports_structured_keyboard_interrupt(monkeypatch, tmp_path, capsys):
    module = _load_test_env_module()
    instance = ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=tmp_path,
        url="http://127.0.0.1:8420",
        log_path=tmp_path / "vbot.log",
    )

    monkeypatch.setattr(module, "resolve_instance", lambda **kwargs: instance)

    def raise_interrupt(resolved_instance, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(module, "start_server_command", raise_interrupt)

    assert module.start_server("127.0.0.1", None, None) == 130

    captured = capsys.readouterr()
    assert "server..... FAILED" in captured.out
    assert "result: interrupted while waiting for local server readiness" in captured.out
    assert f"url: {instance.url}" in captured.out
    assert f"log: {instance.log_path}" in captured.out


def test_resolve_fake_provider_from_seeded_settings(tmp_path):
    module = _load_test_env_module()
    settings = json.loads(
        (PROJECT_ROOT / "tests" / "e2e" / "fake-provider-settings.json").read_text(encoding="utf-8")
    )
    settings["providers"]["custom"]["fake"]["base_url"] = "http://127.0.0.1:18422/v1"
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    instance = ServerInstance(
        host="127.0.0.1",
        port=8422,
        data_dir=tmp_path,
        url="http://127.0.0.1:8422",
        log_path=tmp_path / "vbot.log",
    )

    fake_provider = module._resolve_fake_provider(instance)

    assert fake_provider == module.FakeProviderInstance(
        host="127.0.0.1",
        port=18422,
        pid_path=tmp_path / "processes" / "fake-provider.pid",
        log_path=tmp_path / "processes" / "fake-provider.log",
    )


def test_start_fake_provider_launches_owned_node_process(monkeypatch, tmp_path):
    module = _load_test_env_module()
    fake_provider = module.FakeProviderInstance(
        host="127.0.0.1",
        port=18422,
        pid_path=tmp_path / "processes" / "fake-provider.pid",
        log_path=tmp_path / "processes" / "fake-provider.log",
    )
    readiness = iter([False, True])
    popen_calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(module, "_fake_provider_is_ready", lambda _instance: next(readiness))
    monkeypatch.setattr(module, "_fake_provider_port_is_bound", lambda _instance: False)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    assert module.start_fake_provider(fake_provider) is True
    assert fake_provider.pid_path.read_text(encoding="utf-8") == "4321"
    assert popen_calls[0][0] == ["node", str(module.FAKE_PROVIDER_ENTRY)]
    assert popen_calls[0][1]["env"]["VBOT_E2E_PROVIDER_PORT"] == "18422"


def test_stop_fake_provider_terminates_only_ready_owned_process(monkeypatch, tmp_path):
    module = _load_test_env_module()
    fake_provider = module.FakeProviderInstance(
        host="127.0.0.1",
        port=18422,
        pid_path=tmp_path / "processes" / "fake-provider.pid",
        log_path=tmp_path / "processes" / "fake-provider.log",
    )
    fake_provider.pid_path.parent.mkdir(parents=True)
    fake_provider.pid_path.write_text("4321", encoding="utf-8")
    killed = []

    monkeypatch.setattr(module, "_fake_provider_is_ready", lambda _instance: True)
    monkeypatch.setattr(module, "_wait_for_fake_provider", lambda _instance, *, ready: not ready)
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert module.stop_fake_provider(fake_provider) is True
    assert killed == [(4321, module.signal.SIGTERM)]
    assert not fake_provider.pid_path.exists()


def test_main_starts_seeded_provider_before_vbot(monkeypatch, tmp_path):
    module = _load_test_env_module()
    instance = ServerInstance(
        host="127.0.0.1",
        port=8422,
        data_dir=tmp_path,
        url="http://127.0.0.1:8422",
        log_path=tmp_path / "vbot.log",
    )
    fake_provider = module.FakeProviderInstance(
        host="127.0.0.1",
        port=18422,
        pid_path=tmp_path / "processes" / "fake-provider.pid",
        log_path=tmp_path / "processes" / "fake-provider.log",
    )
    calls = []

    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: argparse.Namespace(
            command="start",
            host="127.0.0.1",
            port=None,
            data_dir=None,
        ),
    )
    monkeypatch.setattr(module, "build_frontend", lambda: 0)
    monkeypatch.setattr(module, "resolve_instance", lambda **_kwargs: instance)
    monkeypatch.setattr(module, "_resolve_fake_provider", lambda _instance: fake_provider)
    monkeypatch.setattr(
        module,
        "start_fake_provider",
        lambda resolved: calls.append(("provider", resolved)) or True,
    )
    monkeypatch.setattr(
        module,
        "start_server",
        lambda *_args: calls.append(("server", instance)) or 0,
    )

    assert module.main() == 0
    assert calls == [("provider", fake_provider), ("server", instance)]
