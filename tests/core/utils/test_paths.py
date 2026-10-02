import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from core.utils.paths import file_url_path, model_path


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


@pytest.mark.parametrize(
    ("url", "windows", "posix"),
    [
        ("file:///C:/Users/a%20b.png", "C:/Users/a b.png", "/C:/Users/a b.png"),
        ("file://localhost/srv/x.txt?raw#top", "/srv/x.txt", "/srv/x.txt"),
        ("file://LocalHost/srv/x.txt", "/srv/x.txt", "/srv/x.txt"),
        # A host names another computer: a UNC path on Windows, no path elsewhere.
        ("file://fileserver.example/share/x.txt", "//fileserver.example/share/x.txt", None),
    ],
    ids=["drive", "localhost", "localhost-any-case", "other-computer"],
)
def test_file_url_path_names_the_path_on_this_computer(
    url: str, windows: str, posix: str | None
) -> None:
    path = file_url_path(url)

    if sys.platform == "win32":
        assert path is not None
        assert PureWindowsPath(path) == PureWindowsPath(windows)
    else:
        assert path == posix
