"""Tests for the in-process post-write syntax check."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools.syntax_check import warning_for_edited_file, warning_for_written_file


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("module.py", "def f():\n    return 1\n"),
        ("data.json", '{"a": 1, "b": [2, 3]}'),
        ("config.yaml", "a: 1\nb:\n  - 2\n  - 3\n"),
        ("pyproject.toml", '[tool]\nname = "x"\n'),
        ("notes.txt", "this is not { valid json but txt is unchecked"),
        ("README.md", "# heading\n```\nunbalanced"),
    ],
)
def test_written_valid_or_unchecked_returns_none(name: str, content: str) -> None:
    assert warning_for_written_file(Path(name), content) is None


def test_written_python_with_coding_cookie_is_valid() -> None:
    # A ``# coding:`` cookie in a str source would make ast.parse raise; the
    # checker parses bytes to avoid that false positive.
    content = "# -*- coding: utf-8 -*-\nx = 1\n"
    assert warning_for_written_file(Path("m.py"), content) is None


@pytest.mark.parametrize(
    ("name", "content", "marker"),
    [
        ("module.py", "def f(:\n    return 1\n", "SyntaxError"),
        ("broken.py", "if True:\npass\n", "IndentationError"),
        ("data.json", '{"a": 1,}', "JSONDecodeError"),
        ("config.yaml", "a: 1\n  b: 2\n bad: indent\n", "YAMLError"),
        ("pyproject.toml", "key = = 1\n", "TOMLDecodeError"),
    ],
)
def test_written_broken_reports_parser_error(name: str, content: str, marker: str) -> None:
    warning = warning_for_written_file(Path(name), content)
    assert warning is not None
    assert warning.startswith("Syntax check failed after this write:")
    assert marker in warning


def test_edited_valid_result_returns_none() -> None:
    before = "x = 1\n"
    after = "x = 2\n"
    assert warning_for_edited_file(Path("m.py"), before, after) is None


def test_edited_break_is_attributed_to_the_edit() -> None:
    before = "x = 1\n"
    after = "x = (1\n"  # unbalanced paren introduced by the edit

    warning = warning_for_edited_file(Path("m.py"), before, after)

    assert warning is not None
    assert warning.startswith("Syntax check failed after this edit:")
    assert "SyntaxError" in warning


def test_edited_preexisting_error_is_not_blamed_on_the_edit() -> None:
    before = "def f(:\n    return 1\n"  # already broken before the edit
    after = "def f(:\n    return 2\n"  # still broken, edit changed an unrelated line

    warning = warning_for_edited_file(Path("m.py"), before, after)

    assert warning is not None
    assert "already syntactically invalid before this edit" in warning


def test_edited_fix_clears_the_warning() -> None:
    before = "def f(:\n    return 1\n"  # broken before
    after = "def f():\n    return 1\n"  # the edit fixed it

    assert warning_for_edited_file(Path("m.py"), before, after) is None


def test_edited_unchecked_extension_returns_none() -> None:
    assert warning_for_edited_file(Path("notes.txt"), "{ bad", "{ still bad") is None
