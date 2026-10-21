"""``Shell(...)`` command patterns: normalization and matching against simple commands."""

from __future__ import annotations

import functools
import re
from typing import TYPE_CHECKING

from core.permissions.commands._specs import POWERSHELL_ALIASES

if TYPE_CHECKING:
    from core.permissions.commands.commands import SimpleCommand

_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# Stands for a dynamic word in the command text; no pattern can contain it.
_HOLE = "\x00"


def normalize(pattern: str) -> str:
    """Single spaces, Claude Code's legacy ``prefix:*`` as ``prefix *``, the program's bare name."""
    from core.permissions.commands._paths import program_name

    if _CONTROL.search(pattern):
        raise ValueError("a command pattern is one line without control characters")
    text = " ".join(pattern.split())
    if not text:
        raise ValueError("a command pattern names at least a command or *")
    if text.endswith(":*") and not text.endswith(" :*"):
        text = text[:-2].rstrip() + " *"
    first, space, rest = text.partition(" ")
    if "*" not in first and ("/" in first or "\\" in first or "." in first):
        first = program_name(first) or first
    return first + space + rest


def matches(
    pattern: str, command: SimpleCommand, *, case_insensitive: bool, dynamic_matches_any: bool
) -> bool:
    fold = case_insensitive or command.shell != "bash"
    text = command.name + "".join(
        " " + (word.text if word.static else _HOLE) for word in command.words
    )
    for alternative in _alternatives(pattern, command.shell == "powershell"):
        if _HOLE in text and dynamic_matches_any:
            if _intersects(alternative, text, fold=fold):
                return True
        elif _regex(alternative, fold).fullmatch(text):
            return True
    return False


@functools.lru_cache(maxsize=2048)
def _alternatives(pattern: str, powershell: bool) -> tuple[str, ...]:
    """The pattern, PowerShell aliases resolved; a trailing `` *`` also matches the bare name."""
    text = normalize(pattern)
    if powershell:
        first, space, rest = text.partition(" ")
        target = POWERSHELL_ALIASES.get(first.lower())
        if target is not None:
            text = target + space + rest
    return (text, text[:-2]) if text.endswith(" *") else (text,)


@functools.lru_cache(maxsize=2048)
def _regex(pattern: str, fold: bool) -> re.Pattern[str]:
    # A literal character never matches a hole; only "*" spans one.
    body = ".*".join(re.escape(segment) for segment in pattern.split("*"))
    return re.compile(body, re.DOTALL | (re.IGNORECASE if fold else 0))


def _intersects(pattern: str, text: str, *, fold: bool) -> bool:
    """Whether some value of the holes in *text* makes the pattern match.

    Both sides are globs with one wildcard each (``*``, the hole). A literal
    pattern must match the text with each hole as ``.*``. Otherwise, with
    wildcards on both sides, a common string exists exactly when their
    literal starts are prefixes of one another and their literal ends
    suffixes: the longer start, every middle segment of both, and the longer
    end form one.
    """
    if fold:
        pattern, text = pattern.casefold(), text.casefold()
    if "*" not in pattern:
        holes = ".*".join(re.escape(segment) for segment in text.split(_HOLE))
        return re.fullmatch(holes, pattern, re.DOTALL) is not None
    pattern_parts = pattern.split("*")
    text_parts = text.split(_HOLE)
    start_a, start_b = pattern_parts[0], text_parts[0]
    end_a, end_b = pattern_parts[-1], text_parts[-1]
    return (start_a.startswith(start_b) or start_b.startswith(start_a)) and (
        end_a.endswith(end_b) or end_b.endswith(end_a)
    )
