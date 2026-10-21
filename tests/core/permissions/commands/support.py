"""Shared helpers for the command analysis tables: a fixed folder layout and a plan summary."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from core.permissions.commands.commands import CommandPlan, PlannedPathAccess

# Every table runs in ROOT/w/p with the home folder ROOT/h; expectations name
# paths relative to ROOT with "/" separators, then a wildcard access's pattern
# below its literal folder, "/**" for subtree accesses and " !name" for each
# folder name an exclusion leaves out.
ROOT = Path("C:/") if sys.platform == "win32" else Path("/")
CWD = ROOT / "w" / "p"
HOME = ROOT / "h"


@dataclass(frozen=True)
class Expected:
    commands: tuple[str, ...] | None
    paths: frozenset[str]
    unresolved: frozenset[str]
    urls: tuple[str, ...]
    hosts: tuple[str, ...]


def case(
    line: str,
    *,
    id: str,
    commands: tuple[str, ...] | None = None,
    paths: tuple[str, ...] = (),
    unresolved: tuple[str, ...] = (),
    urls: tuple[str, ...] = (),
    hosts: tuple[str, ...] = (),
    marks: Any = (),
) -> Any:
    """One table row: the plan of *line*; ``commands`` None skips comparing them."""
    expected = Expected(commands, frozenset(paths), frozenset(unresolved), urls, hosts)
    return pytest.param(line, expected, id=id, marks=marks)


def check(plan: CommandPlan, expected: Expected) -> None:
    actual = Expected(
        tuple(
            " ".join(
                [command.name, *(w.text if w.static else f"<{w.text}>" for w in command.words)]
            )
            for command in plan.commands
        ),
        frozenset(_path(access) for access in plan.paths),
        frozenset(element.kind for element in plan.unresolved),
        plan.urls,
        plan.hosts,
    )
    if expected.commands is None:
        actual = Expected(None, actual.paths, actual.unresolved, actual.urls, actual.hosts)
    assert actual == expected, plan


def _path(access: PlannedPathAccess) -> str:
    try:
        text = access.path.relative_to(ROOT).as_posix()
    except ValueError:
        text = access.path.as_posix()
    if access.glob is not None:
        text = f"{text}/{access.glob}"
    pruned = "".join(f" !{name}" for name in access.pruned)
    return f"{access.kind} {text}{'/**' if access.subtree else ''}{pruned}"
