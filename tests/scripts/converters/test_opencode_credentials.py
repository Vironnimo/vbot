"""Explicit conversion preserves Account slots and refuses to overwrite secrets."""

from pathlib import Path

import pytest

from scripts.converters.opencode_credentials import convert_credentials


def test_conversion_preserves_other_lines_and_named_accounts(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = (
        b"# local credentials\r\nOTHER_KEY=unchanged\r\n"
        b"OPENCODE_GO_API_KEY='default-test'\r\n"
        b"  OPENCODE_GO_API_KEY__WORK = named-test\r\n"
    )
    path.write_bytes(original)
    assert convert_credentials(tmp_path) == 2
    assert path.read_bytes() == original
    assert convert_credentials(tmp_path, apply=True) == 2
    assert path.read_bytes() == original.replace(b"OPENCODE_GO_API_KEY", b"OPENCODE_API_KEY")
    assert convert_credentials(tmp_path, apply=True) == 0


def test_identical_shared_slot_is_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('OPENCODE_GO_API_KEY="same-test"\nOPENCODE_API_KEY=same-test\n')
    assert convert_credentials(tmp_path, apply=True) == 1
    assert path.read_text() == "OPENCODE_API_KEY=same-test\n"


@pytest.mark.parametrize("apply", [False, True])
def test_conflicting_slots_leave_the_whole_file_unchanged(tmp_path: Path, apply: bool) -> None:
    path = tmp_path / ".env"
    original = (
        "OPENCODE_GO_API_KEY=go-test\nOPENCODE_GO_API_KEY__WORK=work-test\n"
        "OPENCODE_API_KEY__WORK=other-test\n"
    )
    path.write_text(original)
    with pytest.raises(ValueError) as caught:
        convert_credentials(tmp_path, apply=apply)
    assert "work-test" not in str(caught.value)
    assert "other-test" not in str(caught.value)
    assert path.read_text() == original


def test_invalid_account_suffix_does_not_get_silently_renamed(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("OPENCODE_GO_API_KEY__DEFAULT=test\n")
    with pytest.raises(ValueError):
        convert_credentials(tmp_path, apply=True)
    assert path.read_text() == "OPENCODE_GO_API_KEY__DEFAULT=test\n"
