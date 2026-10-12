"""Edit distance between short texts such as names, for typo-tolerant lookups."""

from __future__ import annotations


def edit_distance(first: str, second: str, bound: int) -> int:
    """Return the optimal string alignment distance, or ``bound + 1`` once it exceeds ``bound``.

    One edit inserts, removes or replaces a character, or swaps two adjacent ones.
    Characters compare as given; callers fold case themselves.
    """
    if abs(len(first) - len(second)) > bound:
        return bound + 1
    earlier: list[int] = []
    previous = list(range(len(second) + 1))
    for i, left in enumerate(first, 1):
        current = [i]
        for j, right in enumerate(second, 1):
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right))
            if i > 1 and j > 1 and left == second[j - 2] and first[i - 2] == right:
                value = min(value, earlier[j - 2] + 1)
            current.append(value)
        if min(current) > bound:
            return bound + 1
        earlier, previous = previous, current
    return min(previous[-1], bound + 1)
