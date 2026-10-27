"""The shared OS primitive keeps empty lock files untouched and locks exclusive."""

from pathlib import Path

import pytest

from core.utils.file_lock import acquire_file_lock, release_file_lock


@pytest.mark.parametrize("blocking", [False, True])
def test_exclusive_empty_file_lock_releases_explicitly_or_on_close(
    tmp_path: Path, blocking: bool
) -> None:
    path = tmp_path / "owner.lock"
    with path.open("a+b") as owner, path.open("a+b") as contender:
        # Windows locks from the current descriptor position, including beyond
        # EOF. Every owner must lock byte 0 without first writing a sentinel.
        owner.seek(7)
        acquire_file_lock(owner.fileno(), blocking=blocking)
        try:
            with pytest.raises(OSError):
                acquire_file_lock(contender.fileno())
            assert path.stat().st_size == 0
        finally:
            release_file_lock(owner.fileno())
        acquire_file_lock(contender.fileno())
        release_file_lock(contender.fileno())

        acquire_file_lock(owner.fileno())
        owner.close()
        acquire_file_lock(contender.fileno())
        release_file_lock(contender.fileno())
    assert path.read_bytes() == b""
