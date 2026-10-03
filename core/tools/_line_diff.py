"""Minimal line diffs for change statistics and diff presentation.

One diff for every place that counts or shows changed lines (Run change
statistics, Tool diff details, the changed-region preview of ``apply_patch``),
so their counts agree. It is Myers' O(ND) algorithm, the default of
``git diff``: the edit script is minimal, so ``added`` and ``removed`` equal
git's numstat, and the cost grows with the size of the change instead of the
size of the file.

Three reductions keep it fast before the search runs: the common prefix and
suffix are trimmed, lines that occur on only one side are set aside (they can
never match, so they are removed or added in every minimal script), and the
remaining lines are interned to integers. A change too large to search within
``MAX_EDIT_DISTANCE`` reports its searched region as replaced, which bounds
the cost of pathological inputs (reordering a large file) at the price of a
non-minimal count there, like git's own cost limit.
"""

from __future__ import annotations

from array import array
from collections.abc import Hashable, Sequence
from typing import Literal

type LineOpcodeTag = Literal["equal", "replace", "delete", "insert"]
type LineOpcode = tuple[LineOpcodeTag, int, int, int, int]

# Edit distance (removed plus added lines, after the reductions) up to which the
# search finds a minimal script. Its time grows with the square of the distance:
# the limit keeps one pathological diff to a fraction of a second.
MAX_EDIT_DISTANCE = 2000


def line_opcodes[T: Hashable](old: Sequence[T], new: Sequence[T]) -> list[LineOpcode]:
    """Return ``SequenceMatcher``-style opcodes turning ``old`` into ``new``.

    Opcodes cover both sequences in order without gaps; ``equal`` ranges hold
    equal lines, the others what changed.
    """
    return _opcodes(_matched_pairs(old, new), len(old), len(new))


def grouped_line_opcodes[T: Hashable](
    old: Sequence[T], new: Sequence[T], context: int
) -> list[list[LineOpcode]]:
    """Return the opcodes grouped into hunks with ``context`` unchanged lines around changes.

    Matches ``SequenceMatcher.get_grouped_opcodes``: equal runs longer than
    twice the context split hunks. Equal sequences yield no hunk.
    """
    codes = line_opcodes(old, new)
    if all(code[0] == "equal" for code in codes):
        return []
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = (tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2)
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = (tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context))
    groups: list[list[LineOpcode]] = []
    group: list[LineOpcode] = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > 2 * context:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return groups


def line_change_counts[T: Hashable](old: Sequence[T], new: Sequence[T]) -> tuple[int, int]:
    """Return ``(added, removed)`` line counts of the minimal diff from ``old`` to ``new``."""
    reduced = _Reduced(old, new)
    found = _search(reduced.old_kept, reduced.new_kept, trace=None)
    matched = reduced.prefix + reduced.suffix
    if found is not None:
        matched += (len(reduced.old_kept) + len(reduced.new_kept) - found) // 2
    return len(new) - matched, len(old) - matched


class _Reduced[T: Hashable]:
    """The lines a search still has to align: shared lines between the common prefix and suffix."""

    def __init__(self, old: Sequence[T], new: Sequence[T]) -> None:
        old_end, new_end = len(old), len(new)
        lo = 0
        while lo < old_end and lo < new_end and old[lo] == new[lo]:
            lo += 1
        while old_end > lo and new_end > lo and old[old_end - 1] == new[new_end - 1]:
            old_end -= 1
            new_end -= 1
        in_new = set(new[lo:new_end])
        in_old = set(old[lo:old_end])
        self.prefix = lo
        self.suffix = len(old) - old_end
        self.old_index = [index for index in range(lo, old_end) if old[index] in in_new]
        self.new_index = [index for index in range(lo, new_end) if new[index] in in_old]
        ids: dict[T, int] = {}
        self.old_kept = [ids.setdefault(old[index], len(ids)) for index in self.old_index]
        self.new_kept = [ids.setdefault(new[index], len(ids)) for index in self.new_index]


