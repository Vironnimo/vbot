"""Grep: search behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.tools.grep import (
    MAX_OUTPUT_BYTES,
    grep_handler,
)
from core.tools.search import RESULTS_LIMITED_MARKER
from tests.core.tools.grep_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    force_python_fallback,
    install_fake_rg,
    make_context,
)


def test_grep_output_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("one.txt").write_text("hit\nmiss\nhit\n", encoding="utf-8")
    workspace.joinpath("two.txt").write_text("hit\n", encoding="utf-8")

    files_result = grep_handler(
        make_context(workspace), {"pattern": "hit", "output_mode": "files_with_matches"}
    )
    count_result = grep_handler(make_context(workspace), {"pattern": "hit", "output_mode": "count"})

    files_data = assert_success_envelope(files_result)
    count_data = assert_success_envelope(count_result)
    assert files_data["content"] == "one.txt\ntwo.txt"
    assert count_data["content"] == "one.txt:2\ntwo.txt:1"


@pytest.mark.parametrize("output_mode", ["files_with_matches", "count"])
def test_grep_rejects_context_outside_content_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_mode: str,
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("one.txt").write_text("hit\n", encoding="utf-8")

    result = grep_handler(
        make_context(workspace),
        {"pattern": "hit", "output_mode": output_mode, "context": 1},
    )

    assert_failure_envelope(result, "invalid_arguments")


def test_grep_literal_and_ignore_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("Alpha.1\nalphaX1\n", encoding="utf-8")

    regex_result = grep_handler(make_context(workspace), {"pattern": "Alpha.1"})
    literal_result = grep_handler(
        make_context(workspace), {"pattern": "Alpha.1", "literal": True, "ignore_case": True}
    )

    regex_data = assert_success_envelope(regex_result)
    literal_data = assert_success_envelope(literal_result)
    assert regex_data["content"] == "notes.txt:1: Alpha.1"
    assert literal_data["content"] == "notes.txt:1: Alpha.1"


def test_grep_glob_filter_limits_candidate_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("keep.py").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("skip.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "*.py"})
    data = assert_success_envelope(result)
    assert data["content"] == "keep.py:1: needle"


@pytest.mark.parametrize("glob_pattern", ["/absolute/*.py", "../*.py"])
def test_grep_rejects_invalid_glob_filter_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, glob_pattern: str
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": glob_pattern})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "glob" in error["message"]


def test_grep_context_lines_in_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "context": 1})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: before\nnotes.txt:2: needle\nnotes.txt:3: after"


def test_grep_no_matches_returns_success_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "missing"})
    data = assert_success_envelope(result)
    assert data["content"] == "No matches found for pattern: missing"


def test_grep_adds_limit_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

    context = make_context(workspace)
    result = grep_handler(context, {"pattern": "hit", "limit": 2})
    data = assert_success_envelope(result)
    assert data["content"] == (
        "notes.txt:1: hit\nnotes.txt:2: hit\n[Results limited to 2 matches.]"
    )
    assert context.presentation_facts == [
        {"kind": "count", "value": 2, "unit": "matches", "at_least": True}
    ]


def test_grep_truncates_long_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    long_line = "needle" + "x" * 600
    workspace.joinpath("notes.txt").write_text(long_line, encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle"})
    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert content.startswith("notes.txt:1: needle")
    assert content.endswith("...[truncated]")
    assert len(content) < len("notes.txt:1: " + long_line)


def test_grep_caps_large_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text(
        "\n".join(f"needle {index} " + "x" * 490 for index in range(130)),
        encoding="utf-8",
    )

    result = grep_handler(make_context(workspace), {"pattern": "needle", "limit": 130})
    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[... output truncated ...]" in content
    assert len(content.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_grep_skips_read_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bad = workspace / "bad.txt"
    good = workspace / "good.txt"
    bad.write_text("needle\n", encoding="utf-8")
    good.write_text("needle\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text_or_fail(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == bad:
            raise PermissionError("blocked")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_or_fail)

    result = grep_handler(make_context(workspace), {"pattern": "needle"})
    data = assert_success_envelope(result)
    assert data["content"] == "good.txt:1: needle"


def test_grep_rg_command_enables_multiline_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = install_fake_rg(monkeypatch)

    grep_handler(make_context(workspace), {"pattern": "alpha.beta", "multiline": True})

    command = created[0].command
    assert "--multiline" in command
    assert "--multiline-dotall" in command


def test_grep_multiline_matches_across_lines_in_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "alpha.beta", "multiline": True})

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: alpha\nnotes.txt:2: beta"


def test_grep_multiline_counts_matches_not_lines_in_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("alpha\nbeta\nalpha\nbeta\n", encoding="utf-8")

    result = grep_handler(
        make_context(workspace),
        {"pattern": "alpha.beta", "multiline": True, "output_mode": "count"},
    )

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:2"


def test_grep_pages_results_with_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "offset": 1, "limit": 1})

    data = assert_success_envelope(result)
    assert data["content"] == (f"notes.txt:2: hit\n{RESULTS_LIMITED_MARKER.format(limit=1)}")


def test_grep_reports_offset_beyond_total_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\nhit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "offset": 5})

    data = assert_success_envelope(result)
    assert data["content"] == "No results at offset 5; 2 matches total."
