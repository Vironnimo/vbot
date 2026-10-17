"""The local access token file: one per server start, removed only by its writer."""

from __future__ import annotations

from pathlib import Path

from core.access.local_token import (
    create_local_access_token,
    local_token_path,
    read_local_access_token,
    remove_local_access_token,
)


def test_each_start_writes_a_fresh_token_that_only_its_writer_removes(tmp_path: Path) -> None:
    first = create_local_access_token(tmp_path, 8420)
    second = create_local_access_token(tmp_path, 8420)

    assert first.path == local_token_path(tmp_path, 8420)
    assert first.path == tmp_path.resolve() / "runtime" / "local-access-8420.token"
    assert first.value != second.value
    assert read_local_access_token(tmp_path, 8420) == second.value
    assert read_local_access_token(tmp_path, 8421) is None

    # An earlier server's exit leaves the newer server's token in place.
    remove_local_access_token(first)
    assert read_local_access_token(tmp_path, 8420) == second.value
    remove_local_access_token(second)
    assert not second.path.exists()


def test_a_malformed_token_file_holds_no_token(tmp_path: Path) -> None:
    path = local_token_path(tmp_path, 8420)
    path.parent.mkdir(parents=True)
    for content in ("", "vbl_short", "x" * 1000, "vbt_" + "A" * 43):
        path.write_text(content, encoding="ascii")
        assert read_local_access_token(tmp_path, 8420) is None
