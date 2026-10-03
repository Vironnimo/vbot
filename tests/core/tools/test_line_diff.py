"""Minimal line diffs shared by change statistics and diff presentation."""

from __future__ import annotations

import random

import pytest

import core.tools._line_diff as line_diff_module
from core.tools._line_diff import grouped_line_opcodes, line_change_counts, line_opcodes


def _longest_common_subsequence(old: list[int], new: list[int]) -> int:
    lengths = [[0] * (len(new) + 1) for _ in range(len(old) + 1)]
    for x in range(len(old) - 1, -1, -1):
        for y in range(len(new) - 1, -1, -1):
            lengths[x][y] = (
                lengths[x + 1][y + 1] + 1
                if old[x] == new[y]
                else max(lengths[x + 1][y], lengths[x][y + 1])
            )
    return lengths[0][0]


def _assert_script(old: list[int], new: list[int]) -> tuple[int, int]:
    """Assert the opcodes rebuild ``new`` from ``old``; return their (added, removed)."""
    rebuilt: list[int] = []
    old_at = new_at = 0
    added = removed = 0
    for tag, i1, i2, j1, j2 in line_opcodes(old, new):
        assert (i1, j1) == (old_at, new_at)
        if tag == "equal":
            assert old[i1:i2] == new[j1:j2] and i2 > i1
        else:
            assert tag == ("replace" if i2 > i1 and j2 > j1 else "delete" if i2 > i1 else "insert")
            added += j2 - j1
            removed += i2 - i1
        rebuilt.extend(new[j1:j2])
        old_at, new_at = i2, j2
    assert (old_at, new_at) == (len(old), len(new))
    assert rebuilt == new
    return added, removed


def test_scripts_are_minimal_like_git() -> None:
    """Counts equal git's numstat: lines outside a longest common subsequence."""
    generator = random.Random(7)
    for _ in range(1500):
        old = [generator.randint(0, 5) for _ in range(generator.randint(0, 20))]
        new = list(old)
        for _ in range(generator.randint(0, 6)):
            choice = generator.random()
            if choice < 0.3 and new:
                new.pop(generator.randrange(len(new)))
            elif choice < 0.6:
                new.insert(generator.randint(0, len(new)), generator.randint(0, 8))
            elif new:
                new[generator.randrange(len(new))] = generator.randint(0, 8)
        if generator.random() < 0.3:
            new = [generator.randint(0, 5) for _ in range(generator.randint(0, 20))]
        common = _longest_common_subsequence(old, new)
        expected = (len(new) - common, len(old) - common)
        assert line_change_counts(old, new) == expected
        assert _assert_script(old, new) == expected


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # Line endings are content: a trailing newline or CRLF change is a changed line.
        (["a\n", "b"], ["a\n", "b\n"], (1, 1)),
        (["a\r\n", "b\r\n"], ["a\n", "b\r\n"], (1, 1)),
        # Frequently repeated lines stay ordinary diff units (git reports 1/1).
        (["x\n"] * 300 + ["unique\n"], ["x\n"] * 300 + ["changed\n"], (1, 1)),
        ([], ["a\n", "b\n"], (2, 0)),
        (["a\n", "b\n"], [], (0, 2)),
        ([], [], (0, 0)),
    ],
)
def test_counts_treat_every_line_as_content(
    old: list[str], new: list[str], expected: tuple[int, int]
) -> None:
    assert line_change_counts(old, new) == expected


def test_a_change_beyond_the_search_limit_reads_as_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(line_diff_module, "MAX_EDIT_DISTANCE", 2)
    old = ["keep\n", "a\n", "b\n", "c\n", "d\n", "keep end\n"]
    new = ["keep\n", "d\n", "c\n", "b\n", "a\n", "keep end\n"]

    assert line_change_counts(old, new) == (4, 4)
    assert line_opcodes(old, new) == [
        ("equal", 0, 1, 0, 1),
        ("replace", 1, 5, 1, 5),
        ("equal", 5, 6, 5, 6),
    ]


def test_hunks_keep_context_and_split_on_long_unchanged_runs() -> None:
    old = [f"{index}\n" for index in range(20)]
    new = list(old)
    new[2] = "two\n"
    new[15] = "fifteen\n"

    assert grouped_line_opcodes(old, old, 3) == []
    assert grouped_line_opcodes(old, new, 3) == [
        [("equal", 0, 2, 0, 2), ("replace", 2, 3, 2, 3), ("equal", 3, 6, 3, 6)],
        [("equal", 12, 15, 12, 15), ("replace", 15, 16, 15, 16), ("equal", 16, 19, 16, 19)],
    ]
