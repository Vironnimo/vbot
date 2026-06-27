"""In-process syntax checks for files just written by ``write``/``edit``.

After a successful write or edit, a fast whole-file parse catches the corruption
class — mashed quotes, truncated content, broken brackets or indentation — at the
moment of the edit instead of much later when something else trips over it. The
check is syntax-only and dependency-free (stdlib parsers plus the already-present
PyYAML); it is **not** a linter, a type checker, or the quality gates, and it
never blocks the write. Callers surface the returned message as a non-fatal
warning in the success envelope so the model can fix what it just broke.

Two entry points mirror the two tools:

- ``warning_for_written_file`` — ``write`` replaces the whole file, so any parse
  error is attributable to this write; no baseline is needed.
- ``warning_for_edited_file`` — ``edit`` is surgical, so the file is parsed both
  before and after and a pre-existing break is never blamed on the edit. The
  comparison is binary (parseable before / after), which sidesteps fragile
  error-message line-number diffing: if the file was already invalid it stays a
  warning, but worded so the model knows the edit did not necessarily cause it.
"""

from __future__ import annotations

import ast
import json
import tomllib
from collections.abc import Callable
from pathlib import Path

import yaml


def _check_python(content: str) -> str | None:
    # Parse the utf-8 *bytes*, not the str: a ``# coding:`` cookie in a str source
    # makes ast.parse raise "encoding declaration in Unicode string" — a false
    # positive. The content was decoded as utf-8 on read, so re-encoding is exact.
    try:
        ast.parse(content.encode("utf-8"))
    except SyntaxError as error:
        location = f" (line {error.lineno}, column {error.offset})" if error.lineno else ""
        return f"{type(error).__name__}: {error.msg}{location}"
    return None


def _check_json(content: str) -> str | None:
    try:
        json.loads(content)
    except json.JSONDecodeError as error:
        return f"JSONDecodeError: {error.msg} (line {error.lineno}, column {error.colno})"
    return None


def _check_yaml(content: str) -> str | None:
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as error:
        return f"YAMLError: {error}"
    return None


def _check_toml(content: str) -> str | None:
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        return f"TOMLDecodeError: {error}"
    return None


# Extension → whole-file syntax checker. Each returns None when the content
# parses, or a one-line error message (with line/column where available).
_CHECKERS_BY_SUFFIX: dict[str, Callable[[str], str | None]] = {
    ".py": _check_python,
    ".json": _check_json,
    ".yaml": _check_yaml,
    ".yml": _check_yaml,
    ".toml": _check_toml,
}


def _checker_for(path: Path) -> Callable[[str], str | None] | None:
    return _CHECKERS_BY_SUFFIX.get(path.suffix.lower())


def warning_for_written_file(path: Path, content: str) -> str | None:
    """Return a syntax warning for freshly written content, or None.

    The whole file is new, so any parse error belongs to this write. Files with
    no recognized extension are skipped (None).
    """
    checker = _checker_for(path)
    if checker is None:
        return None
    error = checker(content)
    if error is None:
        return None
    return f"Syntax check failed after this write: {error}"


def warning_for_edited_file(path: Path, before: str, after: str) -> str | None:
    """Return a syntax warning for an edit, never blaming pre-existing breakage.

    Returns None when the post-edit content parses (the edit kept or made the
    file valid) and when the file has no recognized extension. When the result is
    invalid, the message distinguishes an error this edit introduced from one the
    file already had.
    """
    checker = _checker_for(path)
    if checker is None:
        return None
    after_error = checker(after)
    if after_error is None:
        return None
    if checker(before) is None:
        # The file parsed before this edit and does not now — the edit broke it.
        return f"Syntax check failed after this edit: {after_error}"
    # The file was already invalid before this edit; do not attribute it.
    return (
        "File was already syntactically invalid before this edit and still is "
        f"(this edit did not necessarily cause it): {after_error}"
    )


__all__ = ["warning_for_edited_file", "warning_for_written_file"]
