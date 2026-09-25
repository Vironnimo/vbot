"""Apply patch: Delete and Move act on directory entries, including links."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.tools.file_state import FileReadState
from tests.core.tools.apply_patch_helpers import apply, text, update


def _patch(body: str) -> str:
    return f"*** Begin Patch\n{body}\n*** End Patch"


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def _shared_target(tmp_path: Path) -> Path:
    (tmp_path / "shared").mkdir()
    target = tmp_path / "shared" / "real.txt"
    target.write_bytes(b"precious\n")
    return target


def test_delete_removes_the_link_and_keeps_its_target(tmp_path):
    target = _shared_target(tmp_path)
    link = tmp_path / "link.txt"
    _symlink_or_skip(link, target)

    result = apply(tmp_path, _patch("*** Delete File: link.txt"))

    assert result["ok"], result
    assert text(result) == "Deleted link.txt."
    assert not os.path.lexists(link)
    assert target.read_bytes() == b"precious\n"


def test_move_renames_the_link_and_keeps_its_target(tmp_path):
    target = _shared_target(tmp_path)
    link = tmp_path / "link.txt"
    _symlink_or_skip(link, target)

    result = apply(tmp_path, _patch("*** Move File: link.txt -> moved.txt"))

    assert result["ok"], result
    moved = tmp_path / "moved.txt"
    assert not os.path.lexists(link)
    assert moved.is_symlink()
    assert moved.resolve() == target.resolve()
    assert target.read_bytes() == b"precious\n"
    assert text(result) == "Moved link.txt to moved.txt."


def test_update_through_a_link_edits_the_target_and_move_to_renames_the_link(tmp_path):
    target = _shared_target(tmp_path)
    link = tmp_path / "link.txt"
    _symlink_or_skip(link, target)

    result = apply(
        tmp_path,
        _patch("*** Update File: link.txt\n*** Move to: renamed.txt\n@@\n-precious\n+edited"),
    )

    assert result["ok"], result
    # The edit lands in the link's target, and the link itself is renamed.
    assert text(result) == "Updated shared/real.txt:\n1| edited\nMoved link.txt to renamed.txt."
    assert target.read_bytes() == b"edited\n"
    assert (tmp_path / "renamed.txt").is_symlink()
    assert not os.path.lexists(link)


def test_dangling_links_are_entries_for_delete_and_move_destinations(tmp_path):
    missing = tmp_path / "missing.txt"
    dangling = tmp_path / "dangling.txt"
    _symlink_or_skip(dangling, missing)
    (tmp_path / "source.txt").write_bytes(b"data\n")

    blocked = apply(tmp_path, _patch("*** Move File: source.txt -> dangling.txt"))

    assert blocked["error"]["code"] == "destination_exists"
    assert not missing.exists()
    assert (tmp_path / "source.txt").read_bytes() == b"data\n"

    deleted = apply(tmp_path, _patch("*** Delete File: dangling.txt"))

    assert deleted["ok"], deleted
    assert not os.path.lexists(dangling)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions")
def test_windows_junction_is_deleted_and_moved_as_a_link(tmp_path):
    import _winapi

    (tmp_path / "outside").mkdir()
    secret = tmp_path / "outside" / "secret.txt"
    secret.write_bytes(b"keep\n")
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(tmp_path / "outside"), str(junction))

    moved = apply(tmp_path, _patch("*** Move File: junction -> renamed"))

    assert moved["ok"], moved
    assert not os.path.lexists(junction)
    assert (tmp_path / "renamed" / "secret.txt").read_bytes() == b"keep\n"

    deleted = apply(tmp_path, _patch("*** Delete File: renamed"))

    assert deleted["ok"], deleted
    assert not os.path.lexists(tmp_path / "renamed")
    assert secret.read_bytes() == b"keep\n"


def test_case_only_move_renames_the_file(tmp_path):
    (tmp_path / "readme.txt").write_bytes(b"hi\n")
    state = FileReadState()

    result = apply(tmp_path, _patch("*** Move File: readme.txt -> README.txt"), state=state)

    assert result["ok"], result
    assert result["data"] == {"status": "applied", "content": "Moved readme.txt to README.txt."}
    assert os.listdir(tmp_path) == ["README.txt"]
    assert (tmp_path / "README.txt").read_bytes() == b"hi\n"
    # The renamed file counts as read for a later full replacement.
    replaced = apply(tmp_path, _patch("*** Add File: README.txt\n+hello"), state=state)
    assert replaced["ok"], replaced


def test_update_with_case_only_move_to_edits_and_renames(tmp_path):
    (tmp_path / "readme.txt").write_bytes(b"hi\n")

    result = apply(tmp_path, update("*** Move to: README.TXT\n@@\n-hi\n+hello", "readme.txt"))

    assert result["ok"], result
    assert text(result) == "Updated readme.txt and moved it to README.TXT:\n1| hello"
    assert os.listdir(tmp_path) == ["README.TXT"]
    assert (tmp_path / "README.TXT").read_bytes() == b"hello\n"


def test_move_to_the_existing_spelling_is_already_applied(tmp_path):
    (tmp_path / "readme.txt").write_bytes(b"hi\n")

    result = apply(tmp_path, _patch("*** Move File: readme.txt -> readme.txt"))

    assert result["ok"], result
    assert result["data"] == {
        "status": "unchanged",
        "content": "readme.txt already has that name. No file was changed.",
    }
    assert os.listdir(tmp_path) == ["readme.txt"]
