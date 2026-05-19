import importlib.util
import sys
from pathlib import Path

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
