"""Grep: execution behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import core.tools.grep as grep_module
import core.tools.search as search_module
from core.tools.grep import (
    grep_handler,
)
from core.tools.search import RESULTS_LIMITED_MARKER, SEARCH_TIMEOUT_MARKER
from tests.core.tools.grep_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    force_python_fallback,
    get_success_content,
    install_fake_rg,
    make_context,
)


def test_grep_returns_cancelled_failure_when_user_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace, user_cancelled=True), {"pattern": "needle"})

    assert_failure_envelope(result, "cancelled_by_user")


def test_grep_marks_timed_out_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_module, "SEARCH_TIMEOUT_SECONDS", -1.0)

    result = grep_handler(make_context(workspace), {"pattern": "needle"})

    content = get_success_content(result)
    assert SEARCH_TIMEOUT_MARKER in content


def test_grep_uses_python_fallback_when_rg_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("fallback hit\n", encoding="utf-8")

    def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.Popen should not be called without rg")

    monkeypatch.setattr(grep_module.subprocess, "Popen", fail_if_called)

    result = grep_handler(make_context(workspace), {"pattern": "fallback"})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: fallback hit"


def test_grep_returns_failure_for_rg_nonzero_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    install_fake_rg(monkeypatch, stderr_text="some unexpected rg failure", returncode=2)

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    error = assert_failure_envelope(result, "grep_error")
    assert error["message"] == "some unexpected rg failure"


def test_grep_returns_failure_for_discovered_rg_execution_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")

    def raise_oserror(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(grep_module.subprocess, "Popen", raise_oserror)

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    error = assert_failure_envelope(result, "grep_error")
    assert "failed to execute ripgrep" in error["message"]
    assert "permission denied" in error["message"]


def test_grep_uses_rg_success_output_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(grep_module, "subprocess_creation_flags", lambda: 123)
    created = install_fake_rg(monkeypatch, stdout_text="notes.txt:1:hello\n")

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: hello"
    assert created[0].creationflags == 123


def test_grep_stops_reading_rg_output_at_limit_and_kills_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stdout_text = "".join(f"notes.txt:{index}:hit\n" for index in range(1, 6))
    created = install_fake_rg(monkeypatch, stdout_text=stdout_text)

    result = grep_handler(make_context(workspace), {"pattern": "hit", "limit": 2})

    data = assert_success_envelope(result)
    assert data["content"] == (
        f"notes.txt:1: hit\nnotes.txt:2: hit\n{RESULTS_LIMITED_MARKER.format(limit=2)}"
    )
    assert len(created) == 1
    assert created[0].killed is True


def test_grep_renders_rg_paths_relative_to_cwd_or_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    install_fake_rg(monkeypatch, stdout_text="sub\\a.txt:1:alpha\n")

    inside_result = grep_handler(make_context(workspace), {"pattern": "alpha"})
    outside_result = grep_handler(
        make_context(workspace), {"pattern": "alpha", "path": str(outside)}
    )

    assert get_success_content(inside_result) == "sub/a.txt:1: alpha"
    assert get_success_content(outside_result) == (
        f"{(outside / 'sub' / 'a.txt').resolve().as_posix()}:1: alpha"
    )


def test_grep_rejects_unknown_arguments(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = grep_handler(make_context(workspace), {"pattern": "x", "description": "label"})
    error = assert_failure_envelope(result, "invalid_arguments")
    assert isinstance(error["message"], str)


def test_grep_failure_envelope_is_valid_for_missing_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = grep_handler(make_context(workspace), {"pattern": "x", "path": "missing"})
    error = assert_failure_envelope(result, "path_not_found")
    assert "missing" in error["message"]


def test_grep_rg_offset_skips_streamed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stdout_text = "".join(f"notes.txt:{index}:hit\n" for index in range(1, 4))
    install_fake_rg(monkeypatch, stdout_text=stdout_text)

    result = grep_handler(make_context(workspace), {"pattern": "hit", "offset": 1, "limit": 1})

    data = assert_success_envelope(result)
    assert data["content"] == (f"notes.txt:2: hit\n{RESULTS_LIMITED_MARKER.format(limit=1)}")


def test_grep_maps_rg_regex_parse_error_to_invalid_regex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_fake_rg(
        monkeypatch,
        stderr_text="regex parse error:\n    (?=x)\nerror: look-around is not supported",
        returncode=2,
    )

    result = grep_handler(make_context(workspace), {"pattern": "(?=x)"})

    error = assert_failure_envelope(result, "invalid_regex")
    assert "regex parse error" in error["message"]


def test_grep_runs_rg_even_when_python_rejects_the_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # \p{Lu} is valid Rust regex but invalid Python re: the executing engine
    # decides validity, so with rg available the search must succeed.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_fake_rg(monkeypatch, stdout_text="notes.txt:1:Xyz\n")

    result = grep_handler(make_context(workspace), {"pattern": r"\p{Lu}"})

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: Xyz"


def test_grep_reports_invalid_regex_without_rg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("Xyz\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": r"\p{Lu}"})

    error = assert_failure_envelope(result, "invalid_regex")
    assert "invalid regex pattern" in error["message"]
