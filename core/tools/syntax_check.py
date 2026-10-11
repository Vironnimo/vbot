"""In-process syntax checks for files just written by ``apply_patch``.

After a successful write or edit, a fast whole-file parse catches the corruption
class — mashed quotes, truncated content, broken brackets or indentation — at the
moment of the edit instead of much later when something else trips over it. The
check is syntax-only and dependency-free (stdlib parsers plus the already-present
PyYAML); it is **not** a linter or a type checker, and it never blocks the
write. Callers surface the returned message as a non-fatal
warning in the success envelope so the model can fix what it just broke.

Two entry points mirror the two tools:

- ``warning_for_written_file`` — Add replaces the whole file, so any parse
  error is attributable to this write; no baseline is needed.
- ``warning_for_edited_file`` — ``apply_patch`` is surgical, so the file is parsed both
  before and after and a pre-existing break is never blamed on the edit. The
  comparison is binary (parseable before / after), which sidesteps fragile
  error-message line-number diffing: if the file was already invalid it stays a
  warning, but worded so the model knows the edit did not necessarily cause it.

A warning for an error this write or edit caused is followed by the lines around
the error, numbered like ``read`` output, with a caret under the reported column:
without them, Agents read 30-45 lines to find the spot (Sessions, 2026-10).
"""

from __future__ import annotations

import ast
import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.tools._change_preview import preview_window, render_preview_line
from core.tools.arguments import split_text_lines

# Lines shown above and below the line an error names.
_CONTEXT_LINES = 2


@dataclass(frozen=True)
class _Problem:
    """A parse error: its one-line message and, where the parser names it, its place."""

    message: str
    line: int | None = None
    column: int | None = None


def _check_python(content: str) -> _Problem | None:
    # Parse the utf-8 *bytes*, not the str: a ``# coding:`` cookie in a str source
    # makes ast.parse raise "encoding declaration in Unicode string" — a false
    # positive. The content was decoded as utf-8 on read, so re-encoding is exact.
    try:
        ast.parse(content.encode("utf-8"))
    except SyntaxError as error:
        location = f" (line {error.lineno}, column {error.offset})" if error.lineno else ""
        return _Problem(
            f"{type(error).__name__}: {error.msg}{location}", error.lineno, error.offset
        )
    return None


def _check_json(content: str) -> _Problem | None:
    try:
        json.loads(content)
    except json.JSONDecodeError as error:
        return _Problem(
            f"JSONDecodeError: {error.msg} (line {error.lineno}, column {error.colno})",
            error.lineno,
            error.colno,
        )
    return None


def _check_yaml(content: str) -> _Problem | None:
    # PyYAML's message already quotes the line and marks the column.
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as error:
        return _Problem(f"YAMLError: {error}")
    return None


def _check_toml(content: str) -> _Problem | None:
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        return _Problem(f"TOMLDecodeError: {error}", error.lineno, error.colno)
    return None


# Extension → whole-file syntax checker. Each returns None when the content
# parses, or the error with its line and column where available.
_CHECKERS_BY_SUFFIX: dict[str, Callable[[str], _Problem | None]] = {
    ".py": _check_python,
    ".json": _check_json,
    ".yaml": _check_yaml,
    ".yml": _check_yaml,
    ".toml": _check_toml,
}


def _checker_for(path: Path) -> Callable[[str], _Problem | None] | None:
    return _CHECKERS_BY_SUFFIX.get(path.suffix.lower())


def _with_excerpt(lead: str, problem: _Problem, content: str) -> str:
    """Return ``lead`` followed by the lines around the error and a caret under its column."""
    lines = split_text_lines(content)
    if problem.line is None or not lines or problem.line < 1:
        return lead
    # A parser names the line after the last one for an error at the very end.
    number = min(problem.line, len(lines))
    column = problem.column if number == problem.line else None
    shown = [lead]
    for current in range(
        max(1, number - _CONTEXT_LINES), min(len(lines), number + _CONTEXT_LINES) + 1
    ):
        text = lines[current - 1]
        focus = column if current == number else None
        rendered = render_preview_line(text, current, focus)
        shown.append(rendered)
        if focus is None or focus < 1:
            continue
        start, _ = preview_window(text, focus)
        gutter = rendered.index("|") + 2
        # Tabs stay tabs, so the caret lines up under the character it marks.
        before = "".join(
            "\t" if character == "\t" else " " for character in text[start : focus - 1]
        )
        shown.append(" " * gutter + before + "^")
    return "\n".join(shown)


def warning_for_written_file(path: Path, content: str) -> str | None:
    """Return a syntax warning for freshly written content, or None.

    The whole file is new, so any parse error belongs to this write. Files with
    no recognized extension are skipped (None). The warning's first line is the
    error; the lines around it follow.
    """
    checker = _checker_for(path)
    if checker is None:
        return None
    problem = checker(content)
    if problem is None:
        return None
    return _with_excerpt(
        f"Syntax check failed after this write: {problem.message}", problem, content
    )


def warning_for_edited_file(path: Path, before: str, after: str) -> str | None:
    """Return a syntax warning for an edit, never blaming pre-existing breakage.

    Returns None when the post-edit content parses (the edit kept or made the
    file valid) and when the file has no recognized extension. When the result is
    invalid, the message distinguishes an error this edit introduced, followed by
    the lines around it, from one the file already had.
    """
    checker = _checker_for(path)
    if checker is None:
        return None
    problem = checker(after)
    if problem is None:
        return None
    if checker(before) is None:
        # The file parsed before this edit and does not now — the edit broke it.
        return _with_excerpt(
            f"Syntax check failed after this edit: {problem.message}", problem, after
        )
    # The file was already invalid before this edit; do not attribute it.
    return (
        "File was already syntactically invalid before this edit and still is "
        f"(this edit did not necessarily cause it): {problem.message}"
    )


__all__ = ["warning_for_edited_file", "warning_for_written_file"]
