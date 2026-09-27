from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from core.utils.paths import model_path


@pytest.mark.parametrize(
    ("path", "rendered"),
    [
        # A native relative path is rendered without resolving it.
        (Path("relative") / "child.txt", "relative/child.txt"),
        (PureWindowsPath(r"C:\Users\Viro\file.txt"), "C:/Users/Viro/file.txt"),
        (PureWindowsPath(r"\\server\share\file.txt"), "//server/share/file.txt"),
        (PurePosixPath("/srv/vbot/file.txt"), "/srv/vbot/file.txt"),
    ],
    ids=["native-relative", "windows-drive", "windows-unc", "posix"],
)
def test_model_path_renders_forward_slash_paths(path: PurePath, rendered: str) -> None:
    assert model_path(path) == rendered
