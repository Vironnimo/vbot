"""Name what the permission rules left out of a listing or a suggestion.

A restricted Session's listings and missing-path suggestions skip
what the rules refuse to read. Silently shortened results read as complete, so
an Agent concluded that a refused folder was absent or empty; each note names
the left-out parts instead. That reveals nothing new: the names are direct
entries of a folder the Agent may read.
"""

from __future__ import annotations

from collections.abc import Sequence

# Names a note spells out; the rest are counted.
_NAMED = 5


def listing_note(names: Sequence[str]) -> str:
    """The note under a directory listing that leaves out the entries ``names``."""
    return f"[Not listed: {_named(names)} - {_reason(len(names))}]"


def similar_note(names: Sequence[str], *, also: bool) -> str:
    """The sentence of a not-found message for refused entries similar to the path."""
    lead = "Also similar" if also else "Similar"
    return f"{lead}: {_named(names)} - {_reason(len(names))}"


def _reason(items: int) -> str:
    return f"the permission rules refuse reading {'it' if items == 1 else 'them'}."


def _named(names: Sequence[str]) -> str:
    shown = list(names[:_NAMED])
    if len(names) > _NAMED:
        shown.append(f"{len(names) - _NAMED} more")
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} and {shown[-1]}"
