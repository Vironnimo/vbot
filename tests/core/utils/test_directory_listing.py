"""Path pickers browse the server's filesystem one directory at a time."""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.utils import directory_listing
from core.utils.directory_listing import (
    DirectoryEntry,
    DirectoryListingError,
    ListingPathError,
    list_directory,
    list_directory_sync,
)
from tests.directory_links import link_directory


def _hide(path: Path) -> None:
    """Give ``path`` the Windows hidden attribute; elsewhere only dot-names are hidden."""
    if sys.platform == "win32":
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        ctypes.windll.kernel32.SetFileAttributesW(
            str(path), attributes | stat.FILE_ATTRIBUTE_HIDDEN
        )


@pytest.mark.asyncio
async def test_places_list_the_filesystem_roots_and_the_home_directory() -> None:
    listing = await list_directory(None)

    assert (listing.path, listing.parent, listing.truncated) == ("", None, False)
    assert listing.home == Path.home().as_posix()
    assert listing.separator == os.sep
    roots = [entry.name for entry in listing.entries]
    if sys.platform == "win32":
        assert Path.home().anchor.replace("\\", "/") in roots
        assert all(re.fullmatch(r"[A-Z]:/", name) for name in roots)
    else:
        assert roots == ["/"]
    assert {(entry.kind, entry.link, entry.hidden) for entry in listing.entries} == {
        ("directory", False, False)
    }
    # A name prefix filters the roots too; the home directory stays.
    unmatched = await list_directory(None, prefix="no such root")
    assert (unmatched.entries, unmatched.home) == ((), listing.home)


@pytest.mark.asyncio
async def test_home_paths_expand_on_the_server() -> None:
    (Path.home() / "projects" / "vbot").mkdir(parents=True)

    home = await list_directory("~")
    projects = await list_directory("~/projects")

    assert (home.path, [entry.name for entry in home.entries]) == (
        Path.home().as_posix(),
        ["projects"],
    )
    assert (projects.parent, [entry.name for entry in projects.entries]) == (
        Path.home().as_posix(),
        ["vbot"],
    )


@pytest.mark.asyncio
async def test_a_directory_lists_directories_first_with_link_and_hidden_marks(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "folder"
    for name in ("beta", "Alpha", ".cache"):
        (folder / name).mkdir(parents=True)
    for name in ("b.txt", "A.md", ".env"):
        (folder / name).write_text("x", encoding="utf-8")
    _hide(folder / "b.txt")
    (tmp_path / "outside").mkdir()
    link_directory(folder / "linked", tmp_path / "outside")
    windows = sys.platform == "win32"

    listing = await list_directory(folder.as_posix(), include_files=True)
    # Native separators work as well; without include_files only directories come back.
    folders = await list_directory(str(folder) + os.sep)

    assert (listing.path, listing.parent, listing.truncated) == (
        folder.as_posix(),
        tmp_path.as_posix(),
        False,
    )
    assert listing.entries == (
        DirectoryEntry(".cache", "directory", link=False, hidden=True),
        DirectoryEntry("Alpha", "directory", link=False, hidden=False),
        DirectoryEntry("beta", "directory", link=False, hidden=False),
        DirectoryEntry("linked", "directory", link=True, hidden=False),
        DirectoryEntry(".env", "file", link=False, hidden=True),
        DirectoryEntry("A.md", "file", link=False, hidden=False),
        DirectoryEntry("b.txt", "file", link=False, hidden=windows),
    )
    assert folders.path == folder.as_posix()
    assert [entry.name for entry in folders.entries] == [".cache", "Alpha", "beta", "linked"]


@pytest.mark.asyncio
async def test_a_filesystem_root_has_no_parent(tmp_path: Path) -> None:
    anchor = tmp_path.anchor

    listing = await list_directory(anchor)

    assert (listing.path, listing.parent) == (Path(anchor).as_posix(), None)


@pytest.mark.asyncio
async def test_a_root_confines_the_listing_and_answers_relative_to_it(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "src" / "lib").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "secret").mkdir()
    link_directory(root / "escape", tmp_path / "secret")

    top = await list_directory("", root=str(root))
    unnamed = await list_directory(None, root=str(root))
    source = await list_directory("src/lib/..", root=str(root), include_files=True)
    library = await list_directory(os.path.join("src", "lib"), root=root.as_posix())

    assert (top.path, top.parent, [entry.name for entry in top.entries]) == (
        "",
        None,
        ["escape", "src"],
    )
    assert unnamed == top
    assert (source.path, source.parent, [entry.name for entry in source.entries]) == (
        "src",
        "",
        ["lib", "main.py"],
    )
    assert (library.path, library.parent) == ("src/lib", "src")
    for outside in ("..", "src/../..", "escape", "escape/deeper", str(tmp_path), "/src"):
        with pytest.raises(ListingPathError):
            await list_directory(outside, root=str(root))
    for request in ({"path": "src"}, {"path": "", "root": "project"}):
        with pytest.raises(ListingPathError):
            await list_directory(request["path"], root=request.get("root"))


