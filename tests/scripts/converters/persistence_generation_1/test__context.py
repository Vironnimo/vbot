"""Tests for the shared Generation 1 conversion context."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)


def test_staged_paths_mirror_the_data_directory(tmp_path: Path) -> None:
    context = ConversionContext(source=tmp_path / "data", staging=tmp_path / "staging")

    staged = context.staged("channels/main/state.db")

    assert staged == tmp_path / "staging" / "channels" / "main" / "state.db"
    assert staged.parent.is_dir()


def test_retired_paths_are_recorded_once(tmp_path: Path) -> None:
    context = ConversionContext(source=tmp_path, staging=tmp_path / "staging")

    context.retire("channels/main/access.json")
    context.retire(PurePosixPath("channels/main/access.json"))

    assert context.retired == [PurePosixPath("channels/main/access.json")]


@pytest.mark.parametrize("relative", ["", "../outside.json", "/absolute.json"])
def test_paths_outside_the_data_directory_are_rejected(tmp_path: Path, relative: str) -> None:
    context = ConversionContext(source=tmp_path, staging=tmp_path / "staging")

    with pytest.raises(ConversionError):
        context.staged(relative)


def test_report_accumulates_counts_and_skips(tmp_path: Path) -> None:
    context = ConversionContext(source=tmp_path, staging=tmp_path / "staging")

    context.report.count("sessions", "entries", 3)
    context.report.count("sessions", "entries")
    context.report.skip("mcp", "res_abc", "no matching Tool result")
    context.report.skip("sessions", "session s1", "user msg_1 dropped", changes_history=True)

    assert context.report.to_dict() == {
        "counts": {"sessions": {"entries": 4}},
        "skipped": [
            {
                "area": "mcp",
                "item": "res_abc",
                "reason": "no matching Tool result",
                "changes_history": False,
            },
            {
                "area": "sessions",
                "item": "session s1",
                "reason": "user msg_1 dropped",
                "changes_history": True,
            },
        ],
    }
