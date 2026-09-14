from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cli.application import integration
from cli.application.state import Installation


def _install(tmp_path):
    install = Installation(tmp_path / "install", "desktop-client", None, None, None)
    runtime = install.root / "versions" / "rel_test" / "runtime"
    runtime.mkdir(parents=True)
    (install.root / "active-version").write_text("rel_test\n")
    (runtime.parent / "release.json").write_text("{}")
    (runtime / "vBot.GUI.exe").write_bytes(b"verified GUI bootstrap")
    return install


def test_gui_companion_is_added_once_and_preserved_across_updates(tmp_path, monkeypatch):
    install = _install(tmp_path)
    monkeypatch.delenv("APPDATA", raising=False)
    integration.refresh_gui_entrypoints(install)
    launcher = install.root / "vBot.GUI.exe"
    assert launcher.read_bytes() == b"verified GUI bootstrap"
    (install.version() / "runtime" / "vBot.GUI.exe").write_bytes(b"next build")
    integration.refresh_gui_entrypoints(install)
    assert launcher.read_bytes() == b"verified GUI bootstrap"


@pytest.mark.parametrize("owner", ["this", "foreign", "custom_arguments", "already_gui"])
def test_shortcut_repair_preserves_foreign_and_customized_links(tmp_path, monkeypatch, owner):
    install = _install(tmp_path)
    appdata = tmp_path / "appdata"
    shortcut = appdata / "Microsoft/Windows/Start Menu/Programs/vBot/vBot Desktop.lnk"
    shortcut.parent.mkdir(parents=True)
    shortcut.write_bytes(b"shortcut")
    target = install.root / ("vBot.GUI.exe" if owner == "already_gui" else "vBot.exe")
    if owner == "foreign":
        target = tmp_path / "another-install" / "vBot.exe"
    link = SimpleNamespace(
        TargetPath=str(target),
        Arguments="desktop --host custom" if owner == "custom_arguments" else "desktop",
        Save=Mock(),
    )
    dispatch = Mock(return_value=SimpleNamespace(CreateShortcut=Mock(return_value=link)))
    com = SimpleNamespace(CoInitialize=Mock(), CoUninitialize=Mock())
    monkeypatch.setattr(integration.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setitem(integration.sys.modules, "pythoncom", com)
    monkeypatch.setitem(
        integration.sys.modules, "win32com.client", SimpleNamespace(Dispatch=dispatch)
    )
    integration.refresh_gui_entrypoints(install)
    assert link.Save.call_count == (1 if owner == "this" else 0)
    assert Path(link.TargetPath) == (install.root / "vBot.GUI.exe" if owner == "this" else target)
    com.CoUninitialize.assert_called_once()


def test_protocol_one_payload_without_gui_companion_does_not_change_entrypoints(tmp_path):
    install = _install(tmp_path)
    (install.version() / "runtime" / "vBot.GUI.exe").unlink()
    integration.refresh_gui_entrypoints(install)
    assert not (install.root / "vBot.GUI.exe").exists()
