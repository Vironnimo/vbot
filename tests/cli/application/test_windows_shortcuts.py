"""Real shell links: the taskbar reads exactly these files and properties."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("shell links exist only on Windows", allow_module_level=True)

from cli.application import windows_shortcuts as shortcuts  # noqa: E402
from desktop import _windows  # noqa: E402

_LINK_SET_ARGUMENTS, _LINK_SET_PATH = 11, 20


@pytest.fixture(autouse=True)
def com_apartment():
    shortcuts._ole32.CoInitializeEx(None, 0x2)
    yield
    shortcuts._ole32.CoUninitialize()


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    return tmp_path / "Roaming"


def _shortcut_path(profile: Path, folder: Path) -> Path:
    path = profile / folder / "vBot Desktop.lnk"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_shortcut(path: Path, target: Path, arguments: str) -> None:
    with (
        shortcuts._shell_link() as link,
        shortcuts._query(link, shortcuts._IID_PERSIST_FILE) as file,
    ):
        shortcuts._method(link, _LINK_SET_PATH, wintypes.LPCWSTR)(str(target))
        shortcuts._method(link, _LINK_SET_ARGUMENTS, wintypes.LPCWSTR)(arguments)
        save = shortcuts._method(file, shortcuts._FILE_SAVE, wintypes.LPCWSTR, wintypes.BOOL)
        save(str(path), True)


def _read_shortcut(path: Path) -> tuple[str | None, bool]:
    """The shortcut's identity, and whether it still starts the same target."""
    with (
        shortcuts._shell_link() as link,
        shortcuts._query(link, shortcuts._IID_PERSIST_FILE) as file,
        shortcuts._query(link, shortcuts._IID_PROPERTY_STORE) as store,
    ):
        load = shortcuts._method(file, shortcuts._FILE_LOAD, wintypes.LPCWSTR, wintypes.DWORD)
        load(str(path), 0)
        target = ctypes.create_unicode_buffer(32768)
        get_path = shortcuts._method(
            link, 3, wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        )
        get_path(target, 32768, None, 0)
        return shortcuts._app_id(store), target.value.endswith("vBot.GUI.exe")


def test_identity_matches_the_desktop_window() -> None:
    assert shortcuts.DESKTOP_APP_ID == _windows.DESKTOP_APP_ID


@pytest.mark.parametrize("folder", shortcuts._SHORTCUT_FOLDERS, ids=["start-menu", "pinned"])
def test_a_desktop_shortcut_gains_the_identity_once(profile: Path, tmp_path: Path, folder) -> None:
    root = tmp_path / "vBot"
    path = _shortcut_path(profile, folder)
    _write_shortcut(path, root / "vBot.GUI.exe", "desktop")

    shortcuts.claim_desktop_shortcuts(root)

    assert _read_shortcut(path) == (shortcuts.DESKTOP_APP_ID, True)
    written = path.stat().st_mtime_ns
    path.touch()
    touched = path.stat().st_mtime_ns
    shortcuts.claim_desktop_shortcuts(root)
    assert written <= touched == path.stat().st_mtime_ns


@pytest.mark.parametrize(
    ("target", "arguments"),
    [("vBot.exe", ""), ("vBot.GUI.exe", "server"), ("other/vBot.GUI.exe", "desktop")],
    ids=["tray", "other-command", "other-installation"],
)
def test_other_shortcuts_stay_untouched(
    profile: Path, tmp_path: Path, target: str, arguments: str
) -> None:
    root = tmp_path / "vBot"
    path = _shortcut_path(profile, shortcuts._SHORTCUT_FOLDERS[0])
    _write_shortcut(path, root / target, arguments)

    shortcuts.claim_desktop_shortcuts(root)

    assert _read_shortcut(path)[0] is None


def test_a_broken_shortcut_does_not_stop_the_launch(profile: Path, tmp_path: Path) -> None:
    root = tmp_path / "vBot"
    broken = _shortcut_path(profile, shortcuts._SHORTCUT_FOLDERS[0])
    broken.write_bytes(b"not a shell link")
    pinned = _shortcut_path(profile, shortcuts._SHORTCUT_FOLDERS[1])
    _write_shortcut(pinned, root / "vBot.GUI.exe", "desktop")

    shortcuts.claim_desktop_shortcuts(root)

    assert broken.read_bytes() == b"not a shell link"
    assert _read_shortcut(pinned)[0] == shortcuts.DESKTOP_APP_ID
