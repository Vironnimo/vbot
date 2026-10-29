"""The change pipeline under Permission Rules: refused before any effect."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
from core.tools._patch_report import edit_outcome
from tests.core.tools.apply_patch_test_support import call, text


def _boundary(root: Path, rules: object) -> PermissionBoundary:
    return PermissionBoundary.build(
        normalize_permission_rules(rules),
        BoundaryContext(workdir=root / "work", home=root / "home", data_dir=root / "data"),
    )


@pytest.fixture
def work(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    (tmp_path / "outside").mkdir()
    return tmp_path / "work"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "*** Move File: file.txt -> ../outside/moved.txt",
        "*** Update File: file.txt\n*** Move to: ../outside/moved.txt\n@@\n-old\n+new",
    ],
)
async def test_a_move_out_of_the_writable_area_is_refused_before_any_effect(
    work: Path, operation: str
) -> None:
    source = work / "file.txt"
    source.write_bytes(b"old\n")
    moved = work.parent / "outside" / "moved.txt"
    patch = f"*** Begin Patch\n{operation}\n*** End Patch"

    refused = await call(
        work, {"patch": patch}, permissions=_boundary(work.parent, {"write": "work_area"})
    )

    assert refused["error"]["code"] == "permission_denied"
    assert refused["error"]["retryable"] is False
    assert source.read_bytes() == b"old\n" and not moved.exists()
    assert (await call(work, {"patch": patch}))["ok"] and moved.exists()


@pytest.mark.asyncio
async def test_a_later_permission_refusal_keeps_completed_changes_and_continues_siblings(
    work: Path,
) -> None:
    first, second = work / "first", work / "second"
    first.mkdir()
    second.mkdir()
    (first / "value.txt").write_bytes(b"allowed\n")
    protected = second / "value.txt"
    protected.write_bytes(b"protected-content\n")
    link = first / "link.txt"
    try:
        os.symlink("value.txt", link)
    except OSError:
        pytest.skip("Creating symbolic links is not available")
    moved = second / "link.txt"
    patch = (
        "*** Begin Patch\n"
        "*** Move File: first/link.txt -> second/link.txt\n"
        "*** Update File: second/link.txt\n@@\n-protected-content\n+changed\n"
        "*** Add File: sibling.txt\n+done\n"
        "*** End Patch"
    )
    result = await call(
        work,
        {"patch": patch},
        permissions=_boundary(
            work.parent, {"write": "work_area", "deny": ["Read({workdir}/second/value.txt)"]}
        ),
    )

    assert result["ok"] and edit_outcome(text(result)) == "partial"
    assert "protected-content" not in text(result)
    assert not os.path.lexists(link) and moved.is_symlink()
    assert os.readlink(moved) == "value.txt"
    assert protected.read_bytes() == b"protected-content\n"
    assert (work / "sibling.txt").read_bytes() == b"done\n"
