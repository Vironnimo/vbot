"""What one analysis keeps while it walks a script, and helpers over lowered words.

``Location`` is one folder the shell may be in, ``Context`` one script being
analyzed (a command line or a nested shell's script) with its candidate
locations per bash subshell scope, ``Listing`` what a pipeline element hands
on, ``Value`` a word's resolved text. The word helpers work on ``_syntax``
words only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.permissions.commands import _paths
from core.permissions.commands._syntax import Binding, Part, Word
from core.permissions.commands.commands import ShellKind, UnresolvedKind

# A cd that may fail keeps the old folder; beyond this many candidates the folder is unknown.
MAX_CANDIDATES = 16
SOURCE_LIMIT = 500
UNRESOLVED_KIND: Mapping[str, UnresolvedKind] = {
    "read": "path_read",
    "write": "path_write",
    "start_dir": "cwd",
}


@dataclass(frozen=True, slots=True)
class Location:
    """One folder the shell may be in, with what ``cd -``, ``popd`` and drive switches return to."""

    cwd: Path | None
    previous: Path | None = None
    stack: tuple[Path | None, ...] = ()
    drives: tuple[tuple[str, Path], ...] = ()

    def moved(self, target: Path | None) -> Location:
        drives = dict(self.drives)
        old_drive, new_drive = _paths.drive_of(self.cwd), _paths.drive_of(target)
        if self.cwd is not None and old_drive is not None and old_drive != new_drive:
            drives[old_drive] = self.cwd
        return Location(target, self.cwd, self.stack, tuple(sorted(drives.items())))


# One entry a listing names: a path, the source text and the pattern of the entries
# below it that it lists (None: the path itself).
type ListedRoot = tuple[Path, str, str | None]


@dataclass(slots=True)
class Listing:
    """What a pipeline element hands on: the entries it lists, or a pass-through (``filter``).

    ``roots`` None: the entries are unknown. ``recursive``: it also lists
    everything below them.
    """

    roots: list[ListedRoot] | None = None
    filter: bool = False
    recursive: bool = False


@dataclass(slots=True)
class Context:
    """One script (a command line or a nested shell's script) being analyzed.

    ``listings`` holds what each pipeline element hands on, by pipeline id and
    index; ``produced`` what a PowerShell command whose output an expression
    uses (``(Get-Item x).Delete()``) names, by its source offset.
    """

    shell: ShellKind
    depth: int
    assigned: set[str]
    environment_changed: bool
    states: dict[tuple[int, ...], tuple[Location, ...]]
    listings: dict[tuple[int, int], Listing] = field(default_factory=dict)
    produced: dict[int, Listing] = field(default_factory=dict)
    redefined: set[str] = field(default_factory=set)
    delayed_expansion: bool = False
    # The folders the script's process started in (where .NET resolves relative paths).
    start: tuple[Location, ...] = ()

    def locations(self, scope: tuple[int, ...]) -> tuple[Location, ...]:
        for length in range(len(scope), -1, -1):
            state = self.states.get(scope[:length])
            if state is not None:
                if length != len(scope):
                    self.states[scope] = state
                return state
        raise AssertionError("the root scope always has a state")


@dataclass(frozen=True, slots=True)
class Value:
    text: str | None  # None: dynamic
    source: str

    @property
    def static(self) -> bool:
        return self.text is not None


def items(word: Word | None) -> tuple[Word, ...]:
    """The words of an array literal, flattened; a plain word alone."""
    if word is None:
        return ()
    if word.items is not None:
        return tuple(item for element in word.items for item in items(element))
    return (word,)


def first_bound(binding: Binding, *names: str) -> Word | None:
    for name in names:
        value = binding.bound.get(name)
        if value is not None:
            return value
    return None


def literal(word: Word) -> str | None:
    """The text of a word without expansions, else None."""
    if all(part.kind == "text" for part in word.parts) and word.items is None:
        return "".join(part.value for part in word.parts)
    return None


def joined(words: Sequence[Word]) -> Word:
    """The words as one, separated by spaces (cmd's ``cd`` takes the rest of the line)."""
    parts: list[Part] = []
    for index, word in enumerate(words):
        if index:
            parts.append(Part("text", " "))
        parts.extend(word.parts)
    return Word(tuple(parts), " ".join(word.source for word in words))


def joined_path(folder: Word, name: Word | None) -> Word:
    if name is None:
        return folder
    separator = Part("text", "\\" if _paths.WINDOWS else "/")
    return Word(
        (*folder.parts, separator, *name.parts),
        f"{folder.source} {name.source}",
        tilde=folder.tilde,
    )


def sibling(source: Word, name: str) -> Word:
    """The word naming *name* in the folder of *source* (``Rename-Item -NewName``)."""
    parts = list(source.parts)
    text = (
        "".join(part.value for part in parts)
        if all(part.kind == "text" for part in parts)
        else None
    )
    if text is None:
        return Word((Part("dynamic", source.source),), source.source)
    cut = max(text.rfind("/"), text.rfind("\\"))
    folder = text[: cut + 1] if cut >= 0 else ""
    return Word.literal(folder + name, source=f"{source.source} -> {name}")


def relative_to(word: Word, directory: Word | None) -> Word:
    if directory is None:
        return word
    return joined_path(directory, word)


def environment_key(name: str) -> str:
    return name.upper() if _paths.WINDOWS else name
