"""Worktree ownership reads must fail closed on corrupt marker files."""

from pathlib import Path

import pytest

from scripts._worktree_records import _read_worktree_marker


@pytest.mark.parametrize("content", [b"\xff", b"{broken", b"[]"])
def test_unreadable_marker_cannot_authorize_data_cleanup(tmp_path: Path, content: bytes) -> None:
    marker = tmp_path / ".vbot-worktree-owner.json"
    marker.write_bytes(content)

    assert _read_worktree_marker(marker) is None