@pytest.mark.asyncio
async def test_a_directory_that_cannot_be_listed_names_the_reason(
    tmp_path: Path, deny_access: Callable[[Path], None]
) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "denied").mkdir()
    deny_access(tmp_path / "denied")
    expected = {
        "missing": "not_found",
        "file.txt/deeper": "not_found",
        "file.txt": "not_a_directory",
        # Unreadable never reads as missing.
        "denied": "unreadable",
    }

    reasons = {}
    for name in expected:
        with pytest.raises(DirectoryListingError) as caught:
            await list_directory((tmp_path / name).as_posix())
        reasons[name] = caught.value.reason

    assert reasons == expected


@pytest.mark.asyncio
async def test_a_large_directory_is_truncated_at_the_entry_limit_after_the_name_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real limit holds ordinary directories; the test lowers it.
    assert directory_listing.DIRECTORY_LISTING_LIMIT >= 1000
    for name in ("a", "b", "c", "Cache"):
        (tmp_path / name).mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(directory_listing, "DIRECTORY_LISTING_LIMIT", 4)
    complete = await list_directory(tmp_path.as_posix())
    monkeypatch.setattr(directory_listing, "DIRECTORY_LISTING_LIMIT", 2)
    cut = await list_directory(tmp_path.as_posix())
    # The prefix applies before the limit and ignores case, so a typed name reaches
    # every entry it starts, also those a listing without it leaves out.
    named = await list_directory(tmp_path.as_posix(), include_files=True, prefix="C")

    assert (len(complete.entries), complete.truncated) == (4, False)
    assert (len(cut.entries), cut.truncated) == (2, True)
    assert ([entry.name for entry in named.entries], named.truncated) == (["c", "Cache"], False)


def test_a_listing_that_outlasts_its_budget_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(5):
        (tmp_path / f"folder-{index}").mkdir()
    # Every reading of the clock is one second later.
    ticks = itertools.count()
    monkeypatch.setattr(directory_listing, "_monotonic", lambda: float(next(ticks)))

    with pytest.raises(DirectoryListingError) as caught:
        list_directory_sync(tmp_path.as_posix(), timeout_seconds=3)

    assert caught.value.reason == "timeout"


@pytest.mark.asyncio
async def test_a_directory_that_does_not_answer_does_not_hold_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = asyncio.all_tasks()
    entered, release = threading.Event(), threading.Event()
    real_scandir = os.scandir

    def unresponsive(path: Any) -> Any:
        entered.set()
        release.wait(5)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", unresponsive)
    # The budget is spent at once, while the clock stands still for the worker.
    monkeypatch.setattr(directory_listing, "DIRECTORY_LISTING_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(directory_listing, "_monotonic", lambda: 0.0)

    with pytest.raises(DirectoryListingError) as caught:
        await list_directory(tmp_path.as_posix())

    assert caught.value.reason == "timeout"
    assert entered.wait(5)
    release.set()
    leftover = asyncio.all_tasks() - before - {asyncio.current_task()}
    if leftover:
        await asyncio.wait(leftover)
