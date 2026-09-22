"""The native Desktop shortcut must not initialize the full console/server stack."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import _commands, main
from cli.application import desktop
from cli.application.state import Installation


def _installation(root: Path, shape: str = "server-desktop") -> Installation:
    install = Installation(root, shape, "127.0.0.1", 18420, str(root / "data"))
    version = root / "versions" / "rel_test"
    (version / "app").mkdir(parents=True)
    runtime = version / "runtime"
    executable = runtime / ("vBot.Desktop.exe" if os.name == "nt" else "bin/python3")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    (root / "active-version").write_text("rel_test\n", encoding="ascii")
    install.save()
    return install


def test_gui_shortcut_launches_without_importing_console_or_server_services(tmp_path: Path):
    install = _installation(tmp_path)
    script = """
import importlib.abc
import os
import runpy
import subprocess
import sys
from pathlib import Path

class StartupImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            'cli._commands', 'cli.parser', 'cli.application.host',
            'cli.server_management', 'core.settings', 'core.tools',
            'core.channels', 'core.model_tasks', 'server',
        }:
            raise AssertionError(f'Unexpected startup dependency: {fullname}')

sys.meta_path.insert(0, StartupImports())
root = Path(sys.argv[1])
sys.executable = str(root / 'vBot.GUI.exe')
launches = []
class DesktopProcess(subprocess.Popen):
    def __init__(self, arguments, **options):
        self._child_created = False
        launches.append((arguments, options))

subprocess.Popen = DesktopProcess
sys.argv = [sys.executable, 'desktop']
try:
    runpy.run_module('cli.main', run_name='__main__')
except SystemExit as error:
    assert error.code == 0, error.code
assert len(launches) == 1
arguments, options = launches[0]
assert arguments[1:] == ['-m', 'desktop.main', '--host', '127.0.0.1', '--port', '18420']
assert Path(arguments[0]).is_relative_to(root / 'versions' / 'rel_test' / 'runtime')
assert options['cwd'] == root / 'versions' / 'rel_test' / 'app'
assert options['env']['VBOT_INSTALL_ROOT'] == str(root)
assert 'PYTHONPATH' not in options['env']
assert not any(name.startswith('VBOT_RUN_') for name in options['env'])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(install.root)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["removing", "no_desktop", "missing_runtime"])
def test_gui_shortcut_rejects_unavailable_installation_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    install = _installation(tmp_path, "server" if failure == "no_desktop" else "server-desktop")
    if failure == "removing":
        (tmp_path / "removal-pending.json").write_text("{}", encoding="utf-8")
    elif failure == "missing_runtime":
        install.interpreter(role="Desktop").unlink()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "vBot.GUI.exe"))
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("spawned"))
    monkeypatch.setattr(main, "_configure_console_output", lambda: None)
    monkeypatch.setattr(_commands, "run", lambda *_: pytest.fail("used full CLI"))

    with pytest.raises(SystemExit) as error:
        main.main(["desktop"])
    assert error.value.code == 1


@pytest.mark.parametrize(
    ("executable", "arguments"),
    [
        ("vBot.GUI.exe", ["desktop", "--host", "192.0.2.8", "--port", "18420"]),
        ("vBot.GUI.exe", ["desktop", "--bad-option"]),
        ("vBot.GUI.exe", ["--help"]),
        ("vBot.exe", ["desktop"]),
        ("python.exe", ["desktop"]),
    ],
)
def test_other_entrypoints_retain_full_cli_dispatch(monkeypatch, executable, arguments):
    calls = []
    monkeypatch.setattr(sys, "executable", executable)
    monkeypatch.setattr(main, "_configure_console_output", lambda: None)
    monkeypatch.setattr(_commands, "run", lambda argv: calls.append(argv) or 7)
    with pytest.raises(SystemExit) as error:
        main.main(arguments)
    assert error.value.code == 7
    assert calls == [arguments]
