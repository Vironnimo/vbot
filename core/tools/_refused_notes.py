"""Name what the permission rules left out of a listing, a search or a suggestion.

A restricted Session's listings, searches and missing-path suggestions skip
what the rules refuse to read. Silently shortened results read as complete, so
an Agent concluded that a refused folder was absent or empty; each note names
the left-out parts instead. That reveals nothing new: the names are direct
entries of a folder the Agent may read, or parts of deny rules its System
Prompt lists; vBot's own protected locations are named only as such.
"""

from __future__ import annotations

from collections.abc import Sequence

# Names a note spells out; the rest are counted.
_NAMED = 5
_PROTECTED = "vBot's own files"


def listing_note(names: Sequence[str]) -> str:
    """The note under a directory listing that leaves out the entries ``names``."""
    return f"[Not listed: {_named(names)} - {_reason(len(names))}]"


def search_note(parts: Sequence[str], *, protected: bool) -> str:
    """The note of a search that left out ``parts`` (and protected locations)."""
    items = len(parts) + protected
    return f"Left out of this search: {_named(parts, protected=protected)} - {_reason(items)}"


def similar_note(names: Sequence[str], *, also: bool) -> str:
    """The sentence of a not-found message for refused entries similar to the path."""
    lead = "Also similar" if also else "Similar"
    return f"{lead}: {_named(names)} - {_reason(len(names))}"


def _reason(items: int) -> str:
    return f"the permission rules refuse reading {'it' if items == 1 else 'them'}."


def _named(names: Sequence[str], *, protected: bool = False) -> str:
    shown = list(names[:_NAMED])
    if len(names) > _NAMED:
        shown.append(f"{len(names) - _NAMED} more")
    if protected:
        shown.append(_PROTECTED)
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} and {shown[-1]}"
