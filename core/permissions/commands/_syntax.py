"""The shell-neutral form every parser lowers a command line into.

PowerShell (through the helper), bash (tree-sitter) and cmd lower their syntax
into a ``Script``: its commands in source order, each with its words as
templates of literal text, variable references and parts that are only known
when the command runs. ``_analysis.py`` gives these words meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type PartKind = Literal["text", "variable", "dynamic"]
type RedirectKind = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class Part:
    """Literal ``text``, a ``variable`` reference by normalized name, or ``dynamic`` source text."""

    kind: PartKind
    value: str


@dataclass(frozen=True, slots=True)
class Word:
    """One command word as written, lowered into parts.

    ``glob``: an unquoted wildcard may expand it (bash; PowerShell decides per
    parameter). ``tilde``: a leading ``~`` expands to the home directory.
    ``items``: the elements of a PowerShell array literal (``a,b``), which a
    native program receives as separate arguments. ``block``: a PowerShell
    script block, whose commands are part of the same lowered script.
    ``parameter``: a PowerShell parameter token (``-Path``), with its attached
    ``argument`` (``-Path:x``).
    """

    parts: tuple[Part, ...]
    source: str
    glob: bool = False
    tilde: bool = False
    items: tuple[Word, ...] | None = None
    block: bool = False
    splat: bool = False
    parameter: str | None = None
    argument: Word | None = None

    @classmethod
    def literal(cls, text: str, *, source: str | None = None, glob: bool = False) -> Word:
        return cls((Part("text", text),), text if source is None else source, glob=glob)


@dataclass(frozen=True, slots=True)
class Redirect:
    kind: RedirectKind
    target: Word


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Position in a pipeline of at least two elements; ``id`` is unique within its ``Script``."""

    id: int
    index: int


@dataclass(slots=True)
class Binding:
    """PowerShell's static parameter binding of one cmdlet or function call."""

    bound: dict[str, Word | None]
    unbound: list[Word]
    errors: list[str]


@dataclass(slots=True)
class RawCommand:
    """One simple command (or, without ``words``, a statement that only redirects).

    ``words[0]`` is the command name as written. ``scope`` is the bash subshell
    path the command runs in (directory changes inside a scope stay there).
    PowerShell adds the ``invocation`` operator (``&``, ``.``), the
    ``resolved`` cmdlet or function name with its ``binding``, and whether the
    name is a function the script defines itself.
    """

    words: list[Word]
    source: str
    redirects: list[Redirect] = field(default_factory=list)
    pipeline: Pipeline | None = None
    scope: tuple[int, ...] = ()
    invocation: str = ""
    resolved: str | None = None
    binding: Binding | None = None
    script_function: bool = False
    stop_parsing: bool = False


@dataclass(slots=True)
class Script:
    """A lowered command line; ``error`` names why it could not be parsed."""

    commands: list[RawCommand] = field(default_factory=list)
    assigned: set[str] = field(default_factory=set)
    environment_changed: bool = False
    error: str | None = None
