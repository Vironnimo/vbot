"""Contract tests of the path normalization every permission check shares."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.permissions import (
    BoundaryContext,
    PathAccess,
    PermissionBoundary,
    normalize_path,
    normalize_permission_rules,
)
from tests.directory_links import link_directory

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows path grammar")


def _boundary(tmp_path: Path, rules: object) -> PermissionBoundary:
    for name in ("work", "outside", "home", "data"):
        (tmp_path / name).mkdir(exist_ok=True)
    context = BoundaryContext(
        workdir=tmp_path / "work", home=tmp_path / "home", data_dir=tmp_path / "data"
    )
    return PermissionBoundary.build(normalize_permission_rules(rules), context)


_LOCKED = {"write": "work_area", "deny": ["Edit({workdir}/locked)"]}


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("locked/x", id="plain"),
        pytest.param("./locked//x", id="dot-and-empty-segments"),
        pytest.param("sub/../locked/x", id="dotdot"),
        pytest.param("{work}/locked/x", id="absolute"),
        pytest.param("LOCKED\\X", id="case-and-backslash", marks=windows_only),
        pytest.param("locked. \\x", id="trailing-dot-and-space", marks=windows_only),
        pytest.param("\\\\?\\{work}\\locked\\x", id="verbatim-drive-prefix", marks=windows_only),
        pytest.param("{WORK}/locked/x", id="upper-case-absolute", marks=windows_only),
    ],
)
def test_spellings_of_one_path_get_the_same_decision(tmp_path: Path, spelling: str) -> None:
    boundary = _boundary(tmp_path, _LOCKED)
    work = str(tmp_path / "work")
    path = spelling.replace("{work}", work).replace("{WORK}", work.upper())

    refusal = boundary.check_path(path, "write")

    assert refusal is not None and refusal.reason == "deny_rule"
    expected = (tmp_path / "work" / "locked" / "x").as_posix().casefold()
    assert refusal.target.casefold() == expected
    normalized = normalize_path(path, base=tmp_path / "work", home=tmp_path / "home")
    assert normalized.lexical.casefold() == expected
    assert (
        boundary.check_path(path.replace("ocked", "isted").replace("OCKED", "ISTED"), "write")
        is None
    )


@pytest.mark.parametrize(
    ("path", "form"),
    [
        pytest.param("file://fileserver/share/a.txt", "network path", id="file-url-host"),
        pytest.param("\\\\fileserver\\share\\a.txt", "network path", id="unc", marks=windows_only),
        pytest.param(
            "//fileserver/share/a.txt", "network path", id="unc-slashes", marks=windows_only
        ),
        pytest.param(
            "\\\\?\\UNC\\fileserver\\share\\a",
            "network path",
            id="verbatim-unc",
            marks=windows_only,
        ),
        pytest.param("\\\\.\\PhysicalDrive0", "device path", id="device", marks=windows_only),
        pytest.param(
            "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\x",
            "device path",
            id="globalroot",
            marks=windows_only,
        ),
        pytest.param("a.txt:secret", "alternate data stream", id="ads", marks=windows_only),
        pytest.param("a.txt::$DATA", "alternate data stream", id="data-stream", marks=windows_only),
    ],
)
def test_unsafe_forms_are_refused_before_any_filesystem_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str, form: str
) -> None:
    boundary = _boundary(tmp_path, {})

    def touched(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the check touched the filesystem")

    for name in ("realpath", "exists", "lexists", "isdir"):
        monkeypatch.setattr(os.path, name, touched)
    monkeypatch.setattr(os, "stat", touched)
    monkeypatch.setattr(os, "lstat", touched)

    refusal = boundary.check_path(path, "read")

    assert refusal is not None and refusal.reason == "unsafe_form"
    assert refusal.location == form


@windows_only
def test_an_allow_rule_that_names_a_network_path_permits_it(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path, {"allow": ["Read(//fileserver/share/docs)"]})

    assert boundary.check_path("\\\\fileserver\\share\\docs\\a.txt", "read") is None
    assert boundary.check_path("\\\\fileserver\\share\\other.txt", "read") is not None
    assert boundary.check_path("\\\\fileserver\\share\\docs\\a.txt", "write") is not None


@pytest.mark.parametrize(
    ("link", "target", "path", "access", "rules"),
    [
        pytest.param(
            "{work}/link",
            "{outside}",
            "link/a.txt",
            "read",
            {"read": "work_area"},
            id="inside-pointing-outside",
        ),
        pytest.param(
            "{work}/link",
            "{outside}",
            "link/new.txt",
            "write",
            {"write": "work_area"},
            id="inside-pointing-outside-new-file",
        ),
        pytest.param(
            "{outside}/back",
            "{work}",
            "{outside}/back/a.txt",
            "read",
            {"read": "work_area"},
            id="outside-pointing-inside",
        ),
        pytest.param(
            "{outside}/secrets",
            "{work}/secrets",
            "{outside}/secrets/key.pem",
            "read",
            {"deny": ["Read({workdir}/secrets/**)"]},
            id="deny-reached-through-a-link",
        ),
    ],
)
def test_links_and_junctions_are_checked_on_both_sides(
    tmp_path: Path,
    link: str,
    target: str,
    path: str,
    access: PathAccess,
    rules: object,
) -> None:
    boundary = _boundary(tmp_path, rules)
    (tmp_path / "work" / "secrets").mkdir()

    def resolved(text: str) -> str:
        return text.replace("{work}", str(tmp_path / "work")).replace(
            "{outside}", str(tmp_path / "outside")
        )

    link_directory(Path(resolved(link)), Path(resolved(target)))

    refusal = boundary.check_path(resolved(path), access)

    assert refusal is not None
    assert refusal.resolved is not None


@pytest.mark.skipif(WINDOWS, reason="Windows applies .. before following links")
def test_posix_dotdot_climbs_out_of_the_link_target(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path, {"read": "work_area"})
    (tmp_path / "outside" / "deep").mkdir()
    (tmp_path / "work" / "link").symlink_to(tmp_path / "outside" / "deep")

    refusal = boundary.check_path("link/../x.txt", "read")

    assert refusal is not None and refusal.reason == "not_permitted"


def test_short_names_compare_as_their_long_names(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("8.3 short names exist only on Windows")
    import ctypes

    folder = tmp_path / "work" / "Long Folder Name"
    folder.mkdir(parents=True)
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.WinDLL("kernel32").GetShortPathNameW(str(folder), buffer, 1024)
    short = Path(buffer.value)
    if short.name == folder.name:
        pytest.skip("8.3 names are disabled on this volume")
    boundary = _boundary(tmp_path, {"deny": ["Read({workdir}/Long Folder Name)"]})

    refusal = boundary.check_path(str(short / "a.txt"), "read")

    assert refusal is not None and refusal.reason == "deny_rule"
    assert refusal.target.endswith("/Long Folder Name/a.txt")
