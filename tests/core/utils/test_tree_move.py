"""Directory-tree moves are all or nothing: a failure never leaves a half-removed source."""

import errno
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.utils import tree_move
from core.utils.tree_move import move_tree, remove_tree


def make_tree(root: Path) -> Path:
    (root / "sub").mkdir(parents=True)
    (root / "top.txt").write_text("top", encoding="utf-8")
    (root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    return root


def contents(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def other_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Treat ``tmp_path/other`` as another volume and return that directory.

    A rename into or out of it fails like one between volumes. Every other rename,
    such as setting the source aside, still works unless ``retire_error`` makes it
    fail like a tree that a program holds open.
    """

    def install(retire_error: OSError | None = None) -> Path:
        volume = tmp_path / "other"
        volume.mkdir()
        real_replace = os.replace

        def replace(source: Path, destination: Path) -> None:
            if volume in (*Path(source).parents, *Path(destination).parents):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            if retire_error is not None:
                raise retire_error
            real_replace(source, destination)

        monkeypatch.setattr(tree_move, "_replace", replace)
        return volume

    return install


def test_move_tree_renames_within_a_volume(tmp_path: Path) -> None:
    source = make_tree(tmp_path / "source")
    expected = contents(source)

    move_tree(source, tmp_path / "moved")

    assert not source.exists()
    assert contents(tmp_path / "moved") == expected


def test_move_tree_across_volumes_copies_then_removes_the_original(
    tmp_path: Path, other_volume: Callable[..., Path]
) -> None:
    source = make_tree(tmp_path / "source")
    read_only = source / "sub" / "nested.txt"
    read_only.chmod(stat.S_IREAD)  # like ``.git`` objects, which block a plain rmtree on Windows
    expected = contents(source)
    volume = other_volume()

    move_tree(source, volume / "moved")
    (volume / "moved" / "sub" / "nested.txt").chmod(stat.S_IWRITE | stat.S_IREAD)

    assert contents(volume / "moved") == expected
    assert sorted(path.name for path in tmp_path.iterdir()) == ["other"]


@pytest.mark.parametrize("failure", ["rename-refused", "copy-fails", "original-held-open"])
def test_move_tree_failure_leaves_the_source_whole_and_no_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    other_volume: Callable[..., Path],
    failure: str,
) -> None:
    source = make_tree(tmp_path / "source")
    expected = contents(source)
    refusal = PermissionError(errno.EACCES, "held open by another program")
    volume = tmp_path / "other"
    if failure == "rename-refused":
        volume.mkdir()

        def refuse(_source: Path, _destination: Path) -> None:
            raise refusal

        monkeypatch.setattr(tree_move, "_replace", refuse)
    else:
        other_volume(retire_error=refusal if failure == "original-held-open" else None)
    if failure == "copy-fails":
        real_copytree = tree_move.shutil.copytree

        def copy_partway(src: Path, dst: Path, *args: Any, **options: Any) -> None:
            real_copytree(src, dst, *args, **options)
            if Path(dst) == volume / "moved":  # not the recursive sub-copies
                (Path(dst) / "top.txt").unlink()
                raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(tree_move.shutil, "copytree", copy_partway)

    with pytest.raises(OSError):
        move_tree(source, volume / "moved")

    assert contents(source) == expected
    assert sorted(path.name for path in tmp_path.iterdir()) == ["other", "source"]
    assert list(volume.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to rename a tree with open files")
def test_move_tree_leaves_the_source_whole_while_a_program_holds_a_file_open(
    tmp_path: Path,
) -> None:
    source = make_tree(tmp_path / "source")
    expected = contents(source)

    with (source / "sub" / "nested.txt").open("rb"), pytest.raises(OSError):
        move_tree(source, tmp_path / "moved")

    assert contents(source) == expected
    assert not (tmp_path / "moved").exists()


@pytest.mark.parametrize("destination", ["existing", "inside-source"])
def test_move_tree_refuses_a_destination_that_cannot_hold_the_tree(
    tmp_path: Path, destination: str
) -> None:
    source = make_tree(tmp_path / "source")
    expected = contents(source)
    target = tmp_path / "existing" if destination == "existing" else source / "sub" / "moved"
    target.parent.mkdir(exist_ok=True)
    if destination == "existing":
        target.mkdir()

    with pytest.raises(OSError):
        move_tree(source, target)

    assert contents(source) == expected


def test_move_tree_keeps_the_copy_when_the_original_cannot_be_fully_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_volume: Callable[..., Path]
) -> None:
    source = make_tree(tmp_path / "source")
    expected = contents(source)
    volume = other_volume()

    def delete_partway(path: Path) -> None:
        (path / "source" / "top.txt").unlink()
        raise PermissionError(errno.EACCES, "held open by another program")

    monkeypatch.setattr(tree_move, "_remove_tree", delete_partway)

    move_tree(source, volume / "moved")

    assert contents(volume / "moved") == expected
    assert not source.exists()
    leftovers = [path for path in tmp_path.iterdir() if path.name.startswith(".source.moved-")]
    assert len(leftovers) == 1


def _link_directory(link: Path, target: Path) -> None:
    """Create a directory link without privileges: a junction on Windows, a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("stop", [None, lambda: None], ids=["at-once", "stoppable"])
def test_remove_tree_deletes_only_inside_its_root_and_never_follows_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: Callable[[], None] | None
) -> None:
    outside = make_tree(tmp_path / "outside")
    expected = contents(outside)
    within = tmp_path / "archive"
    tree = make_tree(within / "entry" / "agent")
    _link_directory(tree / "linked", outside)
    read_only = tree / "sub" / "nested.txt"
    read_only.chmod(stat.S_IREAD)
    _link_directory(within / "link", outside)
    # Each link refuses its first removal, as a read-only link does on Windows; clearing
    # that attribute must never reach the read-only directory the link points at.
    outside.chmod(stat.S_IREAD | stat.S_IEXEC)
    real_unlink, refused_once = os.unlink, set()

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name in ("link", "linked") and path not in refused_once:
            refused_once.add(path)
            raise PermissionError(errno.EACCES, "read-only link", str(path))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)

    for refused in (outside, within, within / ".." / "outside", within / "link" / "top.txt"):
        with pytest.raises(ValueError, match="not inside"):
            remove_tree(refused, within=within)
    remove_tree(within / "link", within=within, stop=stop)
    remove_tree(within / "entry", within=within, stop=stop)
    remove_tree(within / "missing", within=within, stop=stop)

    assert len(refused_once) == 2
    assert list(within.iterdir()) == []
    assert not os.stat(outside).st_mode & stat.S_IWRITE
    assert contents(outside) == expected
    outside.chmod(stat.S_IRWXU)


def test_remove_tree_ends_where_stop_raises_and_a_later_removal_finishes(
    tmp_path: Path,
) -> None:
    within = tmp_path / "archive"
    tree = make_tree(within / "entry")
    calls: list[None] = []

    class StoppedError(Exception):
        pass

    def stop() -> None:
        calls.append(None)
        if len(calls) == 3:
            raise StoppedError

    with pytest.raises(StoppedError):
        remove_tree(tree, within=within, stop=stop)

    # One of the three entries went before the stop; the rest stays for the next removal.
    assert len(list(tree.rglob("*"))) == 2
    remove_tree(tree, within=within)
    assert not tree.exists()
