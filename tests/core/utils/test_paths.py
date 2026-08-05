from pathlib import Path, PurePosixPath, PureWindowsPath

from core.utils.paths import model_path


def test_model_path_renders_native_path_without_resolving() -> None:
    assert model_path(Path("relative") / "child.txt") == "relative/child.txt"


def test_model_path_renders_windows_drive_path() -> None:
    assert model_path(PureWindowsPath(r"C:\Users\Viro\file.txt")) == ("C:/Users/Viro/file.txt")


def test_model_path_renders_windows_unc_path() -> None:
    assert model_path(PureWindowsPath(r"\\server\share\file.txt")) == ("//server/share/file.txt")


def test_model_path_preserves_posix_path() -> None:
    assert model_path(PurePosixPath("/srv/vbot/file.txt")) == "/srv/vbot/file.txt"