def _matched_pairs[T: Hashable](old: Sequence[T], new: Sequence[T]) -> list[tuple[int, int]]:
    """Return the matched ``(old index, new index)`` pairs of a minimal diff, ascending."""
    reduced = _Reduced(old, new)
    pairs = [(index, index) for index in range(reduced.prefix)]
    trace: list[array[int]] = []
    distance = _search(reduced.old_kept, reduced.new_kept, trace=trace)
    # Without lines shared by both sides nothing matches and nothing was traced.
    if distance is not None and reduced.old_kept and reduced.new_kept:
        for old_at, new_at in _backtrack(
            trace, distance, len(reduced.old_kept), len(reduced.new_kept)
        ):
            pairs.append((reduced.old_index[old_at], reduced.new_index[new_at]))
    old_end, new_end = len(old) - reduced.suffix, len(new) - reduced.suffix
    pairs.extend((old_end + offset, new_end + offset) for offset in range(reduced.suffix))
    return pairs


def _opcodes(pairs: list[tuple[int, int]], old_length: int, new_length: int) -> list[LineOpcode]:
    codes: list[LineOpcode] = []
    old_at = new_at = 0
    index = 0
    while index < len(pairs):
        old_match, new_match = pairs[index]
        if old_match > old_at or new_match > new_at:
            tag = _gap_tag(old_match > old_at, new_match > new_at)
            codes.append((tag, old_at, old_match, new_at, new_match))
        run = 1
        while index + run < len(pairs) and pairs[index + run] == (old_match + run, new_match + run):
            run += 1
        codes.append(("equal", old_match, old_match + run, new_match, new_match + run))
        old_at, new_at = old_match + run, new_match + run
        index += run
    if old_at < old_length or new_at < new_length:
        tag = _gap_tag(old_at < old_length, new_at < new_length)
        codes.append((tag, old_at, old_length, new_at, new_length))
    return codes


def _gap_tag(removes: bool, adds: bool) -> LineOpcodeTag:
    if removes and adds:
        return "replace"
    return "delete" if removes else "insert"


def _search(old: list[int], new: list[int], trace: list[array[int]] | None) -> int | None:
    """Return the minimal edit distance, or ``None`` beyond ``MAX_EDIT_DISTANCE``.

    With ``trace``, also record per distance d the furthest x of every
    diagonal -d..d reached with d - 1 edits, from which ``_backtrack`` recovers
    the matched pairs.
    """
    old_length, new_length = len(old), len(new)
    if not old_length or not new_length:
        return old_length + new_length
    limit = min(old_length + new_length, MAX_EDIT_DISTANCE)
    offset = limit + 1
    frontier = array("i", bytes(4 * (2 * limit + 3)))
    for distance in range(limit + 1):
        if trace is not None:
            trace.append(frontier[offset - distance : offset + distance + 1])
        for diagonal in range(-distance, distance + 1, 2):
            x = _step(frontier, offset, distance, diagonal)
            y = x - diagonal
            while x < old_length and y < new_length and old[x] == new[y]:
                x += 1
                y += 1
            frontier[offset + diagonal] = x
            if x >= old_length and y >= new_length:
                return distance
    return None


def _step(frontier: array[int], offset: int, distance: int, diagonal: int) -> int:
    """Return the x a path reaches on ``diagonal`` with one more edit, before its snake."""
    if diagonal == -distance or (
        diagonal != distance and frontier[offset + diagonal - 1] < frontier[offset + diagonal + 1]
    ):
        return frontier[offset + diagonal + 1]
    return frontier[offset + diagonal - 1] + 1


def _backtrack(
    trace: list[array[int]], distance: int, old_length: int, new_length: int
) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    x, y = old_length, new_length
    for step in range(distance, 0, -1):
        previous = trace[step]
        diagonal = x - y
        if diagonal == -step or (
            diagonal != step and previous[diagonal - 1 + step] < previous[diagonal + 1 + step]
        ):
            prior = diagonal + 1
            prior_x = previous[prior + step]
            start_x, start_y = prior_x, prior_x - prior + 1
        else:
            prior = diagonal - 1
            prior_x = previous[prior + step]
            start_x, start_y = prior_x + 1, prior_x - prior
        while x > start_x and y > start_y:
            x -= 1
            y -= 1
            pairs.append((x, y))
        x, y = prior_x, prior_x - prior
    while x > 0 and y > 0:
        x -= 1
        y -= 1
        pairs.append((x, y))
    pairs.reverse()
    return pairs
