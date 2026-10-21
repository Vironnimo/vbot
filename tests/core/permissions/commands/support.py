"""Shared helpers for the command analysis tables: a fixed folder layout and a plan summary."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from core.permissions.commands.commands import CommandPlan

# Every table runs in ROOT/w/p with the home folder ROOT/h; expectations name
# paths relative to ROOT with "/" separators and "/**" for subtree accesses.
ROOT = Path("C:/") if sys.platform == "win32" else Path("/")
CWD = ROOT / "w" / "p"
HOME = ROOT / "h"


@dataclass(frozen=True)
class Expected:
    commands: tuple[str, ...] | None
    paths: frozenset[str]
    unresolved: frozenset[str]
    urls: tuple[str, ...]


def case(
    line: str,
    *,
    id: str,
    commands: tuple[str, ...] | None = None,
    paths: tuple[str, ...] = (),
    unresolved: tuple[str, ...] = (),
    urls: tuple[str, ...] = (),
    marks: Any = (),
) -> Any:
    """One table row: the plan of *line*; ``commands`` None skips comparing them."""
    expected = Expected(commands, frozenset(paths), frozenset(unresolved), urls)
    return pytest.param(line, expected, id=id, marks=marks)


def check(plan: CommandPlan, expected: Expected) -> None:
    actual = Expected(
        tuple(
            " ".join(
                [command.name, *(w.text if w.static else f"<{w.text}>" for w in command.words)]
            )
            for command in plan.commands
        ),
        frozenset(_path(access.kind, access.path, access.subtree) for access in plan.paths),
        frozenset(element.kind for element in plan.unresolved),
        plan.urls,
    )
    if expected.commands is None:
        actual = Expected(None, actual.paths, actual.unresolved, actual.urls)
    assert actual == expected, plan


def _path(kind: str, path: Path, subtree: bool) -> str:
    try:
        text = path.relative_to(ROOT).as_posix()
    except ValueError:
        text = path.as_posix()
    return f"{kind} {text}{'/**' if subtree else ''}"
